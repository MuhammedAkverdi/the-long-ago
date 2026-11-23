"""Configuration, paths and small shared helpers."""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time
import random
import unicodedata

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = pathlib.Path(os.environ.get("WORK_DIR", ROOT / "work"))
STATE_PATH = ROOT / "state" / "state.json"
MOCK = os.environ.get("MOCK") == "1"

with open(ROOT / "config.yaml", encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)


def env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value and not MOCK:
        raise RuntimeError(f"Eksik ortam değişkeni / missing secret: {name}")
    return value


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def slugify(text: str, max_len: int = 50) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "episode"


class PermanentError(RuntimeError):
    """An error that retrying will not fix (bad key, unknown model, bad request)."""


def _is_permanent(exc: Exception) -> bool:
    if isinstance(exc, PermanentError):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 429)


def retry(fn, tries: int = 5, base: float = 4.0, what: str = "call"):
    """Run fn() with exponential backoff. Permanent 4xx errors are raised at once."""
    last = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we really want to retry anything transient
            last = exc
            if attempt == tries or _is_permanent(exc):
                break
            wait = base * (2 ** (attempt - 1)) + random.uniform(0, 2)
            log(f"{what} failed ({exc.__class__.__name__}: {str(exc)[:200]}); retry {attempt}/{tries - 1} in {wait:.0f}s")
            time.sleep(wait)
    raise last  # type: ignore[misc]


def run(cmd: list[str], cwd: pathlib.Path | None = None) -> str:
    """Run a command, raise with stderr tail on failure, return stdout."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(map(str, cmd[:6]))}...\n{proc.stderr[-3000:]}")
    return proc.stdout


def duration(path: pathlib.Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


def load_state() -> dict:
    with open(STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)
    for key, default in (("topics", {}), ("episodes", {}), ("recent_structures", []), ("scheduled", [])):
        state.setdefault(key, default)
    return state


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.replace(STATE_PATH)


def write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def read_json(path: pathlib.Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def upload_mode() -> str:
    return (os.environ.get("UPLOAD_MODE") or CFG["channel"]["upload_mode"]).strip().lower()
