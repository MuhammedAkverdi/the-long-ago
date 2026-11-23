"""Thumbnail = generated image + bold text with outline (1280x720 JPEG < 2 MB)."""
from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from . import images
from .config import CFG

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
]


def _font(size: int):
    for path in FONT_CANDIDATES:
        if pathlib.Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def overlay(base: Image.Image, text: str, out: pathlib.Path, darken: float = 1.0) -> pathlib.Path:
    img = base.convert("RGB").resize((1280, 720), Image.LANCZOS)
    if darken != 1.0:
        img = ImageEnhance.Brightness(img).enhance(darken)
    # left-side gradient for legibility
    shade = Image.new("L", (1280, 720), 0)
    sd = ImageDraw.Draw(shade)
    for x in range(700):
        sd.line([(x, 0), (x, 720)], fill=int(170 * (1 - x / 700)))
    img = Image.composite(Image.new("RGB", img.size, (0, 0, 0)), img, shade)
    draw = ImageDraw.Draw(img)
    text = text.upper()
    size = 120
    while size > 50:
        font = _font(size)
        lines = _wrap(draw, text, font, 620)
        if len(lines) <= 3:
            break
        size -= 8
    y = 360 - len(lines) * size * 0.6
    for line in lines:
        draw.text((50, y), line, font=font, fill=(255, 236, 190), stroke_width=max(4, size // 14),
                  stroke_fill=(20, 12, 4))
        y += size * 1.15
    img.save(out, "JPEG", quality=90, optimize=True)
    return out


def make(prompt: str, text: str, folder: pathlib.Path) -> pathlib.Path:
    raw = images.generate(prompt + ". Single clear focal subject on the right half, empty darker space on the left.",
                          folder / "thumb_raw.png", providers=CFG["images"].get("thumbnail_providers"))
    if raw is None:
        raise RuntimeError("thumbnail image generation failed")
    return overlay(Image.open(raw), text, folder / "thumbnail.jpg")
