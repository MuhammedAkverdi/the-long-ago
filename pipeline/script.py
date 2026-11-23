"""Research -> outline -> chapters -> fact check -> metadata."""
from __future__ import annotations

import json
import random
import re

from .config import CFG, MOCK, log
from .llm import generate_json, generate_text, research
from .topics import CHANNEL_BRIEF

# Different narrative shapes so episodes are not template copies of each other.
STRUCTURES = {
    "day": "Follow one composite ordinary person through a single day, dawn to night, "
           "stepping out of the story now and then to explain the wider history.",
    "lifetime": "Follow a typical life from birth to old age, one life stage per chapter.",
    "year": "Move through one year season by season: work, food, festivals, hardship.",
    "objects": "Tell the story through everyday objects (a bowl, a coin, a shoe, a lamp...), "
               "one object opening each chapter.",
    "myths": "Each chapter takes a popular belief about this time and place and compares it "
             "with what the evidence actually shows.",
    "journey": "Follow a traveller arriving in the place and moving through its spaces: "
               "the road, the market, a home, a workplace, a temple, an inn.",
    "questions": "Each chapter answers a question a modern viewer would ask "
                 "(What did they eat? How did they wash? What did they do for fun?).",
}

SYSTEM = (CHANNEL_BRIEF + "\nYou write narration that is read aloud by a single calm narrator. "
          "Write in plain, vivid spoken English with varied sentence length. Never use headings, "
          "bullet points, stage directions, emojis or markdown inside narration. Never invent "
          "quotations, statistics or named sources. When evidence is uncertain, say so naturally.")


def pick_structure(recent: list[str]) -> str:
    options = [k for k in STRUCTURES if k not in recent[-3:]] or list(STRUCTURES)
    return random.choice(options)


def _mock_script(topic: str, structure: str) -> dict:
    chapters = []
    for i in range(3):
        chapters.append({
            "heading": f"Chapter {i + 1}",
            "narration": " ".join([f"This is mock sentence {j} of chapter {i + 1}, about {topic}." for j in range(12)]),
            "image_prompts": [f"mock scene {i}-{k}" for k in range(3)],
        })
    return {
        "topic": topic, "structure": structure, "title": f"Mock: {topic}"[:95],
        "description": "A mock description.\n\nSecond paragraph.", "tags": ["history", "mock"],
        "thumbnail_text": "MOCK LIFE", "thumbnail_prompt": "a mock thumbnail scene",
        "chapters": chapters,
        "shorts": [{"title": "Mock short #shorts", "narration": "This is a short mock narration for testing vertical video. " * 3,
                    "image_prompts": ["mock vertical a", "mock vertical b", "mock vertical c"]}],
        "sources": [{"title": "Example source", "url": "https://example.org"}],
        "fact_check": "mock",
    }


def build(topic: str, structure: str) -> dict:
    if MOCK:
        return _mock_script(topic, structure)
    sc = CFG["script"]
    total_words = int(sc["target_minutes"] * sc["words_per_minute"])
    lo, hi = sc["chapters"]
    words_per_image = max(20, int(sc["words_per_minute"] * CFG["video"]["seconds_per_image"] / 60))

    log(f"researching: {topic}")
    notes, sources = research(topic)

    log(f"outlining with structure '{structure}'")

    def check_outline(o):
        chs = o.get("chapters", [])
        if not (lo - 1 <= len(chs) <= hi + 1):
            raise ValueError(f"outline has {len(chs)} chapters")
        for c in chs:
            if not c.get("heading") or not c.get("beats"):
                raise ValueError("chapter missing heading/beats")
            if isinstance(c["beats"], str):
                c["beats"] = [c["beats"]]
            c["beats"] = [b if isinstance(b, str) else json.dumps(b) for b in c["beats"]]
            m = re.search(r"\d+", str(c.get("words", "")))
            c["words"] = int(m.group(0)) if m else 0

    outline = generate_json(f"""Episode topic: {topic}
Narrative structure: {STRUCTURES[structure]}
Total narration length: about {total_words} words, split into {lo}-{hi} chapters.
The first chapter must open with a 2-3 sentence hook that makes the viewer curious,
then a gentle line welcoming them to The Long Ago. The last chapter ends with a calm
reflection connecting their world to ours.

Research notes (only use facts supported here or that are widely established):
<<<
{notes}
>>>

Return JSON: {{"chapters": [{{"heading": "short chapter title (for YouTube chapters)",
"beats": ["3-6 specific points this chapter covers"], "words": integer}}]}}""",
                            system=SYSTEM, validate=check_outline)

    chapters = []
    previous_tail = ""
    for i, ch in enumerate(outline["chapters"]):
        words = ch["words"] if 150 <= ch["words"] <= 1500 else total_words // len(outline["chapters"])
        n_images = max(2, round(words / words_per_image))
        log(f"writing chapter {i + 1}/{len(outline['chapters'])}: {ch['heading']} (~{words} words)")

        def check_chapter(o, n_images=n_images, words=words):
            if len(o.get("narration", "").split()) < words * 0.6:
                raise ValueError("chapter too short")
            if len(o.get("image_prompts", [])) < max(2, n_images // 2):
                raise ValueError("too few image prompts")

        out = generate_json(f"""Write chapter {i + 1} of {len(outline['chapters'])} of the episode "{topic}".
Chapter title: {ch['heading']}
Beats to cover: {'; '.join(ch['beats'])}
Length: about {words} words of narration.
Structure of the whole episode: {STRUCTURES[structure]}
Full outline for context: {[c['heading'] for c in outline['chapters']]}
The previous chapter ended with: "{previous_tail}"
Continue smoothly from it without repeating it.

Research notes:
<<<
{notes}
>>>

Also write {n_images} image prompts, in order, one for each consecutive part of the
narration. Each prompt describes a single concrete scene (people, place, objects, light,
time of day) that matches what is being said at that moment. Ordinary anonymous people
only, never a real historical person's face. Period-accurate clothing and architecture.

Return JSON: {{"narration": "...", "image_prompts": ["..."]}}""",
                            system=SYSTEM, validate=check_chapter)
        chapters.append({"heading": ch["heading"].strip(), "narration": out["narration"].strip(),
                         "image_prompts": [str(p) for p in out["image_prompts"]]})
        previous_tail = " ".join(out["narration"].split()[-40:])

    try:
        chapters, n_issues = fact_check(topic, notes, chapters)
        fact_status = f"{n_issues} düzeltme uygulandı"
    except Exception as exc:  # noqa: BLE001 - never lose a whole episode to the checker
        log(f"fact check failed, continuing without it: {exc}")
        fact_status = "ÇALIŞMADI, dikkatle kontrol edin"

    log("writing metadata")
    full_text = "\n\n".join(c["narration"] for c in chapters)

    def check_meta(o):
        for key in ("title", "description", "tags", "thumbnail_text", "thumbnail_prompt", "shorts"):
            if key not in o:
                raise ValueError(f"missing {key}")
        if len(o["title"]) > 100:
            raise ValueError("title too long")

    meta = generate_json(f"""Here is the narration of a documentary episode about "{topic}":
<<<
{full_text[:30000]}
>>>

Create YouTube packaging. Return JSON with:
"title": click-worthy but honest title, max 70 characters, no clickbait lies, no ALL CAPS words;
"description": 2 short paragraphs (max 900 characters total) that summarize the episode for
  search; mention the time period and place; no hashtags, no links, no timestamps;
"tags": 12-15 search tags;
"thumbnail_text": 2-4 word hook for the thumbnail (e.g. "THEY ATE THIS?");
"thumbnail_prompt": one striking, simple scene for the thumbnail image, single focal subject,
  strong contrast, leave empty space on the left for text;
"shorts": {sc['shorts_per_episode']} objects {{"title": "<70 chars, ends with #shorts",
  "narration": "a self-contained 90-120 word story taken from the most surprising facts in the
  episode, starting with a strong first sentence", "image_prompts": [4 vertical scene prompts]}}""",
                         system=SYSTEM, validate=check_meta)

    return {
        "topic": topic, "structure": structure, "title": meta["title"].strip(),
        "description": meta["description"].strip(), "tags": [str(t) for t in meta["tags"]][:20],
        "thumbnail_text": meta["thumbnail_text"].strip(), "thumbnail_prompt": meta["thumbnail_prompt"],
        "chapters": chapters, "shorts": meta["shorts"][: sc["shorts_per_episode"]], "sources": sources,
        "fact_check": fact_status,
    }


def fact_check(topic: str, notes: str, chapters: list[dict]) -> tuple[list[dict], int]:
    """One review pass: find doubtful claims and rewrite only the affected chapters."""
    numbered = "\n\n".join(f"[CHAPTER {i}]\n{c['narration']}" for i, c in enumerate(chapters))
    review = generate_json(f"""You are a strict history fact-checker. Topic: {topic}.
Compare the script with the research notes and general scholarly consensus. List only
real problems: factual errors, anachronisms, invented numbers or quotes, overconfident
claims about debated topics, or anything that names a real living person.

Research notes:
<<<
{notes}
>>>

Script:
<<<
{numbered}
>>>

Return JSON: {{"issues": [{{"chapter": int, "problem": "...", "fix": "..."}}]}}  (empty list if fine)""",
                           temperature=0.2)
    issues = review if isinstance(review, list) else (review.get("issues") or [])
    log(f"fact check: {len(issues)} issue(s)")
    by_chapter: dict[int, list[dict]] = {}
    for issue in issues:
        try:
            idx = int(issue["chapter"]) if isinstance(issue, dict) else -1
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(issue, dict) and 0 <= idx < len(chapters):
            by_chapter.setdefault(idx, []).append(issue)
    for idx, items in by_chapter.items():
        fixes = "\n".join(f"- {it.get('problem', '')} -> {it.get('fix', '')}" for it in items)
        original = chapters[idx]["narration"]
        revised = generate_text(f"""Revise this documentary narration to fix these problems, changing as
little as possible and keeping the same length, tone and flow. Return only the revised narration text.

Problems:
{fixes}

Narration:
<<<
{original}
>>>""", role="light", system=SYSTEM, temperature=0.4)
        if len(revised.split()) >= 0.8 * len(original.split()):
            chapters[idx]["narration"] = revised.strip()
    return chapters, len(issues)
