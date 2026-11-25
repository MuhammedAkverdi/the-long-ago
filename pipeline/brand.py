"""The channel's one design language: palette, fonts and the plain word cards shown between pictures."""
from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageFont

from .config import CFG, ROOT

_FALLBACK_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"]


def _cfg() -> dict:
    return CFG.get("brand") or {}


def color(name: str, default: str = "#FFFFFF") -> str:
    return str(_cfg().get(name, default))


def rgb(name: str, default: str = "#FFFFFF") -> tuple[int, int, int]:
    h = color(name, default).lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def ffcolor(name: str, default: str = "#FFFFFF") -> str:
    """Colour as ffmpeg wants it (0xRRGGBB)."""
    return "0x" + color(name, default).lstrip("#")


def font_path(kind: str = "title") -> str | None:
    """Path of the brand font ('title' or 'hand'); falls back to a system font, or None."""
    rel = _cfg().get(f"{kind}_font")
    if rel:
        p = ROOT / rel
        if p.exists():
            return str(p)
    for f in _FALLBACK_FONTS:
        if pathlib.Path(f).exists():
            return f
    return None


def font(kind: str, size: int):
    path = font_path(kind)
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


def word_card(text: str, out: pathlib.Path, w: int = 1920, h: int = 1080) -> pathlib.Path:
    """A plain paper card with one short handwritten phrase (costs no image quota)."""
    text = " ".join(str(text).split())[:40]
    img = Image.new("RGB", (w, h), rgb("paper", "#F3EADB"))
    draw = ImageDraw.Draw(img)
    size = int(h * 0.16)
    while size > 40:
        f = font("hand", size)
        if draw.textlength(text, font=f) <= w * 0.8:
            break
        size -= 6
    f = font("hand", size)
    tw = draw.textlength(text, font=f)
    draw.text(((w - tw) / 2, h / 2 - size * 0.6), text, font=f, fill=rgb("ink", "#1C1A17"))
    # a short hand-drawn underline in the accent colour
    y = h / 2 + size * 0.62
    draw.line([((w - tw) / 2 + tw * 0.1, y), ((w + tw) / 2 - tw * 0.1, y + 4)], fill=rgb("accent", "#E9A53C"),
              width=max(6, size // 14))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
