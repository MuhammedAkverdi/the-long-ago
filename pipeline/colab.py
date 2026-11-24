"""Optional Colab GPU helper: hand the episode's image prompts to a Colab notebook and use its images.

Contract (see colab/the_long_ago_images.ipynb):
- job:    release `colab-jobs`, asset `<slug>.json` = {"slug", "created", "images": [{"path", "prompt", "aspect"}]}
- claim:  release `colab-<slug>` gets asset `claimed.txt` when a notebook starts on the job
- result: release `colab-<slug>` gets asset `images.zip` with `<path minus suffix>.jpg` entries
Nothing here is ever fatal: without Colab the episode is drawn with Cloudflare as before.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import pathlib
import subprocess
import tempfile
import time
import zipfile

from PIL import Image

from . import github, images, thumbnail
from .config import CFG, MOCK, log, write_json

JOBS_TAG = "colab-jobs"


def _cfg() -> dict:
    return CFG.get("colab", {}) or {}


def job_for(slug: str, script: dict) -> dict:
    items = []
    for i, c in enumerate(script["chapters"]):
        for k, p in enumerate(c["image_prompts"]):
            items.append({"path": f"img/ch{i:02d}/{k:03d}.png", "prompt": images.full_prompt(str(p), "16:9"),
                          "aspect": "16:9"})
    items.insert(0, {"path": "thumb_raw.png", "prompt": images.full_prompt(thumbnail.raw_prompt(script["thumbnail_prompt"]),
                                                                           "16:9"), "aspect": "16:9"})
    for s_i, s in enumerate(script.get("shorts", [])):
        for k, p in enumerate((s.get("image_prompts") or ["an ordinary household scene"])[:6]):
            items.append({"path": f"short{s_i}/img/{k:03d}.png", "prompt": images.full_prompt(str(p), "9:16"),
                          "aspect": "9:16"})
    return {"slug": slug, "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "images": items}


def _asset_info(tag: str) -> dict[str, float]:
    """Asset name -> upload time (epoch seconds) for a release; {} when it doesn't exist."""
    proc = subprocess.run(["gh", "release", "view", tag, "--repo", github.repo(), "--json", "assets"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return {}
    out = {}
    for a in json.loads(proc.stdout).get("assets", []):
        stamp = a.get("updatedAt") or a.get("createdAt") or ""
        try:
            out[a["name"]] = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            out[a["name"]] = time.time()
    return out


def _assets(tag: str) -> list[str]:
    return list(_asset_info(tag))


def offer(folder: pathlib.Path, slug: str, script: dict) -> bool:
    """Publish the job (once) so a running notebook can start while the narration is being voiced.

    Returns True when a job for this episode is published."""
    if MOCK or not _cfg().get("enabled"):
        return False
    try:
        marker = folder / "colab_job.json"
        if marker.exists():
            return True
        job = job_for(slug, script)
        job["images"] = [i for i in job["images"]  # hand-made pictures already in place are not redrawn
                         if not ((folder / i["path"]).exists() and (folder / i["path"]).stat().st_size > 1000)]
        if not job["images"]:  # nothing left to draw: don't make a notebook load the model for nothing
            write_json(marker, {"paths": []})
            return False
        for name in _assets(JOBS_TAG):  # only one episode is produced at a time: drop stale jobs first
            if name.endswith(".json") and name != f"{slug}.json":
                subprocess.run(["gh", "release", "delete-asset", JOBS_TAG, name, "--repo", github.repo(), "-y"],
                               capture_output=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / f"{slug}.json"
            path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            github.release(JOBS_TAG, "Colab jobs", "Image jobs for the optional Colab notebook", [path])
        write_json(marker, {"paths": [i["path"] for i in job["images"]]})
        log(f"colab: job published ({len(job['images'])} images)")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"colab: could not publish job ({str(exc)[:200]})")
        return False


def collect(folder: pathlib.Path, slug: str, remote=None) -> int:
    """If a notebook claimed the job, wait for its images and unpack them as cached episode images.

    `remote` is a colab_remote.Remote the pipeline started itself; it is always stopped here."""
    try:
        return _collect(folder, slug, remote)
    finally:
        if remote is not None:
            remote.stop()


def _collect(folder: pathlib.Path, slug: str, remote) -> int:
    if MOCK or not _cfg().get("enabled") or not (folder / "colab_job.json").exists():
        return 0
    try:
        if not pending_paths(folder):
            return 0  # every image is already here (e.g. a resumed run)
        tag = f"colab-{slug}"
        claim_minutes = _cfg().get("remote_claim_minutes", 25) if remote is not None else _cfg().get(
            "claim_wait_minutes", 3)  # our own runtime still has to boot, install and load the model
        claim_deadline = time.time() + 60 * float(claim_minutes)
        names = _assets(tag)
        while "images.zip" not in names and "claimed.txt" not in names and time.time() < claim_deadline:
            time.sleep(30)
            names = _assets(tag)
        if "images.zip" not in names and "claimed.txt" not in names:
            log("colab: no notebook took the job; using Cloudflare")
            return 0
        deadline = time.time() + 60 * float(_cfg().get("result_wait_minutes", 90))
        stale_after = 60 * float(_cfg().get("heartbeat_minutes", 12))
        while "images.zip" not in names and time.time() < deadline:
            info = _asset_info(tag)
            names = list(info)
            if "images.zip" in names:
                break
            beat = max([info[n] for n in ("claimed.txt", "progress.txt") if n in info], default=0)
            if time.time() - beat > stale_after:
                log("colab: notebook stopped sending progress; using Cloudflare")
                return 0
            log("colab: notebook is drawing; waiting")
            time.sleep(60)
        if "images.zip" not in names:
            log("colab: notebook did not finish in time; using Cloudflare")
            return 0
        with tempfile.TemporaryDirectory() as tmp:
            github.download_release(tag, "images.zip", pathlib.Path(tmp))
            return _unpack(pathlib.Path(tmp) / "images.zip", folder)
    except Exception as exc:  # noqa: BLE001
        log(f"colab: could not use notebook images ({str(exc)[:200]}); using Cloudflare")
        return 0


def pending_paths(folder: pathlib.Path) -> list[str]:
    """Job images that are not in the episode folder yet."""
    try:
        paths = json.loads((folder / "colab_job.json").read_text(encoding="utf-8")).get("paths") or []
    except (OSError, ValueError):
        return []
    return [p for p in paths if not ((folder / p).exists() and (folder / p).stat().st_size > 1000)]


def _unpack(zip_path: pathlib.Path, folder: pathlib.Path) -> int:
    root = folder.resolve()
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            rel = pathlib.PurePosixPath(name)
            if rel.is_absolute() or ".." in rel.parts or rel.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            out = (root / rel.with_suffix(".png")).resolve()
            if root not in out.parents or (out.exists() and out.stat().st_size > 1000):
                continue  # outside the episode, or already have this image
            try:
                img = Image.open(io.BytesIO(zf.read(name))).convert("RGB")
            except Exception:  # noqa: BLE001 - a broken file just falls back to Cloudflare
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            img.save(out)
            count += 1
    log(f"colab: {count} notebook images ready")
    return count


def cleanup(slug: str) -> None:
    """After delivery: remove the job and the notebook's release so nothing is drawn twice."""
    if MOCK or not _cfg().get("enabled"):
        return
    try:
        subprocess.run(["gh", "release", "delete-asset", JOBS_TAG, f"{slug}.json", "--repo", github.repo(), "-y"],
                       capture_output=True)
        subprocess.run(["gh", "release", "delete", f"colab-{slug}", "--repo", github.repo(), "-y", "--cleanup-tag"],
                       capture_output=True)
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail a delivered episode
        log(f"colab: cleanup skipped ({str(exc)[:120]})")
