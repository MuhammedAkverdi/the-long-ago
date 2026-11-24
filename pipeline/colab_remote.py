"""Start the Colab image notebook headlessly through the official Colab API (MCP endpoint), then delete the runtime.

Needs secrets COLAB_REFRESH_TOKEN (OAuth refresh token with scope
https://www.googleapis.com/auth/colaboratory, minted with the YT_CLIENT_ID/SECRET client) and COLAB_PROJECT
(Google Cloud project id with the Colaboratory API enabled). Without them nothing happens.
Every failure is logged and ignored: the episode then falls back to Cloudflare.
"""
from __future__ import annotations

import atexit
import json
import threading
import time
import uuid

import requests

from .config import CFG, MOCK, ROOT, env, log

MCP = "https://colaboratory.googleapis.com/mcp"
SPECS = [("VARIANT_GPU", "A100", "SHAPE_STANDARD"), ("VARIANT_GPU", "L4", "SHAPE_STANDARD")]
SESSION = "tla-images"


def available() -> bool:
    return (not MOCK and bool(env("COLAB_REFRESH_TOKEN", required=False))
            and bool(env("COLAB_PROJECT", required=False)) and bool(env("GH_TOKEN", required=False)))


class _Client:
    def __init__(self):
        self._token, self._exp = None, 0.0

    def token(self) -> str:
        if not self._token or time.time() > self._exp - 120:
            r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
                "client_id": env("YT_CLIENT_ID"), "client_secret": env("YT_CLIENT_SECRET"),
                "refresh_token": env("COLAB_REFRESH_TOKEN"), "grant_type": "refresh_token"})
            r.raise_for_status()
            data = r.json()
            self._token, self._exp = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._token

    def call(self, tool: str, args: dict, timeout: int = 660) -> dict:
        """tools/call over the MCP HTTP endpoint; returns structured content, raises RuntimeError(code text)."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
        r = requests.post(MCP, json=body, timeout=timeout, headers={
            "Authorization": f"Bearer {self.token()}", "x-goog-user-project": env("COLAB_PROJECT"),
            "content-type": "application/json", "accept": "application/json, text/event-stream"})
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        msg = None
        if "text/event-stream" in r.headers.get("content-type", ""):
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        obj = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if "result" in obj or "error" in obj:
                        msg = obj
        else:
            msg = r.json()
        if not msg:
            raise RuntimeError(f"empty MCP response: {r.text[:200]}")
        if "error" in msg:
            raise RuntimeError(json.dumps(msg["error"])[:400])
        res = msg.get("result", {})
        content = res.get("structuredContent")
        if content is None:
            text = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
            try:
                content = json.loads(text) if text else {}
            except ValueError:
                content = {"text": text}
        if res.get("isError"):
            raise RuntimeError(json.dumps(content)[:400])
        return content or {}


class Remote:
    """One headless notebook run. start() returns at once; stop() always deletes the runtime."""

    def __init__(self):
        self.client = _Client()
        self.runtime: str | None = None
        self._rids: list[str] = []  # every runtime id ever requested: stop() deletes all of them
        self.started = False
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "Remote":
        self.started = True
        atexit.register(self.stop)  # never leave a paid GPU running if the job dies
        self._thread.start()
        return self

    def _create(self) -> str:
        last = None
        for variant, acc, shape in SPECS:
            if self._stopped.is_set():
                break
            rid = f"tla-{uuid.uuid4().hex[:12]}"
            self._rids.append(rid)
            req = str(uuid.uuid4())
            args = {"requestId": req, "runtimeId": rid, "runtime": {"runtimeSpec": {
                "variant": variant, "accelerator": acc, "shape": shape}}}
            for attempt in range(6):
                try:
                    rt = self.client.call("create_runtime", args)
                    name = rt.get("name") or f"runtimes/{rid}"
                    log(f"colab api: runtime {name} ({acc}) ready")
                    return name
                except Exception as exc:  # noqa: BLE001
                    last, text = exc, str(exc)
                    transient = (isinstance(exc, (requests.Timeout, requests.ConnectionError))
                                 or any(k in text for k in ("DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED", "HTTP 5")))
                    if transient and not self._stopped.is_set():
                        time.sleep(min(60, 10 * (attempt + 1)))  # same requestId: waits for the same runtime
                        continue
                    break  # FAILED_PRECONDITION etc.: try the next spec
            log(f"colab api: {acc} not available ({str(last)[:200]})")
            self._delete(f"runtimes/{rid}")  # it may still have been created behind a timeout
        raise RuntimeError(f"no Colab runtime: {last}")

    def _exec(self, code: str, execution_id: str | None = None) -> dict:
        args = {"code": code, "session": f"{self.runtime}/sessions/{SESSION}"}
        if execution_id:
            args["executionId"] = execution_id
        for attempt in range(4):
            try:
                return self.client.call("execute_code", args)
            except Exception as exc:  # noqa: BLE001
                if attempt < 3 and any(k in str(exc) for k in ("DEADLINE_EXCEEDED", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
                    time.sleep(15 * (attempt + 1))
                    continue
                raise
        return {}

    def _run(self) -> None:
        try:
            self.runtime = self._create()
            if self._stopped.is_set():
                return
            script = (ROOT / "colab" / "colab_images.py").read_text(encoding="utf-8")
            launch = (
                "import os, subprocess, sys\n"
                f"open('/content/tla_images.py', 'w').write({script!r})\n"
                f"env = dict(os.environ, GH_TOKEN={env('GH_TOKEN')!r}, COLAB_HEADLESS='1')\n"
                "log = open('/content/tla_images.log', 'w')\n"
                "proc = subprocess.Popen([sys.executable, '-u', '/content/tla_images.py'], env=env,\n"
                "                        stdout=log, stderr=subprocess.STDOUT)\n"
                "print('started', proc.pid)\n")
            out = self._exec(launch, execution_id=str(uuid.uuid4()))
            if out.get("executionError"):
                raise RuntimeError(f"launch failed: {out['executionError']}")
            log("colab api: notebook started on the runtime")
            limit = time.time() + 60 * float(CFG.get("colab", {}).get("remote_max_minutes", 120))
            while not self._stopped.wait(90):
                st = self._exec("print(proc.poll()); print(open('/content/tla_images.log').read()[-400:])")
                lines = (st.get("stdout") or "").splitlines()
                tail = " | ".join(lines[1:])[-300:]
                if lines and lines[0].strip() != "None":
                    log(f"colab api: notebook finished (exit {lines[0].strip()}): {tail}")
                    break
                log(f"colab api: running… {tail[-160:]}")
                if time.time() > limit:
                    log("colab api: time limit reached; stopping the runtime")
                    break
        except Exception as exc:  # noqa: BLE001
            log(f"colab api: {str(exc)[:300]}")
        finally:
            self.stop()

    def _delete(self, name: str) -> None:
        try:
            self.client.call("delete_runtime", {"name": name, "allowMissing": True}, timeout=120)
            log(f"colab api: runtime {name} deleted")
        except Exception as exc:  # noqa: BLE001
            log(f"colab api: could not delete runtime {name}: {str(exc)[:200]}")

    def stop(self) -> None:
        """Delete every runtime this run requested (allowMissing makes repeats harmless)."""
        self._stopped.set()
        names = {f"runtimes/{r}" for r in self._rids}
        if self.runtime:
            names.add(self.runtime)
        self.runtime, self._rids = None, []
        for name in sorted(names):
            self._delete(name)


def cleanup_all() -> None:
    """Delete any runtime this pipeline started (names `runtimes/tla-…`). Run by the workflow even after a cancel."""
    if not available():
        return
    client = _Client()
    try:
        rts = client.call("list_runtimes", {}, timeout=120).get("runtimes", [])
    except Exception as exc:  # noqa: BLE001
        log(f"colab api: cannot list runtimes ({str(exc)[:200]})")
        return
    for rt in rts:
        name = rt.get("name", "")
        if name.startswith("runtimes/tla-"):
            try:
                client.call("delete_runtime", {"name": name, "allowMissing": True}, timeout=120)
                log(f"colab api: leftover runtime {name} deleted")
            except Exception as exc:  # noqa: BLE001
                log(f"colab api: could not delete {name}: {str(exc)[:200]}")


def start() -> Remote | None:
    if not available() or not CFG.get("colab", {}).get("enabled"):
        return None
    try:
        return Remote().start()
    except Exception as exc:  # noqa: BLE001
        log(f"colab api: not started ({str(exc)[:200]})")
        return None
