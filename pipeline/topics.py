"""Weekly topic proposals."""
from __future__ import annotations

import hashlib

from .config import CFG, MOCK
from .llm import generate_json

CHANNEL_BRIEF = """Channel: "The Long Ago" - calm, long-form English documentaries about the
ORDINARY daily life of people in the past (peasants, bakers, sailors, children,
servants, scribes, merchants, monks, miners...). Tone: warm, curious, immersive,
historically careful. Viewers are adults who watch to learn and often to fall asleep.
Good titles look like: "What Life Was Like for a Peasant Family in 1300s England",
"A Day in the Life of a Roman Baker", "How Ordinary People Survived Winter in the Middle Ages"."""


def topic_id(title: str) -> str:
    return hashlib.sha1(title.lower().encode()).hexdigest()[:8]


def propose(past_titles: list[str]) -> list[dict]:
    n = int(CFG["script"]["topics_per_week"])
    if MOCK:
        return [{"title": f"Mock Topic {i}: Life of a Medieval Miller", "pitch": "A test topic.",
                 "id": topic_id(f"mock{i}")} for i in range(n)]
    past = "\n".join(f"- {t}" for t in past_titles[-80:]) or "(none yet)"
    prompt = f"""{CHANNEL_BRIEF}

Propose {n} NEW episode topics. Rules:
- Each is about everyday life of ordinary people in ONE specific place and period.
- Mix eras and regions across the list (e.g. ancient, medieval, early modern, 19th century;
  Europe, Middle East, Asia, Africa, the Americas). At most 2 from Europe.
- Must be well documented by historians/archaeology so the script can be accurate.
- No living people, no graphic violence focus, no modern politics.
- Do not repeat or closely resemble these past topics:
{past}

Return JSON: {{"topics": [{{"title": "YouTube-ready title, max 80 chars",
"pitch": "one sentence on why viewers will click and what they will learn"}}]}}"""

    def validate(obj):
        if len(obj.get("topics", [])) < n - 1:
            raise ValueError("too few topics")

    data = generate_json(prompt, validate=validate, temperature=1.0)
    topics = []
    for t in data["topics"][:n]:
        title = str(t["title"]).strip()[:90]
        topics.append({"title": title, "pitch": str(t.get("pitch", "")).strip(), "id": topic_id(title)})
    return topics
