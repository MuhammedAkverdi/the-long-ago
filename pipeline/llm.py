"""Thin wrappers around the Gemini API (google-genai SDK) with model fallback chains.

Each role in config.yaml lists models in order. If a model is gone (404), refused, or its
quota is exhausted, the next one is tried. "auto" picks the newest available Flash model,
so the channel keeps working when Google renames or retires models.
"""
from __future__ import annotations

import time

import json
import re

import requests

from .config import CFG, MOCK, env, log, retry

_client = None
_dead: set[str] = set()          # models that failed permanently during this run
_auto_cache: list[str] | None = None


def client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=env("GEMINI_API_KEY"))
    return _client


def _types():
    from google.genai import types
    return types


def _auto_models() -> list[str]:
    """Newest general-purpose Flash text models the key can see."""
    global _auto_cache
    if _auto_cache is None:
        names = []
        try:
            for m in client().models.list():
                name = (m.name or "").removeprefix("models/")
                actions = getattr(m, "supported_actions", None) or []
                if "flash" not in name or any(x in name for x in ("image", "tts", "live", "embed", "transcribe", "audio")):
                    continue
                if actions and "generateContent" not in actions:
                    continue
                names.append(name)
        except Exception as exc:  # noqa: BLE001
            log(f"could not list models: {exc}")

        def version(n):
            m = re.search(r"(\d+(?:\.\d+)?)", n)
            return (float(m.group(1)) if m else 0.0, "lite" not in n, "preview" not in n)

        _auto_cache = sorted(set(names), key=version, reverse=True)[:3]
        log(f"auto models: {_auto_cache}")
    return _auto_cache


def candidates(role: str) -> list[str]:
    value = CFG["models"][role]
    listed = value if isinstance(value, list) else [value]
    out: list[str] = []
    for m in listed:
        for name in (_auto_models() if m == "auto" else [m]):
            if name not in out and name not in _dead:
                out.append(name)
    return out


def _is_quota_or_gone(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = str(exc).lower()
    return (code in (400, 403, 404, 429, 500, 503, 504) or "resource_exhausted" in text or "not found" in text
            or "quota" in text or "unavailable" in text)


class LLMUnavailable(RuntimeError):
    """Every model of a role is out of quota or overloaded: the work so far is kept for a later run."""


def with_models(role: str, fn, what: str):
    """Call fn(model) with retries, moving down the role's model list on permanent/quota errors.

    If every model fails only from quota/overload, wait once and try the remaining ones again,
    then raise LLMUnavailable (the caller pauses instead of losing the episode)."""
    last, transient = None, False
    rounds = 1 if what in ("gemini vision", "gemini research") else 2  # QC and grounding have cheap fallbacks
    for round_ in range(rounds):
        if round_:
            if not candidates(role):
                break
            log(f"all '{role}' models busy; waiting 120s before one more round")
            time.sleep(120)
        for model in candidates(role):
            try:
                return retry(lambda m=model: fn(m), tries=4, base=8.0, what=f"{what} [{model}]")
            except Exception as exc:  # noqa: BLE001
                last = exc
                if _is_quota_or_gone(exc):
                    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                    text = str(exc).lower()
                    if code in (429, 500, 503, 504) or "resource_exhausted" in text or "unavailable" in text:
                        transient = True
                    gone = code in (400, 403, 404) or "per day" in text or "perday" in text or "daily" in text
                    if gone and what != "gemini research":  # a grounding refusal must not kill the model for plain text
                        _dead.add(model)  # gone or out of daily quota: skip it for the rest of this run
                    log(f"model {model} unavailable ({str(exc)[:160]}); trying next")
                    continue
                raise
    if transient:  # quota/overload only: worth pausing and trying again tomorrow
        raise LLMUnavailable(f"no working model for '{role}': {last}")
    raise RuntimeError(f"no working model for '{role}': {last}")


def generate_text(prompt: str, *, role: str = "writer", system: str | None = None, temperature: float = 0.9) -> str:
    types = _types()

    def call(model):
        resp = client().models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system, temperature=temperature))
        text = (resp.text or "").strip()
        if not text:
            raise RuntimeError("empty response")
        return text

    return with_models(role, call, "gemini text")


def _parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"[\[{].*[\]}]", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def generate_json(prompt: str, *, role: str = "writer", system: str | None = None,
                  temperature: float = 0.8, validate=None):
    """Ask for JSON. `validate(obj)` may raise to trigger a retry."""
    types = _types()

    def call(model):
        resp = client().models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system, temperature=temperature,
                                               response_mime_type="application/json"))
        obj = _parse_json(resp.text or "")
        if validate:
            validate(obj)
        return obj

    return with_models(role, call, "gemini json")


def generate_json_parts(parts: list, *, role: str = "light", temperature: float = 0.2):
    """Multimodal JSON request (e.g. image + question)."""
    types = _types()

    def call(model):
        resp = client().models.generate_content(
            model=model, contents=parts,
            config=types.GenerateContentConfig(temperature=temperature, response_mime_type="application/json"))
        obj = _parse_json(resp.text or "")
        return obj[0] if isinstance(obj, list) and obj else obj

    return with_models(role, call, "gemini vision")


def _resolve_url(url: str) -> str | None:
    """Follow redirects (grounding links are redirect URLs); return final URL if reachable."""
    try:
        r = requests.get(url, allow_redirects=True, timeout=15, stream=True,
                         headers={"User-Agent": "Mozilla/5.0 (research-bot)"})
        final = r.url
        r.close()
        if r.status_code < 400 and "vertexaisearch" not in final:
            return final
    except requests.RequestException:
        pass
    return None


def research(topic: str) -> tuple[str, list[dict]]:
    """Google-Search-grounded research notes plus verified source links."""
    if MOCK:
        return ("Mock research notes about " + topic, [{"title": "Example source", "url": "https://example.org"}])
    types = _types()
    prompt = f"""You are a careful historical researcher preparing notes for a calm,
long-form YouTube documentary about ordinary daily life: "{topic}".

Write detailed research notes (1500-2500 words) covering: time and place, social
structure, homes, food and meals, work, money and prices, clothing, hygiene and
medicine, family life, religion and festivals, leisure, law and danger, and the
small surprising details that make the past feel real. Prefer findings from
archaeology, primary sources and academic historians; name them. Clearly mark
anything uncertain or debated ("historians disagree..."). Do not invent quotes,
numbers or sources."""

    def call(model):
        resp = client().models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())],
                                               temperature=0.3))
        if not (resp.text or "").strip():
            raise RuntimeError("empty research")
        return resp

    try:
        resp = with_models("researcher", call, "gemini research")
    except RuntimeError as exc:
        # Google Search grounding is not always available on the free tier: research from model knowledge
        log(f"grounded research unavailable ({str(exc)[:120]}); using ungrounded research")
        text = generate_text(prompt + """

End with a line "SOURCES:" followed by 5-8 lines, each one real, well-known scholarly book or
article on this subject in the form "Author, Title (Year)". Only list works you are certain exist.""",
                             role="researcher", temperature=0.3)
        notes, _, tail = text.partition("SOURCES:")
        books = [re.sub(r"^\d+[.)]\s*", "", ln.strip(" -*\t")) for ln in tail.splitlines() if ln.strip(" -*\t")][:8]
        from .sources import verify  # model-recalled titles can be invented: keep only catalogued ones
        checked = verify([{"title": b[:160], "url": ""} for b in books])
        log(f"research: {len(notes)} chars, {len(checked)}/{len(books)} reading-list entries verified (ungrounded)")
        return notes.strip(), checked
    sources: list[dict] = []
    seen = set()
    try:
        chunks = resp.candidates[0].grounding_metadata.grounding_chunks or []
    except (AttributeError, IndexError, TypeError):
        chunks = []
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        if not web or not web.uri:
            continue
        final = _resolve_url(web.uri)
        if final and final not in seen:
            seen.add(final)
            sources.append({"title": (web.title or web.domain or final)[:120], "url": final})
    log(f"research: {len(resp.text)} chars, {len(sources)} verified sources")
    return resp.text.strip(), sources[:12]
