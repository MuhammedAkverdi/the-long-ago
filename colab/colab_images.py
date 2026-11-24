# The Long Ago - Colab image generator
# Plain-script copy of colab/the_long_ago_images.ipynb, for review.
# Each "# %%" marker starts one notebook cell.
#
# %% [markdown]
# # The Long Ago - Colab resim üretici
#
# 1. **Runtime → Change runtime type → A100** seç (A100 yoksa L4 de olur).
# 2. Soldaki **🔑 Secrets** paneline `GH_TOKEN` ekle: yalnızca **the-long-ago** reposuna erişimi olan,
#    **Contents: Read and write** izinli bir fine-grained token. **Notebook access** anahtarını aç.
# 3. **Runtime → Run all**. Defter işleri kendisi bekler, resimleri üretir ve GitHub'a yükler.

# %% Kurulum
import subprocess
import sys

subprocess.check_call([
    sys.executable, "-m", "pip", "install", "-q",
    "diffusers>=0.43.0", "transformers", "accelerate", "imageio", "imageio-ffmpeg", "ftfy",
])
print("Kurulum tamam ✓")

# %% Ayarlar
REPO = "MuhammedAkverdi/the-long-ago"

# Alternatif: "black-forest-labs/FLUX.2-klein-4B" (Apache-2.0, 4B, daha az VRAM)
MODEL_ID = "Tongyi-MAI/Z-Image-Turbo"
MODEL_ID_FALLBACKS = ["Z-a-o/Z-Image-Turbo"]   # aynı model, başka Hugging Face adı

JOBS_TAG = "colab-jobs"         # iş JSON'larının durduğu release
RESULT_TAG_PREFIX = "colab-"    # sonuç release'i: colab-<slug>
MAX_JOB_AGE_DAYS = 4            # bundan eski işler atlanır
WAIT_MINUTES = 15               # iş yoksa en fazla bu kadar beklenir (GPU boşa birim yemesin)
AUTO_DISCONNECT = True          # iş bitince/iş yoksa çalışma zamanını kapat (birim harcamayı durdurur)
import os as _os
HEADLESS = _os.environ.get("COLAB_HEADLESS") == "1"   # started by the pipeline through the Colab API
if HEADLESS:
    AUTO_DISCONNECT = False     # the pipeline deletes the runtime itself
    PREVIEW_EVERY = 0
POLL_SECONDS = 30               # kaç saniyede bir bakılır

# VRAM yetmezse daha küçük boyut ver, ör. {"16:9": (1344, 768), "9:16": (768, 1344)}
SIZES = {
    "16:9": (1920, 1088),       # (genişlik, yükseklik)
    "9:16": (1088, 1920),
}
NUM_STEPS = 9
GUIDANCE = 0.0
JPEG_QUALITY = 92
PREVIEW_EVERY = 0 if HEADLESS else 10  # her N resimde bir küçük önizleme göster (0 = kapalı)

WORK_DIR = "/content/the_long_ago_work"

# Resimler bittikten sonra birkaç kilit sahneyi 5 sn'lik videoya çevirir (Apache-2.0 model).
VIDEO_CLIPS = True
VIDEO_MODEL_ID = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
VIDEO_SIZE = (1280, 704)        # (genişlik, yükseklik) - modelin 720p boyutu
VIDEO_FRAMES = 121              # 24 fps'te ~5 sn
VIDEO_STEPS = 30
VIDEO_GUIDANCE = 5.0
VIDEO_MAX_MINUTES = 50          # bu süre dolunca kalan klipler atlanır (birim harcaması sınırlı kalsın)
VIDEO_NEGATIVE = ("text, letters, watermark, subtitles, deformed hands, extra fingers, distorted face, "
                  "flicker, jitter, fast motion, modern objects, blurry, low quality")

# %% Yardımcı fonksiyonlar
import io
import json
import time
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import requests

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"


def get_token():
    """Read GH_TOKEN from the environment (headless), Colab Secrets, or a getpass prompt."""
    if _os.environ.get("GH_TOKEN"):
        return _os.environ["GH_TOKEN"].strip()
    try:
        from google.colab import userdata
        token = userdata.get("GH_TOKEN")
        if token:
            return token.strip()
    except Exception as exc:  # secret missing, notebook access off, or not in Colab
        print(f"Secrets'tan GH_TOKEN okunamadı ({type(exc).__name__}); elle gir.")
    from getpass import getpass
    return getpass("GH_TOKEN: ").strip()


TOKEN = get_token()
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
session = requests.Session()
session.headers.update(HEADERS)


def gh(method, url, **kwargs):
    """Send an authenticated GitHub API request; a bare path is prefixed with the repo URL."""
    if not url.startswith("http"):
        url = f"{API}/repos/{REPO}{url}"
    kwargs.setdefault("timeout", 60)
    return session.request(method, url, **kwargs)


def check_token():
    """Fail early with a Turkish message if the token cannot see the repo."""
    r = gh("GET", "")
    if r.status_code != 200:
        raise SystemExit(f"Token repoya erişemiyor (HTTP {r.status_code}). İzinleri kontrol et.")
    print(f"GitHub bağlantısı tamam ✓ ({REPO})")


def get_release(tag):
    """Return the release with this tag, or None if it does not exist."""
    r = gh("GET", f"/releases/tags/{tag}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def list_assets(release):
    """Return every asset of a release, following pagination."""
    assets, page = [], 1
    while True:
        r = gh("GET", f"/releases/{release['id']}/assets",
               params={"per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        assets.extend(batch)
        if len(batch) < 100:
            return assets
        page += 1


def download_asset(asset_id):
    """Download an asset's bytes; the redirect target is fetched without the auth header."""
    r = gh("GET", f"/releases/assets/{asset_id}",
           headers={"Accept": "application/octet-stream"}, allow_redirects=False)
    if r.status_code in (301, 302, 303, 307, 308):
        r = requests.get(r.headers["Location"], timeout=300)
    r.raise_for_status()
    return r.content


def delete_asset(asset_id):
    """Delete a release asset by id."""
    r = gh("DELETE", f"/releases/assets/{asset_id}")
    if r.status_code not in (204, 404):
        r.raise_for_status()


def ensure_release(tag):
    """Return the release with this tag, creating it as a prerelease if needed."""
    r = gh("POST", "/releases", json={
        "tag_name": tag, "name": tag, "prerelease": True, "body": "Colab images",
    })
    if r.status_code == 201:
        return r.json()
    if r.status_code == 422:  # already exists
        rel = get_release(tag)
        if rel:
            return rel
    r.raise_for_status()
    raise RuntimeError(f"Release oluşturulamadı: {tag} (HTTP {r.status_code})")


def upload_asset(release, name, data, content_type, retries=3):
    """Upload bytes as a release asset, retrying on network errors and 5xx with backoff."""
    url = f"{UPLOADS}/repos/{REPO}/releases/{release['id']}/assets"
    for attempt in range(retries + 1):
        try:
            r = session.post(url, params={"name": name}, data=data,
                             headers={"Content-Type": content_type}, timeout=600)
            if r.status_code < 500:
                r.raise_for_status()
                return r.json()
            err = f"HTTP {r.status_code}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            err = type(exc).__name__
        if attempt == retries:
            raise RuntimeError(f"{name} yüklenemedi: {err}")
        wait = 5 * 2 ** attempt
        print(f"  Yükleme hatası ({err}), {wait} sn sonra tekrar denenecek…")
        time.sleep(wait)
        # A failed upload can leave a broken asset with the same name behind.
        for a in list_assets(release):
            if a["name"] == name:
                delete_asset(a["id"])


def replace_asset(release, name, data, content_type):
    """Delete any asset with this name, then upload the new one (names must be unique)."""
    for a in list_assets(release):
        if a["name"] == name:
            delete_asset(a["id"])
    return upload_asset(release, name, data, content_type)


def now_utc():
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def parse_time(s):
    """Parse an ISO 8601 timestamp; a naive one is taken as UTC."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_expired(job):
    """True if the job was created more than MAX_JOB_AGE_DAYS ago."""
    return now_utc() - parse_time(job["created"]) > timedelta(days=MAX_JOB_AGE_DAYS)


def is_done(slug):
    """True if the colab-<slug> release already has images.zip."""
    rel = get_release(RESULT_TAG_PREFIX + slug)
    return bool(rel) and any(a["name"] == "images.zip" for a in list_assets(rel))


_job_cache = {}  # asset id -> (updated_at, job dict)


def fetch_jobs():
    """Download all job JSONs from the colab-jobs release (empty list if it does not exist)."""
    rel = get_release(JOBS_TAG)
    if rel is None:
        return []
    jobs = []
    for a in list_assets(rel):
        if not a["name"].endswith(".json"):
            continue
        cached = _job_cache.get(a["id"])
        if cached and cached[0] == a["updated_at"]:
            jobs.append(cached[1])
            continue
        try:
            job = json.loads(download_asset(a["id"]))
            job["slug"], job["created"], job["images"]  # shape check
        except Exception as exc:
            print(f"  Bozuk iş dosyası atlandı: {a['name']} ({exc})")
            continue
        _job_cache[a["id"]] = (a["updated_at"], job)
        jobs.append(job)
    return jobs


def safe_entry_name(path):
    """Return the zip entry name (suffix .jpg) for a job path, rejecting unsafe paths."""
    p = PurePosixPath(path)
    if not path or p.is_absolute() or "\\" in path or ".." in p.parts or ":" in path:
        raise ValueError(f"Güvensiz yol reddedildi: {path!r}")
    return str(p.with_suffix(".jpg"))


def seed_for(path):
    """Stable per-image seed, so reruns give the same picture."""
    return zlib.crc32(path.encode("utf-8")) % 2**31


def show_preview(img, title):
    """Show a small thumbnail in the notebook output."""
    try:
        from IPython.display import display
        thumb = img.copy()
        thumb.thumbnail((480, 480))
        print(f"  Önizleme: {title}")
        display(thumb)
    except Exception:
        pass


check_token()

def pending_jobs(skipped):
    """Return jobs that are neither done, expired nor already handled, oldest first."""
    out = []
    for job in sorted(fetch_jobs(), key=lambda j: parse_time(j["created"])):
        slug = job["slug"]
        if slug in skipped:
            continue
        if is_expired(job):
            print(f"  Süresi geçmiş iş atlandı: {slug}")
            skipped.add(slug)
        elif is_done(slug):
            print(f"  Zaten bitmiş iş atlandı: {slug}")
            skipped.add(slug)
        else:
            out.append(job)
    return out


# %% Model yükleme
def stop_runtime(msg):
    """Release the GPU so no more compute units are spent."""
    print(msg)
    if AUTO_DISCONNECT:
        try:
            from google.colab import runtime
            print("Çalışma zamanı kapatılıyor (birim harcanmasın).")
            runtime.unassign()
        except Exception as e:  # noqa: BLE001
            print(f"Otomatik kapatılamadı ({e}); Runtime → Disconnect and delete runtime yapın.")


def wait_for_job():
    """Wait up to WAIT_MINUTES for a pending job before loading the (heavy) model."""
    end = time.time() + WAIT_MINUTES * 60
    while time.time() < end:
        try:
            if pending_jobs(set()):
                return True
        except Exception as exc:  # noqa: BLE001 - GitHub down or a bad job file: keep waiting until the deadline
            print(f"  İşler okunamadı ({str(exc)[:120]}); tekrar denenecek.")
        print(f"İş bekleniyor… (en fazla {(end - time.time()) / 60:.0f} dk daha)")
        time.sleep(POLL_SECONDS)
    return False


if not wait_for_job():
    stop_runtime(f"{WAIT_MINUTES} dk içinde iş gelmedi.")
    raise SystemExit("İş yok.")

import torch

if not torch.cuda.is_available():
    stop_runtime("GPU yok!")
    raise SystemExit("Runtime → Change runtime type → A100 seçip tekrar çalıştırın.")
gpu_name = torch.cuda.get_device_name(0)
vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
print(f"GPU: {gpu_name} ({vram_gb:.0f} GB)")

try:
    from diffusers import ZImagePipeline as _Pipe
except ImportError:
    from diffusers import DiffusionPipeline as _Pipe

pipe = None


def load_image_pipe():
    """Load the picture model (again, if the video model was loaded in between)."""
    global pipe, MODEL_ID, _video_pipe
    if pipe is not None:
        return pipe
    if _video_pipe is not None:  # free the video model first
        import gc
        _video_pipe = None
        gc.collect()
        torch.cuda.empty_cache()
    err = None
    for mid in [MODEL_ID] + MODEL_ID_FALLBACKS:
        try:
            p = _Pipe.from_pretrained(mid, torch_dtype=torch.bfloat16)
            MODEL_ID = mid
            break
        except Exception as e:  # repo moved/renamed: try the next name
            err, p = e, None
            print(f"{mid} yüklenemedi: {str(e)[:150]}")
    if p is None:
        stop_runtime("Model yüklenemedi.")
        raise RuntimeError(f"Model yüklenemedi: {err}")
    if vram_gb < 30:
        p.enable_model_cpu_offload()
        print("VRAM 30 GB altında: CPU offload açık (biraz daha yavaş).")
    else:
        p.to("cuda")
    p.set_progress_bar_config(disable=True)
    pipe = p
    print(f"Model yüklendi ✓ ({MODEL_ID})")
    return pipe


_video_pipe = None
load_image_pipe()


def generate(prompt, aspect, path):
    """Generate one PIL image for a prompt with a fixed seed derived from its path."""
    if aspect not in SIZES:
        raise ValueError(f"Bilinmeyen en-boy oranı: {aspect!r}")
    width, height = SIZES[aspect]
    generator = torch.Generator("cuda").manual_seed(seed_for(path))
    return load_image_pipe()(
        prompt=prompt,
        width=width,
        height=height,
        num_inference_steps=NUM_STEPS,
        guidance_scale=GUIDANCE,
        generator=generator,
    ).images[0]

# %% Ana döngü


def process_job(job):
    """Claim a job, generate all its images into images.zip and upload it."""
    slug = job["slug"]
    images = job["images"]
    total = len(images)
    print(f"\n▶ İş başlıyor: {slug} ({total} resim)")

    entries = [safe_entry_name(item["path"]) for item in images]  # check all paths first

    release = ensure_release(RESULT_TAG_PREFIX + slug)
    for a in list_assets(release):  # leftovers of an earlier attempt would mislead the pipeline
        if a["name"] in ("clips.zip", "clips.none", "clips.started"):
            delete_asset(a["id"])
    replace_asset(release, "claimed.txt",
                  now_utc().isoformat().encode(), "text/plain")
    print("  İş sahiplenildi ✓")

    job_dir = Path(WORK_DIR) / slug
    job_dir.mkdir(parents=True, exist_ok=True)
    zip_path = job_dir / "images.zip"
    started = time.time()

    want_clip = {v["image"] for v in (job.get("videos") or [])} if VIDEO_CLIPS else set()
    drawn = {}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for i, (item, entry) in enumerate(zip(images, entries), 1):
            img = generate(item["prompt"], item.get("aspect", "16:9"), item["path"])
            if item["path"] in want_clip:
                drawn[item["path"]] = img.convert("RGB")
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=JPEG_QUALITY)
            zf.writestr(entry, buf.getvalue())
            per = (time.time() - started) / i
            left = per * (total - i) / 60
            print(f"  Resim {i}/{total} hazır ({per:.1f} sn/resim, ~{left:.0f} dk kaldı)")
            if i % 5 == 0:  # heartbeat: tells the pipeline this notebook is still alive
                try:
                    replace_asset(release, "progress.txt", f"{i}/{total} {now_utc().isoformat()}".encode(),
                                  "text/plain")
                except Exception as e:  # noqa: BLE001 - a missed heartbeat is not fatal
                    print(f"  (ilerleme bildirilemedi: {str(e)[:80]})")
            if PREVIEW_EVERY and (i == 1 or i % PREVIEW_EVERY == 0):
                show_preview(img, entry)

    size_mb = zip_path.stat().st_size / 1024**2
    print(f"  images.zip yükleniyor ({size_mb:.0f} MB)…")
    # Nothing valid can be there (the job was not done), so clear leftovers first.
    for a in list_assets(release):
        if a["name"] == "images.zip":
            delete_asset(a["id"])
    upload_asset(release, "images.zip", zip_path.read_bytes(), "application/zip")
    print(f"  Yüklendi ✓ {slug} ({(time.time() - started) / 60:.1f} dk)")
    if want_clip:
        try:
            make_clips(job, release, drawn, job_dir)
        except Exception as exc:  # noqa: BLE001 - clips are a bonus: the episode already has its pictures
            print(f"  Video klipler yapılamadı: {type(exc).__name__}: {str(exc)[:300]}")
            try:
                replace_asset(release, "clips.none", str(exc)[:500].encode(), "text/plain")
            except Exception:  # noqa: BLE001
                pass


def load_video_pipe():
    """Free the picture model and load the image-to-video model (once)."""
    global pipe, _video_pipe
    if _video_pipe is not None:
        return _video_pipe
    import gc
    pipe = None
    gc.collect()
    torch.cuda.empty_cache()
    from diffusers import AutoencoderKLWan, WanImageToVideoPipeline
    vae = AutoencoderKLWan.from_pretrained(VIDEO_MODEL_ID, subfolder="vae", torch_dtype=torch.float32)
    vp = WanImageToVideoPipeline.from_pretrained(VIDEO_MODEL_ID, vae=vae, torch_dtype=torch.bfloat16)
    if vram_gb < 30:
        vp.enable_model_cpu_offload()
        try:
            vp.vae.enable_tiling()  # decoding 121 frames at 720p is tight on 24 GB cards
        except Exception:  # noqa: BLE001
            pass
    else:
        vp.to("cuda")
    vp.set_progress_bar_config(disable=True)
    _video_pipe = vp
    print(f"Video modeli yüklendi ✓ ({VIDEO_MODEL_ID})")
    return vp


def fit(img, size):
    """Cover-crop a PIL image to exactly `size`."""
    from PIL import ImageOps
    return ImageOps.fit(img, size)


def make_clips(job, release, drawn, job_dir):
    """Animate a few key pictures into ~5 s clips and upload them as clips.zip."""
    videos = [v for v in job.get("videos") or [] if v["image"] in drawn]
    if not videos:
        return
    print(f"\n🎬 {len(videos)} sahne canlandırılıyor…")
    replace_asset(release, "clips.started", now_utc().isoformat().encode(), "text/plain")
    import threading
    stop_beat = threading.Event()

    def beat():  # model download + first clip take a while: keep telling the pipeline we're alive
        while not stop_beat.wait(150):
            try:
                replace_asset(release, "progress.txt", f"clips {now_utc().isoformat()}".encode(), "text/plain")
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=beat, daemon=True).start()
    try:
        _make_clips(videos, release, drawn, job_dir)
    finally:
        stop_beat.set()


def _make_clips(videos, release, drawn, job_dir):
    from diffusers.utils import export_to_video
    vp = load_video_pipe()
    started, made = time.time(), 0
    zip_path = job_dir / "clips.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for n, v in enumerate(videos, 1):
            if (time.time() - started) / 60 > VIDEO_MAX_MINUTES:
                print(f"  Süre doldu ({VIDEO_MAX_MINUTES} dk); kalan klipler atlandı.")
                break
            entry = PurePosixPath(v["path"])
            if entry.is_absolute() or ".." in entry.parts or entry.suffix != ".mp4":
                continue
            w, h = VIDEO_SIZE
            frames = vp(image=fit(drawn[v["image"]], (w, h)), prompt=v["prompt"], negative_prompt=VIDEO_NEGATIVE,
                        height=h, width=w, num_frames=VIDEO_FRAMES, num_inference_steps=VIDEO_STEPS,
                        guidance_scale=VIDEO_GUIDANCE,
                        generator=torch.Generator("cuda").manual_seed(seed_for(v["path"]))).frames[0]
            out = job_dir / f"clip_{n:02d}.mp4"
            export_to_video(frames, str(out), fps=24)
            zf.write(out, str(entry))
            made += 1
            print(f"  Klip {n}/{len(videos)} hazır ({(time.time() - started) / 60:.1f} dk)")
    if made:
        replace_asset(release, "clips.zip", zip_path.read_bytes(), "application/zip")
        print(f"  clips.zip yüklendi ✓ ({made} klip)")
    else:
        replace_asset(release, "clips.none", b"no clips", "text/plain")


def main():
    """Process pending jobs; stop when none is left (or none arrives for WAIT_MINUTES)."""
    deadline = time.time() + WAIT_MINUTES * 60
    skipped = set()   # done, expired, failed or finished in this session
    done_count = 0
    waiting_msg_shown = False
    try:
        while time.time() < deadline:
            try:
                jobs = pending_jobs(skipped)
            except requests.RequestException as exc:
                print(f"  GitHub'a ulaşılamadı ({exc}); tekrar denenecek.")
                jobs = []
            if not jobs and done_count:
                break  # work done: don't keep the GPU (and its units) waiting
            if not jobs:
                if not waiting_msg_shown:
                    left = (deadline - time.time()) / 60
                    print(f"Yeni iş bekleniyor… (en fazla {left:.0f} dk)")
                    waiting_msg_shown = True
                time.sleep(POLL_SECONDS)
                continue
            deadline = time.time() + WAIT_MINUTES * 60  # a job came: allow a short wait for the next one
            for job in jobs:
                try:
                    process_job(job)
                    done_count += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"  ✗ İş başarısız: {job['slug']}: {type(exc).__name__}: {exc}")
                finally:
                    skipped.add(job["slug"])
                    torch.cuda.empty_cache()
            waiting_msg_shown = False
        print(f"\nBekleyen iş kalmadı. Bitirilen iş: {done_count}.")
    except KeyboardInterrupt:
        print(f"\nDurduruldu. Bitirilen iş: {done_count}.")


try:
    main()
finally:
    stop_runtime("Bitti ✓")
