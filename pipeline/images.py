"""Scene images. Providers are tried in order (config images.providers):
- cloudflare: FLUX.1 schnell on Workers AI (free daily allowance, ~170 images/day)
- gemini:     Nano Banana family (paid; needs billing or Google AI Pro credits)
"""
from __future__ import annotations

import base64
import io
import pathlib
import random
import textwrap

import requests
from PIL import Image, ImageDraw

from .config import CFG, MOCK, PermanentError, env, log, retry

_disabled: set[str] = set()  # providers out of quota for this run
_daily_quota_hit = False       # Cloudflare said its *daily* free allocation is used up (not a bad token)


class QuotaError(PermanentError):
    pass


class QuotaPause(Exception):
    """Every image provider is out of today's allowance: keep the work and continue on a later run."""


def exhausted() -> bool:
    """True only when the day's free allowance ran out (a wrong token or outage is a real error, not a pause)."""
    return _daily_quota_hit and all(p in _disabled for p in CFG["images"]["providers"])


def _mock(prompt: str, out: pathlib.Path, aspect: str) -> None:
    w, h = (1344, 768) if aspect == "16:9" else (768, 1344)
    rnd = random.Random(prompt)
    img = Image.new("RGB", (w, h), tuple(rnd.randint(40, 200) for _ in range(3)))
    draw = ImageDraw.Draw(img)
    for i in range(0, w, 96):
        draw.line([(i, 0), (i, h)], fill=(255, 255, 255), width=2)
    draw.multiline_text((40, h // 2), "\n".join(textwrap.wrap(prompt, 30)), fill=(255, 255, 255))
    img.save(out)


def _cloudflare(prompt: str, aspect: str) -> bytes:
    w, h = (1344, 768) if aspect == "16:9" else (768, 1344)
    framing = "wide cinematic landscape composition" if aspect == "16:9" else "tall vertical portrait composition"
    text = f"{prompt}. {framing}"[:2000]
    headers = {"Authorization": f"Bearer {env('CLOUDFLARE_API_TOKEN')}"}
    last: Exception | None = None
    for model in CFG["images"]["cloudflare_models"]:
        if model in _disabled:
            continue
        url = f"https://api.cloudflare.com/client/v4/accounts/{env('CLOUDFLARE_ACCOUNT_ID')}/ai/run/{model}"
        if "flux-2" in model:   # FLUX.2 models take multipart/form-data
            fields = {"prompt": (None, text), "width": (None, str(w)), "height": (None, str(h))}
            r = requests.post(url, headers=headers, files=fields, timeout=180)
            if r.status_code == 400:  # size not accepted -> plain prompt (we crop afterwards)
                r = requests.post(url, headers=headers, files={"prompt": (None, text)}, timeout=180)
        else:
            body = {"prompt": text, "steps": int(CFG["images"]["cloudflare_steps"])}
            r = requests.post(url, headers=headers, timeout=120, json=body)
            if r.status_code == 400 and "properties" in r.text:  # schema changed: send the prompt alone
                r = requests.post(url, headers=headers, timeout=120, json={"prompt": text})
        daily = r.status_code >= 400 and ("neuron" in r.text.lower() or '"code":4006' in r.text.replace(" ", ""))
        if r.status_code == 429 and not daily:  # short rate limit: retry() waits and tries again
            raise RuntimeError(f"{model} rate limited: {r.text[:200]}")
        if daily:
            global _daily_quota_hit
            _daily_quota_hit = True
            log(f"{model}: daily allowance used up (HTTP {r.status_code}: {r.text[:300]}), switching model")
            _disabled.add(model)
            last = QuotaError(f"{model} quota")
            continue
        if r.status_code == 400:  # usually this one prompt was filtered: fail only this image
            raise PermanentError(f"{model} rejected this prompt: {r.text[:200]}")
        if r.status_code in (401, 403, 404):
            log(f"{model}: HTTP {r.status_code} {r.text[:200]}; switching model")
            _disabled.add(model)
            last = PermanentError(f"{model} HTTP {r.status_code}")
            continue
        if r.status_code != 200:
            raise RuntimeError(f"Cloudflare HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        b64 = (data.get("result") or {}).get("image") or data.get("image")
        if not b64:
            raise RuntimeError(f"Cloudflare returned no image: {str(data)[:200]}")
        return base64.b64decode(b64)
    raise QuotaError(f"no Cloudflare image model available ({last})")


def _gemini(prompt: str, aspect: str) -> bytes:
    from google.genai import types
    from .llm import client
    last = None
    for model in CFG["models"]["image"]:
        try:
            resp = client().models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(response_modalities=["IMAGE"],
                                                   image_config=types.ImageConfig(aspect_ratio=aspect)))
            for cand in resp.candidates or []:
                for part in (cand.content.parts if cand.content else []) or []:
                    if part.inline_data and part.inline_data.data:
                        return part.inline_data.data
            last = RuntimeError("no image returned (possibly filtered)")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if getattr(exc, "code", None) in (401, 403, 429):  # no billing / out of quota
                raise QuotaError(str(exc)) from exc
    raise last  # type: ignore[misc]


PROVIDERS = {"cloudflare": _cloudflare, "gemini": _gemini}


def quality_ok(path: pathlib.Path, prompt: str) -> bool:
    """Ask a cheap Gemini vision model whether the image has obvious AI defects."""
    if MOCK or not CFG["images"].get("quality_check"):
        return True
    from google.genai import types
    from .llm import generate_json_parts
    try:
        verdict = generate_json_parts([
            types.Part.from_bytes(data=path.read_bytes(), mime_type="image/png"),
            f"""You are the art director of a calm history documentary channel. The image was made for:
"{prompt[:500]}". Reject it only for clear defects: deformed hands/faces/limbs, extra fingers, garbled
text or letters, modern objects, photorealistic look, nudity or gore, or a scene that does not match.
Return JSON {{"ok": true/false, "score": 1-10, "problem": "short"}}"""], role="qc")
        ok = bool(verdict.get("ok", True)) and int(verdict.get("score", 7)) >= 6
        if not ok:
            log(f"QC rejected {path.name}: {verdict.get('problem', '')}")
        return ok
    except Exception as exc:  # noqa: BLE001 - QC must never block production
        log(f"QC skipped: {str(exc)[:120]}")
        return True


def generate(prompt: str, out: pathlib.Path, aspect: str = "16:9", providers: list[str] | None = None) -> pathlib.Path | None:
    """Generate one image; returns None if every provider fails (caller reuses a neighbour)."""
    if out.exists() and out.stat().st_size > 1000:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    if MOCK:
        _mock(prompt, out, aspect)
        return out
    full = (f"{prompt}. Style: {CFG['video']['image_style']}. Anonymous ordinary people only.")
    for name in providers or CFG["images"]["providers"]:
        if name in _disabled:
            continue
        try:
            data = retry(lambda n=name: PROVIDERS[n](full, aspect), tries=3, what=f"image {name}")
            Image.open(io.BytesIO(data)).convert("RGB").save(out)
            return out
        except QuotaError as exc:  # allowance used up / no usable model: stop asking this provider
            log(f"{name}: {str(exc)[:200]}; disabling for this run")
            _disabled.add(name)
        except PermanentError as exc:  # this prompt failed (e.g. filtered); provider stays enabled
            log(f"{name}: {str(exc)[:200]}")
            if name == "gemini" and any(k in str(exc) for k in ("401", "403", "billing", "PERMISSION")):
                _disabled.add(name)
        except Exception as exc:  # noqa: BLE001
            log(f"{name} failed for this image: {str(exc)[:200]}")
    return None


def generate_many(prompts: list[str], folder: pathlib.Path, aspect: str = "16:9",
                  fallback: list[pathlib.Path] | None = None) -> list[pathlib.Path]:
    """Generate images in order; failed slots reuse the previous (or next) good image.

    With `fallback`, a run that cannot make enough new images reuses those instead of failing."""
    results: list[pathlib.Path | None] = []
    redraws_left = int(len(prompts) * CFG["images"].get("max_redraw_ratio", 0.2)) + 1
    for i, p in enumerate(prompts):
        cached, qc = folder / f"{i:03d}.png", folder / f"{i:03d}.qc"
        if exhausted() and not (cached.exists() and cached.stat().st_size > 1000):
            if fallback:
                results.append(None)
                continue
            raise QuotaPause(f"image allowance used up at {folder.name}/{i:03d}")
        path = generate(p, cached, aspect)
        if path is None and exhausted() and not fallback:  # this image hit the limit: retry it on the next run
            raise QuotaPause(f"image allowance used up at {folder.name}/{i:03d}")
        if path and redraws_left > 0 and not qc.exists() and not quality_ok(path, p):
            redraws_left -= 1
            path.rename(path.with_suffix(".rejected.png"))
            path = generate(p + ". Simple clear composition, hands hidden or relaxed, no writing anywhere",
                            folder / f"{i:03d}.png", aspect) or path.with_suffix(".rejected.png")
        if path:
            qc.touch()  # checked once: a resumed run must not spend a redraw on it again
        results.append(path)
        if (i + 1) % 10 == 0:
            log(f"images: {i + 1}/{len(prompts)}")
    good = [r for r in results if r]
    if len(good) < 0.75 * len(results) and fallback:
        log(f"only {len(good)}/{len(results)} new images; reusing episode images")
        return [r or fallback[i % len(fallback)] for i, r in enumerate(results)]
    if len(good) < 0.75 * len(results):  # too many gaps would look cheap: fail now, retry on a later run
        raise RuntimeError(f"only {len(good)}/{len(results)} images could be generated")
    filled, last = [], None
    for r in results:
        last = r or last
        filled.append(last)
    return [f or good[0] for f in filled]
