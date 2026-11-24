"""Check that an AI-suggested reading list only names books/articles that really exist.

Without Google Search grounding (free tier) the reading list comes from the model's memory,
and language models are known to invent plausible-looking citations. Before a title goes into
a public video description, look it up in two free catalogues (Open Library, Crossref) and
keep it only if a record with a matching title and author surname is found.
"""
from __future__ import annotations

import re
import unicodedata

import requests

from .config import MOCK, log

UA = {"User-Agent": "TheLongAgo-pipeline/1.0 (reading-list check)"}
STOP = {"the", "a", "an", "of", "and", "in", "on", "to", "for", "at", "by", "with", "from"}


FOLD = str.maketrans({"ı": "i", "İ": "I", "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE",
                      "ß": "ss", "đ": "d", "Đ": "D", "þ": "th", "œ": "oe", "Œ": "OE"})


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.translate(FOLD)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def _words(text: str) -> set[str]:
    return {w for w in _norm(text).split() if w not in STOP and len(w) > 1}


NAMEISH = re.compile(r"^(?:[A-Z][\w'.-]*|[A-Z]\.|\(eds?\.?\)|Jr\.?|and|&)$")


def parse(entry: str) -> tuple[str, str] | None:
    """'Author Name, Title of Book (1999)' -> ('Author Name', 'Title of Book').
    Also handles 'Surname, Given (ed.), Title', 'Surname, Given. Title' and 'Author - Title'."""
    entry = re.sub(r"[*_\"“”]", "", entry).strip()
    entry = re.sub(r"^\d+[.)]\s*", "", entry)
    entry = re.sub(r"\s*\((?:eds?|editors?)\.?\)", "", entry, flags=re.I)
    entry = re.sub(r"\([^()]*\b\d{3,4}\b[^()]*\)\s*\.?$", "", entry).strip(" .,;")
    dash = re.split(r"\s[-–—]\s", entry, maxsplit=1)
    if len(dash) == 2 and "," not in dash[0]:
        author, title = dash[0].strip(), dash[1].strip(" .")
        return (author, title) if author and title else None
    parts = [x.strip() for x in entry.split(",")]
    if len(parts) < 2:
        return None
    author, rest = parts[0], parts[1:]
    if len(author.split()) == 1:  # surname first: the next part is the given name(s)
        given = rest[0]
        m = None if re.search(r"\.\s+(?:and|&)\s", given) else re.match(r"^((?:[A-Z][\w'-]*\.?\s?){1,3}(?:\(eds?\.?\))?)\.\s+(.+)$", given)
        if m:  # 'Mortimer, Ian. The Time Traveller's Guide'
            author, rest = f"{author}, {m.group(1).strip()}", [m.group(2)] + rest[1:]
        elif len(rest) > 1 and re.search(r"\s(?:and|&)\s", given):  # 'Bowman, Alan K. and Greg Woolf, Title'
            author, rest = f"{author}, {given}", rest[1:]
        elif len(rest) > 1 and len(given.split()) <= 3 and all(NAMEISH.match(w) for w in given.split()):
            author, rest = f"{author}, {given}", rest[1:]
    title = ", ".join(rest).strip(" .")
    if not title:
        return None
    return re.sub(r"\(eds?\.?\)", "", author).strip(" ,"), title


def _main(title: str) -> str:
    return re.split(r"[:;]|\s[-–—]\s", title, maxsplit=1)[0]


def _surname(author: str) -> str:
    first = re.split(r"\band\b|&|;| et al", author)[0]
    if "," in first:  # 'Lesko, Leonard H.' -> surname first
        return (_norm(first.split(",")[0]).split() or [""])[-1]
    parts = _norm(first).split()
    return parts[-1] if parts else ""


def _two_way(wanted: str, found: str) -> bool:
    a, b = _words(wanted), _words(found)
    if not a or not b:
        return False
    common = len(a & b)
    return common / len(a) >= 0.7 and common / len(b) >= 0.6


def _title_match(wanted: str, found: str) -> bool:
    """Titles must largely agree both ways (main titles, or full titles), so a short invented
    title cannot hide inside a longer real one."""
    return _two_way(_main(wanted), _main(found)) or _two_way(wanted, found)


def _author_match(surname: str, names: list[str]) -> bool:
    return bool(surname) and any(surname in _norm(n).split() for n in names)


def _openlibrary(author: str, title: str, surname: str) -> bool:
    r = requests.get("https://openlibrary.org/search.json",
                     params={"title": _main(title), "author": surname or author, "limit": 5,
                             "fields": "title,author_name"},
                     headers=UA, timeout=20)
    r.raise_for_status()
    return any(_title_match(title, d.get("title", "")) and _author_match(surname, d.get("author_name") or [])
               for d in r.json().get("docs", []))


def _crossref(author: str, title: str, surname: str) -> bool:
    r = requests.get("https://api.crossref.org/works",
                     params={"query.bibliographic": title, "query.author": author, "rows": 5,
                             "select": "title,author"},
                     headers=UA, timeout=20)
    r.raise_for_status()
    for item in r.json().get("message", {}).get("items", []):
        names = [a.get("family", "") for a in item.get("author", []) or []]
        if any(_title_match(title, t) for t in item.get("title", []) or []) and _author_match(surname, names):
            return True
    return False


def verify(entries: list[dict]) -> list[dict]:
    """Keep only entries found in a catalogue. If both catalogues are unreachable, keep none
    (an empty list is safer than publishing invented books under our name)."""
    if MOCK:
        return entries
    kept: list[dict] = []
    for e in entries:
        parsed = parse(e.get("title", ""))
        if not parsed:
            log(f"reading list: dropped (cannot parse) {e.get('title', '')[:80]}")
            continue
        author, title = parsed
        surname = _surname(author)
        found = False
        for lookup in (_openlibrary, _crossref):
            try:
                if lookup(author, title, surname):
                    found = True
                    break
            except (requests.RequestException, ValueError) as exc:
                log(f"reading list: {lookup.__name__} unavailable ({str(exc)[:80]})")
        if found:
            kept.append(e)
        else:
            log(f"reading list: dropped (not found in any catalogue) {e.get('title', '')[:80]}")
    log(f"reading list: {len(kept)}/{len(entries)} entries verified")
    return kept
