"""Real period pictures (paintings, engravings, maps, manuscripts) from Wikimedia Commons.

Only files that are public domain, CC0 or plain CC BY (credited in the description; never
share-alike or non-commercial) are used, and each one is shown to the vision
model first, so an unrelated or modern picture is skipped. Every step is optional: on any error
the episode simply has no archival pictures.
"""
from __future__ import annotations

import io
import json
import pathlib
import re

import requests
from PIL import Image

from .config import CFG, MOCK, log

API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia asks automated clients for a descriptive User-Agent with a way to reach the operator.
HEADERS = {"User-Agent": "TheLongAgoBot/1.0 (https://github.com/muhammedakverdi/the-long-ago)"}
_OK_LICENCE = re.compile(r"public domain|^pd-old|^pd-art|cc0|^cc by \d", re.I)
_BAD_LICENCE = re.compile(r"\bsa\b|share|\bnc\b|\bnd\b|fair use", re.I)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def licence_ok(meta: dict) -> bool:
    lic = _strip_html((meta.get("LicenseShortName") or {}).get("value", ""))
    restrictions = _strip_html((meta.get("Restrictions") or {}).get("value", ""))
    return bool(_OK_LICENCE.search(lic)) and not _BAD_LICENCE.search(lic) and not restrictions


def search(query: str, limit: int = 8) -> list[dict]:
    """Public-domain bitmap files for `query`, best first: [{title, url, page, author, licence}]."""
    r = requests.get(API, headers=HEADERS, timeout=30, params={
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": limit,
        "prop": "imageinfo", "iiprop": "url|extmetadata|size|mime", "iiurlwidth": 1920})
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or {}
    out = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 99)):
        info = (p.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        if not licence_ok(meta) or info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        if min(info.get("width", 0), info.get("height", 0)) < 600:
            continue
        out.append({"title": p.get("title", ""), "url": info.get("thumburl") or info.get("url"),
                    "page": info.get("descriptionurl", ""),
                    "author": _strip_html((meta.get("Artist") or {}).get("value", ""))[:80] or "unknown",
                    "licence": _strip_html((meta.get("LicenseShortName") or {}).get("value", ""))})
    return out


def _relevant(img_bytes: bytes, query: str, context: str) -> bool:
    from google.genai import types
    from .llm import generate_json_parts
    verdict = generate_json_parts([
        types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"),
        f"""This picture is a candidate illustration for a history documentary passage about:
"{context[:400]}" (search: "{query}"). Accept it only if it is a period artwork, engraving, map,
manuscript page or historical photograph that clearly fits this passage, is mostly picture (not a
page of text), not gory, and not a modern photo of a museum or building.
Return JSON {{"ok": true/false, "why": "short"}}"""], role="qc")
    return bool(verdict.get("ok", False))


def fit_16x9(img: Image.Image, w: int = 1920, h: int = 1080) -> Image.Image:
    """Portrait paintings and tall maps sit whole on a blurred copy of themselves instead of being cropped."""
    from PIL import ImageFilter
    if abs(img.width / img.height - w / h) < 0.25:
        return img
    bg = img.copy()
    scale = max(w / bg.width, h / bg.height)
    bg = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1)).crop((0, 0, w, h))
    bg = bg.filter(ImageFilter.GaussianBlur(40)).point(lambda v: int(v * 0.55))
    fg = img.copy()
    fg.thumbnail((int(w * 0.92), int(h * 0.92)))
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    return bg


def fetch(requests_list: list[dict], folder: pathlib.Path) -> list[dict]:
    """requests_list: [{"chapter": i, "query": "...", "context": "..."}]. Returns the pictures found,
    [{"chapter", "path", "title", "author", "licence", "page"}]; cached in folder/manifest.json."""
    manifest = folder / "manifest.json"
    if manifest.exists():
        return json.loads(manifest.read_text(encoding="utf-8"))
    found: list[dict] = []
    limit = int(CFG["video"].get("archival_max", 6))
    if MOCK or not requests_list:
        return found
    folder.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    completed = 0  # searches that ran without a network/API error
    for req in requests_list:
        if len(found) >= limit:
            break
        try:
            candidates = search(str(req["query"]))[:4]
            completed += 1
        except Exception as exc:  # noqa: BLE001 - archival pictures are a bonus, never fatal
            log(f"archival search '{req.get('query')}' skipped: {str(exc)[:150]}")
            continue
        for cand in candidates:
            if cand["title"] in used or not cand["url"]:
                continue
            try:
                r = requests.get(cand["url"], headers=HEADERS, timeout=60)
                r.raise_for_status()
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=90)
                if not _relevant(buf.getvalue(), str(req["query"]), str(req.get("context", ""))):
                    continue
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                log(f"archival candidate {cand['title'][:60]} skipped: {str(exc)[:120]}")
                continue
            path = folder / f"{len(found):02d}.png"
            fit_16x9(img).save(path)
            used.add(cand["title"])
            found.append({"chapter": int(req["chapter"]), "path": path.name, **{k: cand[k] for k in
                          ("title", "author", "licence", "page")}})
            log(f"archival picture: {cand['title']} ({cand['licence']})")
            break
    if not completed:  # Commons unreachable this run: try again when the episode resumes
        return found
    manifest.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    return found


def credits(found: list[dict]) -> list[str]:
    return [f"- {f['title'].removeprefix('File:')} ({f['author']}, {f['licence']}): {f['page']}" for f in found]
