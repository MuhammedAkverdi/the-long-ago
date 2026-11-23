"""GitHub issues & releases through the `gh` CLI (preinstalled on Actions runners)."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

from .config import MOCK, log


def repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "")


def api(method: str, path: str, data: dict | None = None):
    if MOCK:
        log(f"[mock gh] {method} {path} {json.dumps(data)[:200] if data else ''}")
        return {"number": 1, "html_url": "https://github.com/mock/issue/1"} if method == "POST" else []
    cmd = ["gh", "api", "-X", method, f"repos/{repo()}/{path}",
           "-H", "Accept: application/vnd.github+json"]
    if data is not None:
        cmd += ["--input", "-"]
    proc = subprocess.run(cmd, input=json.dumps(data) if data is not None else None,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {method} {path} failed: {proc.stderr[-1000:]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def ensure_label(name: str, color: str) -> None:
    try:
        api("POST", "labels", {"name": name, "color": color})
    except RuntimeError:
        pass  # already exists


def create_issue(title: str, body: str, labels: list[str]) -> dict:
    return api("POST", "issues", {"title": title, "body": body, "labels": labels})


def comment(number: int, body: str) -> None:
    api("POST", f"issues/{number}/comments", {"body": body})


def close(number: int) -> None:
    api("PATCH", f"issues/{number}", {"state": "closed"})


def comments(number: int) -> list[dict]:
    if MOCK:
        return []
    return api("GET", f"issues/{number}/comments?per_page=100") or []


def get_issue(number: int) -> dict:
    return api("GET", f"issues/{number}")


def open_issues(label: str) -> list[dict]:
    if MOCK:
        return []
    return api("GET", f"issues?state=open&labels={label}&per_page=50&sort=created&direction=asc") or []


def release(tag: str, title: str, notes: str, files: list[pathlib.Path]) -> str:
    if MOCK:
        log(f"[mock gh] release {tag} with {[f.name for f in files]}")
        return f"https://github.com/mock/releases/{tag}"
    exists = subprocess.run(["gh", "release", "view", tag, "--repo", repo()], capture_output=True).returncode == 0
    if exists:
        subprocess.run(["gh", "release", "upload", tag, *map(str, files), "--repo", repo(), "--clobber"], check=True)
    else:
        subprocess.run(["gh", "release", "create", tag, *map(str, files), "--repo", repo(),
                        "--title", title, "--notes", notes], check=True)
    return f"https://github.com/{repo()}/releases/tag/{tag}"


def download_release(tag: str, pattern: str, folder: pathlib.Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["gh", "release", "download", tag, "--repo", repo(), "--pattern", pattern,
                    "--dir", str(folder), "--clobber"], check=True)
