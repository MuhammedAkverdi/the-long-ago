"""Research -> outline -> chapters -> fact check -> metadata."""
from __future__ import annotations

import json
import pathlib
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

STORY_RULES = """Storytelling rules (the episode must feel like a story, not an encyclopedia entry):
- Follow one REAL named person or family from the record when the research notes name one
  (for example a worker, a craftsman, a widow known from court records, letters or receipts).
  Use only what the sources say about them; when you must imagine a detail, say so ("perhaps").
  If no one is named in the sources, follow a clearly composite character and say it is composite.
- Every chapter has a conflict, risk or open question: money running short, a dispute, a law,
  a dangerous job, a secret. End each chapter with a tension that the next chapter picks up.
- Include short second-person passages ("You wake before dawn. Your hands still smell of...")
  that put the viewer inside the scene.
- Use concrete numbers wherever the sources give them: wages, prices, hours, distances, ages.
- Vary rhythm: short punchy sentences at key moments, longer ones for description."""

OPENING_FORMULA = """Opening formula (the first ~30 seconds, used by the most-watched history explainers):
1. Start inside the viewer's own day, in second person ("This morning an alarm screamed at you...",
   "Tonight you'll flip a switch...").
2. Hard cut to the past with a concrete time jump ("Now rewind 600 years.") and one vivid,
   specific person or scene from the research.
3. Name the common belief about it ("Everyone assumes...").
4. Hint that real evidence overturns it, naming the kind of evidence (skeletons, court records,
   wage books, letters...), without giving the answer away.
5. End on the open question the episode will answer.
Short spoken sentences. No greeting, no channel name, no "in this video"."""

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
            "cards": [{"after_image": 0, "text": f"MOCK CARD {i}"}], "reveal": 0.5,
        })
    return {
        "topic": topic, "structure": structure, "title": f"Mock: {topic}"[:95],
        "description": "A mock description.\n\nSecond paragraph.", "tags": ["history", "mock"],
        "thumbnail_text": "MOCK LIFE", "thumbnail_prompt": "a mock thumbnail scene",
        "chapters": chapters,
        "shorts": [{"title": "Mock short #shorts", "narration": "This is a short mock narration for testing vertical video. " * 3,
                    "image_prompts": ["mock vertical a", "mock vertical b", "mock vertical c"]}],
        "sources": [{"title": "Example source", "url": "https://example.org"}],
        "fact_check": "mock", "visual_style": "doodle",
        "hook_options": [{"text": "Mock hook.", "score": 8, "why": "mock"}], "archival_requests": [],
    }


def _step(cache, name: str, fn):
    """Run one script step once: its result is saved, so a resumed run skips finished steps."""
    path = cache / f"{name}.json" if cache else None
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    value = fn()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # atomic: a job killed mid-write never leaves a broken step file
    return value


def _style_rules(style: str) -> str:
    if style == "doodle":
        return ("Pictures are simple doodle cartoons: describe ONE clear action per picture with few objects, "
                "and draw the episode's main character the same way every time using this exact look: ")
    return ""


def pick_hook(topic: str, notes: str, outline: dict, n: int) -> tuple[str, list[dict]]:
    """Write n candidate openings, let a separate model judge them, return (best, top-3 with scores)."""
    def check_hooks(o):
        if len([h for h in o.get("hooks", []) if str(h).strip()]) < max(2, n // 2):
            raise ValueError("too few hooks")

    cands = generate_json(f"""Episode: "{topic}". Protagonist: {outline.get('protagonist', 'composite')}.
Most dramatic real moment from the research: {outline.get('opening_moment', '')}
{OPENING_FORMULA}

Research notes (use only facts supported here):
<<<
{notes[:12000]}
>>>

Write {n} DIFFERENT openings (60-90 spoken words each) that follow the formula. Vary the
modern moment, the past scene and the question. Return JSON {{"hooks": ["...", ...]}}""",
                          system=SYSTEM, temperature=1.0, validate=check_hooks)
    hooks = [str(h).strip() for h in cands["hooks"] if str(h).strip()][:n]
    listing = "\n\n".join(f"[{i}] {h}" for i, h in enumerate(hooks))
    judged = generate_json(f"""You judge YouTube video openings for a history explainer channel. Score each
opening 1-10 on: (a) would a casual viewer keep watching after 10 seconds, (b) a concrete, specific
image or person rather than generalities, (c) an honest open question that the episode can answer,
(d) follows this formula:
{OPENING_FORMULA}
Penalise clickbait that the facts cannot support, invented facts, greetings and vague wording.

Openings:
{listing}

Return JSON {{"scores": [{{"index": int, "score": number, "why": "short"}}]}} for every opening.""",
                           role="light", temperature=0.2)
    scores = {}
    for item in judged.get("scores", []) if isinstance(judged, dict) else []:
        try:
            scores[int(item["index"])] = (float(item["score"]), str(item.get("why", "")))
        except (KeyError, TypeError, ValueError):
            continue
    ranked = sorted(range(len(hooks)), key=lambda i: -scores.get(i, (0, ""))[0])
    top = [{"text": hooks[i], "score": scores.get(i, (0, ""))[0], "why": scores.get(i, (0, ""))[1]} for i in ranked[:3]]
    return hooks[ranked[0]], top


REVEAL = "[REVEAL]"


def take_reveal(text: str) -> tuple[str, float | None]:
    """Remove the [REVEAL] marker; return the clean text and where it was (0-1 of the text)."""
    idx = text.find(REVEAL)
    clean = " ".join(text.replace(REVEAL, " ").split())
    if idx < 0 or not clean:
        return clean, None
    before = " ".join(text[:idx].replace(REVEAL, " ").split())
    return clean, round(len(before) / len(clean), 4)


def build(topic: str, structure: str, cache: pathlib.Path | None = None, style: str = "painterly") -> dict:
    if MOCK:
        return _mock_script(topic, structure)
    if cache and (cache / "structure.json").exists():  # a resumed episode keeps its first structure
        structure = json.loads((cache / "structure.json").read_text(encoding="utf-8"))
    elif cache:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "structure.json").write_text(json.dumps(structure), encoding="utf-8")
    sc = CFG["script"]
    total_words = int(sc["target_minutes"] * sc["words_per_minute"])
    lo, hi = sc["chapters"]
    words_per_image = max(20, int(sc["words_per_minute"] * CFG["video"]["seconds_per_image"] / 60))

    log(f"researching: {topic}")
    notes, sources = _step(cache, "research", lambda: list(research(topic)))

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

    outline = _step(cache, "outline", lambda: generate_json(f"""Episode topic: {topic}
Narrative structure: {STRUCTURES[structure]}
Total narration length: about {total_words} words, split into {lo}-{hi} chapters.
{STORY_RULES}
The episode answers ONE curious question a modern viewer would ask about this subject.
{OPENING_FORMULA}
Only after about 45 seconds comes one short line introducing The Long Ago.
This is not a series recap: never say "welcome back".
The last chapter pays off the opening question, then ends with a short reflection
connecting their world to ours.

Research notes (only use facts supported here or that are widely established):
<<<
{notes}
>>>

Return JSON: {{"protagonist": "the real named person (or attested family) the episode follows, with the
evidence for them, or 'composite' if the sources name nobody", "opening_moment": "the dramatic real moment
used in the first seconds", "question": "the one question the episode answers",
"character_look": "for pictures: how the protagonist looks, in 15-25 words (age, build, hair, the same simple
period clothing every time)", "chapters": [{{"heading": "short chapter title (for YouTube chapters)",
"beats": ["3-6 specific points this chapter covers, with concrete numbers where known"],
"cliffhanger": "the tension or open question that ends this chapter", "words": integer}}]}}""",
                            system=SYSTEM, validate=check_outline))

    n_hooks = int(sc.get("hook_candidates", 0) or 0)
    hook, hook_options = "", []
    if n_hooks >= 2:
        log(f"hook tournament: {n_hooks} candidates")
        try:
            hook, hook_options = _step(cache, "hooks", lambda: list(pick_hook(topic, notes, outline, n_hooks)))
        except Exception as exc:  # noqa: BLE001 - without a winner, chapter 1 writes its own opening
            log(f"hook tournament failed, chapter 1 writes its own opening: {str(exc)[:150]}")
    look = str(outline.get("character_look", "")).strip()
    style_rules = _style_rules(style) + (look if style == "doodle" else "")
    cta = bool(sc.get("comment_cta", True))

    chapters = []
    previous_tail = ""
    n_chapters = len(outline["chapters"])
    for i, ch in enumerate(outline["chapters"]):
        words = ch["words"] if 150 <= ch["words"] <= 1500 else total_words // len(outline["chapters"])
        n_images = max(2, round(words / words_per_image * (1.5 if i == 0 else 1)))  # busier opening
        log(f"writing chapter {i + 1}/{len(outline['chapters'])}: {ch['heading']} (~{words} words)")

        def check_chapter(o, n_images=n_images, words=words):
            if len(o.get("narration", "").split()) < words * 0.6:
                raise ValueError("chapter too short")
            if len(o.get("image_prompts", [])) < max(2, n_images // 2):
                raise ValueError("too few image prompts")

        out = _step(cache, f"chapter{i:02d}", lambda i=i, ch=ch, words=words, n_images=n_images,
                    previous_tail=previous_tail, check_chapter=check_chapter: generate_json(f"""Write chapter {i + 1} of {len(outline['chapters'])} of the episode "{topic}".
Chapter title: {ch['heading']}
Beats to cover: {'; '.join(ch['beats'])}
Length: about {words} words of narration.
Structure of the whole episode: {STRUCTURES[structure]}
Full outline for context: {[c['heading'] for c in outline['chapters']]}
The previous chapter ended with: "{previous_tail}"
Continue smoothly from it without repeating it.
{STORY_RULES}
Protagonist of the episode: {outline.get('protagonist', 'composite')}
{("This is the OPENING chapter. Begin with exactly this opening, word for word, then continue: " + hook) if i == 0 and hook else ("This is the OPENING chapter: start with this moment, no greeting first: " + str(outline.get('opening_moment', ''))) if i == 0 else ""}
{("This is the LAST chapter: after the payoff, end with ONE short, specific question for viewers to answer in the comments about which ordinary job or life in the past we should explore next (no begging for likes or subscriptions).") if cta and i == n_chapters - 1 else "End this chapter on: " + (ch.get('cliffhanger') or 'a tension or open question that pulls into the next chapter')}
Put the marker [REVEAL] (exactly like that) right before the single most surprising sentence of this
chapter, once. It is removed before narration; music pauses there.

Research notes:
<<<
{notes}
>>>

Also write {n_images} image prompts, in order, one for each consecutive part of the
narration. Each prompt describes a single concrete scene (people, place, objects, light,
time of day) that matches what is being said at that moment. Ordinary anonymous people
only, never a real historical person's face. Period-accurate clothing and architecture.
The image model takes every word literally and knows nothing about the period, so spell out
the right materials and avoid anachronisms: no glass drinking glasses or foaming beer unless
the period had them (say "clay jar", "wooden cup"...), no famous landmarks that are not at this
exact place (e.g. no Giza pyramids in a Theban village), no crowns, royal headdresses or
regalia on ordinary people, no paved modern roads, no modern tools or furniture. Describe
what ordinary clothing, hair and headwear looked like for this class at this time.
{style_rules}

Also give 1-2 "cards": a key word or very short phrase (1-3 words, e.g. "12 HOURS", "PROGRESS?",
"NO SALT") shown alone on a plain card right after image number "after_image" (0-based).

Return JSON: {{"narration": "...", "image_prompts": ["..."], "cards": [{{"after_image": int, "text": "..."}}]}}""",
                            system=SYSTEM, validate=check_chapter))
        cards = []
        for c in out.get("cards") or []:
            try:
                text = " ".join(str(c["text"]).split())[:28]
                if text:
                    cards.append({"after_image": max(0, int(c.get("after_image", 0))), "text": text})
            except (KeyError, TypeError, ValueError):
                continue
        chapters.append({"heading": ch["heading"].strip(), "narration": out["narration"].strip(),
                         "image_prompts": [str(p) for p in out["image_prompts"]], "cards": cards[:2]})
        previous_tail = " ".join(out["narration"].replace(REVEAL, " ").split()[-40:])

    try:
        chapters, n_issues = _step(cache, "factcheck", lambda: list(fact_check(topic, notes, chapters)))
        fact_status = f"{n_issues} düzeltme uygulandı"
    except Exception as exc:  # noqa: BLE001 - never lose a whole episode to the checker
        log(f"fact check failed, continuing without it: {exc}")
        fact_status = "ÇALIŞMADI, dikkatle kontrol edin"

    for c in chapters:
        c["narration"], c["reveal"] = take_reveal(c["narration"])

    archival = []
    if CFG["video"].get("archival_images"):
        try:
            archival = _step(cache, "archival", lambda: archival_requests(topic, chapters))
        except Exception as exc:  # noqa: BLE001 - optional
            log(f"archival picture search plan skipped: {str(exc)[:150]}")

    log("writing metadata")
    full_text = "\n\n".join(c["narration"] for c in chapters)

    def check_meta(o):
        for key in ("title", "description", "tags", "thumbnail_text", "thumbnail_prompt", "shorts"):
            if key not in o:
                raise ValueError(f"missing {key}")
        if len(o["title"]) > 100:
            raise ValueError("title too long")

    meta = _step(cache, "meta", lambda: generate_json(f"""Here is the narration of a documentary episode about "{topic}":
<<<
{full_text[:30000]}
>>>

Create YouTube packaging. Return JSON with:
"title_options": 3 different honest titles, max 70 characters each, no ALL CAPS words. Use the styles
  that win in this niche: a curiosity question ("What Did ... Actually Do All Day?"), second person
  ("Why You Wouldn't Last a Week as a ..."), or stakes ("What Happened When ..."). Put the best one first;
"title": the same text as the first title option;
"description": 2 short paragraphs (max 900 characters total) that summarize the episode for
  search; mention the time period and place; no hashtags, no links, no timestamps;
"tags": 12-15 search tags;
"thumbnail_text": 1-3 words with tension or a twist, not a label of the topic (good:
  "NO JOBS", "2 SLEEPS?", "LEAVE = DEATH", "THEY ATE THIS?"; bad: "THE REAL TOMB BUILDERS");
"thumbnail_prompt": one character showing one strong emotion in one simple situation, single focal
  subject, plain uncluttered background, strong contrast, empty space on the left for text;
"shorts": {sc['shorts_per_episode']} objects {{"title": "<70 chars, ends with #shorts",
  "narration": "a self-contained 90-120 word story about ONE different surprising fact from the
  episode (each Short a different fact and a different sentence pattern), whose first sentence is
  its own hook: a concrete image plus an open question, in second person where it fits",
  "image_prompts": [3 vertical scene prompts]}}""",
                         system=SYSTEM, validate=check_meta))

    return {
        "topic": topic, "structure": structure, "title": meta["title"].strip(),
        "title_options": [str(t).strip() for t in meta.get("title_options", []) if str(t).strip()][:3],
        "description": meta["description"].strip(), "tags": [str(t) for t in meta["tags"]][:20],
        "thumbnail_text": meta["thumbnail_text"].strip(), "thumbnail_prompt": meta["thumbnail_prompt"],
        "chapters": chapters, "shorts": meta["shorts"][: sc["shorts_per_episode"]], "sources": sources,
        "fact_check": fact_status, "visual_style": style, "hook_options": hook_options,
        "archival_requests": archival,
    }


def archival_requests(topic: str, chapters: list[dict]) -> list[dict]:
    """Ask for a few Wikimedia Commons searches for real period pictures (paintings, maps, prints)."""
    listing = "\n".join(f"[{i}] {c['heading']}: {' '.join(c['narration'].split()[:60])}" for i, c in enumerate(chapters))
    n = int(CFG["video"].get("archival_max", 6))
    out = generate_json(f"""A history episode about "{topic}" has these chapters:
{listing}

Suggest up to {n} Wikimedia Commons searches that would find REAL period pictures for them: paintings,
engravings, woodcuts, old maps, manuscript illuminations or historical photographs from that time and
place (not modern photos). Use the words a museum catalogue would use, 2-6 words, e.g.
"Carpaccio Venice canal painting", "medieval peasants harvest illumination", "1850 lighthouse engraving".
Return JSON {{"searches": [{{"chapter": int, "query": "...", "context": "the sentence it illustrates"}}]}}""",
                        role="light", temperature=0.4)
    items = out.get("searches", []) if isinstance(out, dict) else []
    clean = []
    for it in items:
        try:
            ch = int(it["chapter"])
            if 0 <= ch < len(chapters) and str(it.get("query", "")).strip():
                clean.append({"chapter": ch, "query": str(it["query"]).strip()[:80],
                              "context": str(it.get("context", ""))[:300]})
        except (KeyError, TypeError, ValueError):
            continue
    return clean[:n + 2]


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
little as possible and keeping the same length, tone and flow. Keep the marker [REVEAL] where it is
(move it only if its sentence changes). Return only the revised narration text.

Problems:
{fixes}

Narration:
<<<
{original}
>>>""", role="light", system=SYSTEM, temperature=0.4)
        if len(revised.split()) >= 0.8 * len(original.split()):
            chapters[idx]["narration"] = revised.strip()
    return chapters, len(issues)
