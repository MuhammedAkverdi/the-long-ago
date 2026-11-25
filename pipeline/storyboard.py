"""Motion format: split narration into short beats and give every beat an animated scene.

A beat is one or two spoken sentences (~3-5 s). Each beat gets one scene from a small fixed
library (see SCENES); pipeline/motion.py draws them with HyperFrames. Only "picture" beats
spend image quota; everything else (maps, dates, counters, quotes, stamps, the recurring
cartoon narrator...) is drawn in code.
"""
from __future__ import annotations

import json
import re

from .config import CFG, MOCK, log
from .llm import LLMUnavailable, generate_json

SCENES = {
    "picture": "an illustration of what is being said (uses the next prepared picture). Optional "
               "\"label\": 1-3 words shown on it",
    "kinetic": "big animated words on screen. \"lines\": 1-3 short lines (max 18 characters each) taken "
               "from the beat's own words; \"accent\": index of the line to colour",
    "date": "a calendar / year card. \"year\": e.g. \"1900\" or \"1200 BC\"; optional \"month\" and \"day\"",
    "map": "zoom into a REAL place on a world map. \"place\": its name, \"lat\", \"lon\": its coordinates; "
           "optional \"from_place\", \"from_lat\", \"from_lon\" for a journey line; \"zoom\": "
           "\"local\" | \"region\" | \"country\" | \"continent\"",
    "counter": "a number counting up. \"number\": digits only (e.g. 12000), optional \"prefix\" (e.g. "
               "\"$\") and \"suffix\" (e.g. \"%\"), \"label\": 1-4 words saying what it counts",
    "list": "2-4 items appearing one by one. \"title\" (max 22 chars), \"items\": short phrases",
    "quote": "handwritten lines on an old page (a letter, record, logbook, inscription). \"lines\": "
             "1-3 short lines, \"source\": what the document is",
    "stamp": "a statement with a rubber stamp on it. \"text\": short statement (max 40 chars), \"stamp\": "
             "1 word like MYTH, TRUE, BANNED, ILLEGAL, FALSE, RARE",
    "compare": "split screen. \"left_label\", \"left\", \"right_label\", \"right\" (e.g. THEN / NOW)",
    "character": "the channel's recurring cartoon narrator figure reacting. \"pose\": think | shock | "
                 "point | walk | cold | happy | tired; \"bubble\": max 26 chars it says or thinks",
    "question": "one big question on a dark screen. \"text\": max 40 chars",
}
POSES = {"think", "shock", "point", "walk", "cold", "happy", "tired"}
ZOOMS = {"local", "region", "country", "continent"}
_SENT = re.compile(r"(?<=[.!?…])[\"”’)]?\s+")


def split_beats(text: str, min_words: int = 7, max_words: int = 20) -> list[str]:
    """Sentences, short ones merged and long ones cut at a comma/semicolon, ~3-6 spoken seconds each."""
    sentences = [s.strip() for s in _SENT.split(" ".join(text.split())) if s.strip()]
    pieces: list[str] = []
    for s in sentences:
        words = s.split()
        while len(words) > max_words:
            cut = None
            for k in range(min(len(words) - min_words, max_words), min_words - 1, -1):
                if words[k - 1].endswith((",", ";", ":", "—", "–")):
                    cut = k
                    break
            cut = cut or max_words
            pieces.append(" ".join(words[:cut]))
            words = words[cut:]
        if words:
            pieces.append(" ".join(words))
    beats: list[str] = []
    for p in pieces:
        if beats and (len(beats[-1].split()) < min_words or len(p.split()) < 3) \
                and len(beats[-1].split()) + len(p.split()) <= max_words + 4:
            beats[-1] = f"{beats[-1]} {p}"
        else:
            beats.append(p)
    return beats or [text.strip()]


def _s(v, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clean_scene(raw: dict, beat: str) -> dict | None:
    """Keep only known scenes with usable, short parameters. None = unusable."""
    if not isinstance(raw, dict):
        return None
    t = str(raw.get("t") or raw.get("type") or "").strip().lower()
    if t not in SCENES:
        return None
    if t == "picture":
        return {"t": t, "label": _s(raw.get("label"), 24)}
    if t == "kinetic":
        lines = [_s(x, 22) for x in (raw.get("lines") or []) if _s(x, 22)][:3]
        if not lines:
            return None
        acc = raw.get("accent")
        acc = int(acc) if isinstance(acc, (int, float)) and 0 <= int(acc) < len(lines) else len(lines) - 1
        return {"t": t, "lines": lines, "accent": acc}
    if t == "date":
        year = _s(raw.get("year"), 12)
        if not year:
            return None
        return {"t": t, "year": year, "month": _s(raw.get("month"), 12), "day": _s(raw.get("day"), 3)}
    if t == "map":
        lat, lon = _num(raw.get("lat")), _num(raw.get("lon"))
        if lat is None or lon is None or not (-80 <= lat <= 84 and -180 <= lon <= 180):
            return None
        out = {"t": t, "place": _s(raw.get("place"), 28) or "HERE", "lat": lat, "lon": lon,
               "zoom": raw.get("zoom") if raw.get("zoom") in ZOOMS else "region"}
        flat, flon = _num(raw.get("from_lat")), _num(raw.get("from_lon"))
        if flat is not None and flon is not None and -80 <= flat <= 84 and -180 <= flon <= 180:
            out.update(from_place=_s(raw.get("from_place"), 28), from_lat=flat, from_lon=flon)
        return out
    if t == "counter":
        digits = re.sub(r"[^\d.]", "", str(raw.get("number", ""))).rstrip(".")
        if not re.fullmatch(r"\d+(\.\d+)?", digits):
            return None
        # a number must really be in the beat, so the screen never shows an invented figure
        plain = beat.replace(",", "")
        if digits.split(".")[0] not in plain and not _spelled_ok(digits, beat):
            return None
        return {"t": t, "number": digits, "prefix": _s(raw.get("prefix"), 3), "suffix": _s(raw.get("suffix"), 4),
                "label": _s(raw.get("label"), 30)}
    if t == "list":
        items = [_s(x, 30) for x in (raw.get("items") or []) if _s(x, 30)][:4]
        if len(items) < 2:
            return None
        return {"t": t, "title": _s(raw.get("title"), 24), "items": items}
    if t == "quote":
        lines = [_s(x, 34) for x in (raw.get("lines") or []) if _s(x, 34)][:3]
        if not lines:
            return None
        return {"t": t, "lines": lines, "source": _s(raw.get("source"), 30)}
    if t == "stamp":
        text, stamp = _s(raw.get("text"), 44), _s(raw.get("stamp"), 10).upper()
        if not text or not stamp:
            return None
        return {"t": t, "text": text, "stamp": stamp}
    if t == "compare":
        vals = {k: _s(raw.get(k), 26) for k in ("left_label", "left", "right_label", "right")}
        if not (vals["left"] and vals["right"]):
            return None
        return {"t": t, **vals}
    if t == "character":
        pose = str(raw.get("pose", "think")).lower()
        return {"t": t, "pose": pose if pose in POSES else "think", "bubble": _s(raw.get("bubble"), 28)}
    if t == "question":
        text = _s(raw.get("text"), 44)
        return {"t": t, "text": text} if text else None
    return None


_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
            "ten": 10, "eleven": 11, "twelve": 12, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
            "hundred": 100, "thousand": 1000}


def _spelled_ok(digits: str, beat: str) -> bool:
    """Accept small spelled-out numbers ("twelve hours" -> 12)."""
    try:
        n = int(float(digits))
    except ValueError:
        return False
    return any(w.strip(".,;:!?").lower() in _WORDNUM and _WORDNUM[w.strip(".,;:!?").lower()] == n
               for w in beat.split())


def fallback_scene(beat: str, prev: str | None) -> dict:
    """When the planner gave nothing usable: alternate pictures with big words from the beat."""
    if prev != "picture":
        return {"t": "picture", "label": ""}
    words = [w.strip(".,;:!?\"'()") for w in beat.split()]
    key = [w for w in words if len(w) > 3][:3] or words[:2]
    return {"t": "kinetic", "lines": [" ".join(key).upper()[:22]], "accent": 0}


def plan_chapter(topic: str, heading: str, beats: list[str], picture_share: float) -> list[dict]:
    if MOCK:
        cycle = ["picture", "kinetic", "date", "map", "counter", "character", "quote", "stamp", "list",
                 "compare", "question"]
        demo = {"kinetic": {"lines": ["MOCK", "WORDS"], "accent": 1}, "date": {"year": "1900", "month": "December", "day": "26"},
                "map": {"place": "Flannan Isles", "lat": 58.29, "lon": -7.59, "zoom": "region"},
                "counter": {"number": "3", "label": "keepers"}, "character": {"pose": "shock", "bubble": "Where is everyone?"},
                "quote": {"lines": ["Storm ended.", "Sea calm."], "source": "logbook"},
                "stamp": {"text": "The logbook story", "stamp": "MYTH"},
                "list": {"title": "Duties", "items": ["Trim wicks", "Polish lens", "Write log"]},
                "compare": {"left_label": "THEN", "left": "Oil lamp", "right_label": "NOW", "right": "LED"},
                "question": {"text": "What reached 100 feet up?"}, "picture": {"label": ""}}
        out = []
        for i, b in enumerate(beats):
            t = cycle[i % len(cycle)]
            raw = {"t": t, **demo[t]}
            if t == "counter":
                raw["number"] = "3"
                b = b + " 3"
            out.append(clean_scene(raw, b) or fallback_scene(b, out[-1]["t"] if out else None))
        return out
    listing = "\n".join(f"[{i}] {b}" for i, b in enumerate(beats))
    catalogue = "\n".join(f"- {k}: {v}" for k, v in SCENES.items())
    want = max(1, round(len(beats) * picture_share))

    def check(o):
        if not isinstance(o, dict) or len(o.get("scenes") or []) < len(beats) * 0.7:
            raise ValueError("too few scenes")

    try:
        out = generate_json(f"""You are the motion-graphics editor of a fast, modern history explainer channel
(think of the most-watched faceless YouTube explainers: a new visual every 3-5 seconds, maps, dates,
numbers, big words, a recurring cartoon narrator). Episode: "{topic}". Chapter: "{heading}".

The narration of this chapter, split into numbered beats (each beat is on screen while it is spoken):
{listing}

Scene types you may use:
{catalogue}

Rules:
- Give EVERY beat exactly one scene, in order.
- Use "picture" for about {want} of the {len(beats)} beats, spread out, and mainly where the beat
  describes a scene, a person, an action or a place up close.
- Pick the scene that makes THIS beat easiest to understand at a glance: a date for a date, a map for a
  journey or a place, a counter for a number, a stamp for a myth or a rule, a question for a question.
- Never the same type twice in a row (except picture). Use at least 5 different types in the chapter.
- All text on screen must come from the beat itself or be a plain fact stated in it. Never add new facts,
  names or numbers. Coordinates must be the real location of a real, named place.
- Short, punchy screen text, ALL CAPS is fine. Use "character" for "you" moments and reactions.

Return JSON {{"scenes": [{{"beat": int, "t": "type", ...parameters}}]}}""",
                            role="light", temperature=0.5, validate=check)
        items = [s for s in out.get("scenes", []) if isinstance(s, dict)]
        nums = [int(s["beat"]) for s in items if str(s.get("beat", "")).isdigit()]
        if len(nums) == len(items) and len(set(nums)) == len(nums):
            if 0 not in nums and max(nums, default=0) == len(beats):
                nums = [n - 1 for n in nums]  # the planner counted beats from 1
            raw = dict(zip(nums, items))
        else:  # unusable numbering: trust the order
            raw = dict(enumerate(items))
    except LLMUnavailable:
        raise  # every model is down: pause the episode and plan again tomorrow (never cache a degraded plan)
    except Exception as exc:  # noqa: BLE001 - a failed plan falls back to pictures + big words
        log(f"storyboard plan failed for '{heading}', using simple scenes: {str(exc)[:150]}")
        raw = {}
    scenes: list[dict] = []
    for i, b in enumerate(beats):
        sc = clean_scene(raw.get(i), b)
        prev = scenes[-1] if scenes else None
        if sc and prev and sc["t"] == prev["t"] and sc["t"] != "picture":
            sc = None  # two identical motion scenes in a row look like a stuck screen
        scenes.append(sc or fallback_scene(b, prev["t"] if prev else None))
    return scenes


def build(script: dict) -> list[list[dict]]:
    """Per chapter: [{"text": beat, "scene": {...}}]."""
    mc = CFG.get("motion") or {}
    share = float(mc.get("picture_share", 0.33))
    out = []
    for c in script["chapters"]:
        beats = split_beats(c["narration"], int(mc.get("beat_min_words", 7)), int(mc.get("beat_max_words", 20)))
        scenes = plan_chapter(script.get("topic", ""), c.get("heading", ""), beats, share)
        out.append([{"text": b, "scene": s} for b, s in zip(beats, scenes)])
        log(f"storyboard: '{c.get('heading', '')}' {len(beats)} beats, "
            f"{sum(1 for s in scenes if s['t'] == 'picture')} pictures")
    return out


def picture_prompts(chapter: dict, beats: list[dict]) -> list[str]:
    """One prompt per picture beat, taken in narration order from the chapter's own prompts."""
    prompts = [str(p) for p in chapter.get("image_prompts") or []] or [chapter.get("heading", "a scene")]
    idx = [i for i, b in enumerate(beats) if b["scene"]["t"] == "picture"]
    n = len(beats)
    return [prompts[min(len(prompts) - 1, int(i / max(1, n) * len(prompts)))] for i in idx]


if __name__ == "__main__":  # quick manual check: python -m pipeline.storyboard "some text"
    import sys
    print(json.dumps(split_beats(" ".join(sys.argv[1:])), indent=1))
