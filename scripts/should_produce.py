"""Cheap pre-check for the daily production run (standard library only, runs before any install).

Prints run=true/false for $GITHUB_OUTPUT. It mirrors the early exits of `pipeline produce`:
on a scheduled run that is not a production day and has nothing paused or half-delivered,
the job can stop here instead of spending ~5 minutes installing PyTorch for nothing
(GitHub Free gives private repos a limited monthly Actions allowance). When unsure: run.
"""
import datetime as dt
import json
import os
import re
import sys


def decide() -> bool:
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True
    try:
        with open("state/state.json", encoding="utf-8") as f:
            state = json.load(f)
        cfg = open("config.yaml", encoding="utf-8").read()
    except (OSError, ValueError):
        return True
    m = re.search(r"^\s*produce_weekdays:\s*\[([\d,\s]*)\]", cfg, re.M)
    if not m:  # setting written in another form: be safe and run
        return True
    days = [int(x) for x in m.group(1).split(",") if x.strip()]
    if dt.datetime.now(dt.timezone.utc).weekday() in days:
        return True
    if any(t.get("paused") and t.get("status") == "in_progress" for t in state.get("topics", {}).values()):
        return True
    return any(r.get("type") == "episode" and r.get("status") in ("produced", "uploaded") and not r.get("review_issue")
               for r in state.get("episodes", {}).values())


if __name__ == "__main__":
    try:
        go = decide()
    except Exception as exc:  # noqa: BLE001 - never skip production because of this helper
        print(f"pre-check error, running anyway: {exc}", file=sys.stderr)
        go = True
    print(f"run={'true' if go else 'false'}")
    if not go:
        print("not a production day and nothing paused: skipping the heavy install", file=sys.stderr)
