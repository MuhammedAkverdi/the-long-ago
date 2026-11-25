"""Optional hand-made assets for one episode (e.g. images or a clip made by hand in Google Flow).

Put them in assets/manual/<topic id>/ (the 8-character id from the topics issue):
  open_01.jpg, open_02.jpg, ...  -> the first pictures of chapter 1 (the opening), in name order
  thumb.jpg                      -> the thumbnail's base picture (text is added on top)
  hook.mp4                       -> a video laid over the first seconds of the episode (its sound is not used)
Everything is optional; without the folder the episode is made exactly as before.
"""
from __future__ import annotations

import pathlib

from PIL import Image

from .config import ROOT, log

DIR = ROOT / "assets" / "manual"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def folder_for(slug: str) -> pathlib.Path | None:
    """The manual folder whose name (topic id) appears in the episode slug `<date>-<topic id>-a<n>`."""
    if not DIR.is_dir():
        return None
    parts = slug.split("-")
    for d in sorted(DIR.iterdir()):
        if d.is_dir() and len(parts) > 1 and parts[1] == d.name:
            return d
    return None


def _save_png(src: pathlib.Path, out: pathlib.Path) -> bool:
    try:
        img = Image.open(src).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - a broken manual file is skipped, never fatal
        log(f"manual: cannot read {src.name}: {exc}")
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return True


def apply(folder: pathlib.Path, slug: str, script: dict, openings: bool = True) -> int:
    """Copy manual pictures into the episode folder where the generated ones would go.
    Existing files are kept, so a resumed run does not redo it. Returns how many were placed.
    openings=False: only the thumbnail (the animated format draws its own opening)."""
    src = folder_for(slug)
    if src is None:
        return 0
    placed = 0
    opens = sorted(p for p in src.iterdir() if p.suffix.lower() in IMG_EXT and p.stem.lower().startswith("open"))
    slots = len(script["chapters"][0]["image_prompts"]) if script.get("chapters") else 0
    for k, p in enumerate(opens[:slots] if openings else []):
        out = folder / "img" / "ch00" / f"{k:03d}.png"
        if not out.exists() and _save_png(p, out):
            out.with_suffix(".qc").touch()  # hand-picked: no quality-check redraw
            placed += 1
    thumbs = [p for p in src.iterdir() if p.suffix.lower() in IMG_EXT and p.stem.lower() == "thumb"]
    if thumbs and not (folder / "thumb_raw.png").exists() and _save_png(thumbs[0], folder / "thumb_raw.png"):
        placed += 1
    log(f"manual: {placed} hand-made picture(s) from assets/manual/{src.name}")
    return placed


def hook(slug: str) -> pathlib.Path | None:
    src = folder_for(slug)
    if src is None:
        return None
    clip = src / "hook.mp4"
    return clip if clip.exists() and clip.stat().st_size > 10000 else None
