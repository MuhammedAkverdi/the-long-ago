"""Command line entry point.

    python -m pipeline topics           # Monday: propose topics -> GitHub issue
    python -m pipeline produce          # Tue/Thu: make next chosen episode -> review issue
    python -m pipeline review --issue N --command "/onay"   # owner approves or rejects
    python -m pipeline compile          # monthly: long sleep compilation
    python -m pipeline check            # verify API keys / model ids / voice
    python -m pipeline merge-state BASE MINE THEIRS   # 3-way merge used by the workflows
    MOCK=1 python -m pipeline selftest  # offline end-to-end test (no API keys needed)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import datetime as dt
import re
import sys
from zoneinfo import ZoneInfo

from . import colab, github, images, thumbnail, youtube
from .config import CFG, MOCK, WORK, duration, env, load_state, log, read_json, run, save_state, upload_mode, write_json
from .episode import AI_NOTE, produce, ts, upload_all
from .llm import LLMUnavailable
from .topics import propose

TOPIC_LABEL, REVIEW_LABEL = "konu-secimi", "onay-bekliyor"


def api_mode() -> bool:
    return upload_mode() == "api"


# ---------------------------------------------------------------- scheduling
def next_slots(slots: list[dict], count: int, after: dt.datetime, taken: set[str]) -> list[str]:
    tz = ZoneInfo(CFG["channel"]["timezone"])
    local = after.astimezone(tz)
    found: list[str] = []
    for day in range(0, 60):
        date = (local + dt.timedelta(days=day)).date()
        for slot in sorted(slots, key=lambda s: s["time"]):
            if date.weekday() != slot["weekday"]:
                continue
            hh, mm = map(int, slot["time"].split(":"))
            when = dt.datetime(date.year, date.month, date.day, hh, mm, tzinfo=tz)
            iso = when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if when > local and iso not in taken and iso not in found:
                found.append(iso)
                if len(found) == count:
                    return found
    return found


def pretty(iso: str) -> str:
    when = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return when.astimezone(ZoneInfo(CFG["channel"]["timezone"])).strftime("%d.%m.%Y %H:%M")


# ---------------------------------------------------------------- topics
def cmd_topics(_args) -> None:
    state = load_state()
    past = [t["title"] for t in state["topics"].values()]
    topics = propose(past)
    week = dt.date.today().isocalendar()
    lines = [
        "Merhaba! Bu haftanın video konusu önerileri aşağıda. 👇",
        "",
        f"**Yapmanız gereken:** Beğendiğiniz **{CFG['script']['episodes_per_week']} konunun** kutusunu işaretleyin (telefondan da olur).",
        "Hiçbir şey işaretlemezseniz sistem ertesi sabah listedeki ilk konuları kendisi seçer.",
        "",
    ]
    topics = [t for t in topics if t["id"] not in state["topics"]]
    if not topics:
        raise RuntimeError("model only proposed topics that were already used")
    for t in topics:
        lines.append(f"- [ ] **{t['title']}** — {t['pitch']} <!-- topic:{t['id']} -->")
        state["topics"][t["id"]] = {"title": t["title"], "pitch": t["pitch"], "status": "proposed", "attempts": 0}
    github.ensure_label(TOPIC_LABEL, "c5a572")
    issue = github.create_issue(f"📜 Haftanın konuları — {week[0]}-H{week[1]}", "\n".join(lines), [TOPIC_LABEL])
    log(f"topic issue #{issue['number']} created")
    save_state(state)


def parse_topics(body: str) -> list[tuple[str, bool]]:
    return [(m.group(2), m.group(1).lower() == "x")
            for m in re.finditer(r"^- \[([ xX])\] .*?<!-- topic:(\w+) -->", body or "", re.M)]


def choose_topic(state: dict) -> tuple[str, dict] | None:
    now = dt.datetime.now(dt.timezone.utc)
    for issue in github.open_issues(TOPIC_LABEL):
        items = parse_topics(issue.get("body", ""))
        checked = [tid for tid, c in items if c]
        created = dt.datetime.fromisoformat(issue["created_at"].replace("Z", "+00:00"))
        if checked:
            wanted = checked
        elif now - created > dt.timedelta(hours=20):
            wanted = [tid for tid, _ in items][: CFG["script"]["episodes_per_week"]]
        else:
            continue
        todo = []
        for tid in wanted:
            t = state["topics"].get(tid)
            if not t or t["status"] in ("produced", "failed"):
                continue
            if t.get("attempts", 0) >= 2:  # failed twice: give up on it, try the next one
                t["status"] = "failed"
                github.comment(issue["number"], f"⚠️ \"{t['title']}\" 2 kez denendi ve üretilemedi; atlanıyor.")
                continue
            todo.append(tid)
        if not todo:
            github.close(issue["number"])
            continue
        return todo[0], {"issue": issue["number"], "remaining": len(todo) - 1}
    return None


# ---------------------------------------------------------------- produce
def review_body(record: dict, release_url: str) -> str:
    hours = CFG["approval"]["auto_approve_hours"]
    lines = [f"<!-- episode:{record['slug']} -->", f"## {record['title']}", "",
             f"Süre: **{record['minutes']} dk** · Anlatı yapısı: `{record.get('structure', '-')}` · Shorts: {len(record.get('shorts', []))}",
             f"Doğruluk kontrolü: {record.get('fact_check', '-')}", "",
             f"İzlemek için (telefondan da açılır): {release_url}", "",
             f"**Hiçbir şey yapmazsanız {hours} saat sonra otomatik onaylanır ve yayın takvimine girer.**",
             "- Hemen onaylamak için yoruma `/onay` yazın", "- Yayınlanmasın derseniz `/iptal` yazın", "",
             "Kontrol listesi: tarihî bir hata var mı? Görsellerde garip el/yüz/yazı var mı? Ses düzgün mü?"]
    if not api_mode():
        lines += ["", "_Not: YouTube API izni henüz açılmadı; video onaylansa da yüklenmek için bu izni bekler._"]
    return "\n".join(lines)


def _finish(record: dict, folder) -> dict:
    """Release -> (API) upload -> review issue. Each step is saved, so a re-run continues where it stopped."""
    def persist():
        st = load_state()
        st["episodes"][record["slug"]] = record
        save_state(st)

    if not record.get("release_tag"):
        files = [folder / n for n in ("final.mp4", "thumbnail.jpg", "thumb_raw.png", "captions.srt", "metadata.md", "script.json")]
        files += [folder / sh["file"] for sh in record.get("shorts", [])]
        tag = f"ep-{record['slug']}"
        github.release(tag, record["title"], "The Long Ago — bölüm dosyaları", [f for f in files if f.exists()])
        record["release_tag"] = tag
        persist()
    if not record.get("review_issue"):
        github.ensure_label(REVIEW_LABEL, "d93f0b")
        url = f"https://github.com/{github.repo()}/releases/tag/{record['release_tag']}"
        record["review_issue"] = github.create_issue(f"🎬 Onay bekliyor: {record['title']}",
                                                     review_body(record, url), [REVIEW_LABEL])["number"]
        persist()
    return record


def _prune_work(state: dict) -> None:
    """Drop work folders nothing will resume, so the cross-run cache doesn't grow."""
    if not WORK.exists():
        return
    keep = {t.get("work_slug") for t in state["topics"].values() if t.get("status") == "in_progress"}
    keep |= {slug for slug, r in state["episodes"].items() if not r.get("review_issue")}
    for d in WORK.iterdir():
        if d.is_dir() and d.name not in keep:
            log(f"removing old work folder {d.name}")
            shutil.rmtree(d, ignore_errors=True)


def cmd_produce(_args) -> None:
    state = load_state()
    _prune_work(state)
    # 1) an episode that was made but not fully delivered last time
    for rec in state["episodes"].values():
        if rec.get("type") == "episode" and rec.get("status") in ("produced", "uploaded") and not rec.get("review_issue"):
            if not rec.get("release_tag") and not (WORK / rec["slug"] / "final.mp4").exists():
                # files were lost with the old runner before reaching GitHub: redo this topic (max 2 tries)
                log(f"cannot resume {rec['slug']} (files lost); topic will be retried")
                rec["status"] = "failed"
                if rec.get("topic_id") in state["topics"]:
                    state["topics"][rec["topic_id"]]["status"] = "in_progress"
                continue
            log(f"resuming delivery of {rec['slug']}")
            _finish(rec, WORK / rec["slug"])
            colab.cleanup(rec["slug"])
            return
    paused = [tid for tid, t in state["topics"].items() if t.get("paused") and t.get("status") == "in_progress"]
    if not paused and os.environ.get("GITHUB_EVENT_NAME") == "schedule" \
            and dt.datetime.now(dt.timezone.utc).weekday() not in CFG.get("production", {}).get("produce_weekdays", [1, 3]):
        log("no paused episode and not a production day")
        return
    picked = None
    if paused:
        picked = (paused[0], {"issue": None, "remaining": 1})
    else:
        picked = choose_topic(state)
        save_state(state)  # choose_topic may have marked failures
    if not picked:
        log("nothing to produce (no chosen topics yet)")
        return
    tid, info = picked
    t = state["topics"][tid]
    if not t.pop("paused", False):  # continuing a paused episode is not a new attempt
        t["attempts"] = t.get("attempts", 0) + 1
        t["pauses"] = 0
        t["work_slug"] = f"{dt.date.today():%Y%m%d}-{tid}-a{t['attempts']}"  # a new try starts from clean files
    t["status"] = "in_progress"
    save_state(state)
    t.setdefault("work_slug", f"{dt.date.today():%Y%m%d}-{tid}")
    save_state(state)
    try:
        record = produce(t["title"], state["recent_structures"], slug=t["work_slug"])
    except (images.QuotaPause, LLMUnavailable) as exc:
        state = load_state()
        t = state["topics"][tid]
        t["pauses"] = t.get("pauses", 0) + 1
        if t["pauses"] > 4:  # should finish in ~2 days; something else is wrong, so count it as a failed try
            save_state(state)
            raise RuntimeError(f"episode still unfinished after {t['pauses']} paused runs: {exc}") from exc
        log(f"paused: {exc}. Finished parts are kept; the next daily run continues this episode.")
        t["paused"] = True
        save_state(state)
        return
    record["topic_id"] = tid
    state = load_state()
    state["episodes"][record["slug"]] = record
    state["topics"][tid]["status"] = "produced"
    state["recent_structures"] = (state["recent_structures"] + [record.get("structure", "")])[-10:]
    save_state(state)
    _finish(record, WORK / record["slug"])
    shutil.rmtree(WORK / record["slug"], ignore_errors=True)  # delivered: keep the cross-run cache small
    colab.cleanup(record["slug"])
    if info["remaining"] == 0 and info["issue"]:
        github.close(info["issue"])


# ---------------------------------------------------------------- review / approval
def _record_for_issue(state: dict, number: int) -> dict | None:
    issue = github.get_issue(number)
    m = re.search(r"<!-- episode:([\w-]+) -->", issue.get("body", "") or "")
    return state["episodes"].get(m.group(1)) if m else None


def _upload_from_release(record: dict) -> None:
    """Download the episode files from its GitHub release and upload to YouTube (private)."""
    folder = WORK / record["slug"]
    done = record.get("video_id") and all(sh.get("video_id") for sh in record.get("shorts", []))
    if not done and not (folder / "final.mp4").exists():
        github.download_release(record["release_tag"], "*", folder)
        parts = sorted(folder.glob("part*.mp4"))
        if parts and not (folder / "final.mp4").exists():  # long compilation stored in pieces
            lst = folder / "parts.txt"
            lst.write_text("".join(f"file '{x.name}'\n" for x in parts))
            run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst.name, "-c", "copy",
                 "-movflags", "+faststart", "final.mp4"], cwd=folder)

    def persist():
        st = load_state()
        st["episodes"][record["slug"]] = record
        save_state(st)

    upload_all(record, folder, persist)


def approve(record: dict, issue_no: int, auto: bool = False) -> bool:
    """Upload (API mode) and schedule into the next free slots. Returns True when done."""
    if record.get("status") in ("approved", "rejected"):
        return False
    if not api_mode():
        record["approve_requested"] = True
        st = load_state()
        st["episodes"][record["slug"]] = record
        save_state(st)
        github.comment(issue_no, "👍 Onay kaydedildi. YouTube API izni açılınca otomatik yüklenip zamanlanacak.")
        return False
    _upload_from_release(record)
    state = load_state()
    taken = set(state["scheduled"])
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)
    main_slot = next_slots(CFG["channel"]["publish_slots"], 1, now, taken)[0]
    taken.add(main_slot)
    after_main = dt.datetime.strptime(main_slot, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    shorts = [sh for sh in record.get("shorts", []) if sh.get("video_id")]
    short_slots = next_slots(CFG["channel"]["shorts_slots"], len(shorts), after_main, taken)
    youtube.schedule(record["video_id"], main_slot)
    lines = [("⏱️ İtiraz gelmediği için otomatik onaylandı." if auto else "✅ Onaylandı!"),
             f"Ana video: **{pretty(main_slot)}** (Türkiye saati)"]
    for sh, slot in zip(shorts, short_slots):
        youtube.schedule(sh["video_id"], slot)
        sh["publish_at"] = slot
        lines.append(f"- Short \"{sh['title']}\": {pretty(slot)}")
    lines.append("\nYouTube'da zamanlandı. Vazgeçerseniz yayın saatinden önce `/iptal` yazın.")
    state["scheduled"] = sorted(taken | set(short_slots))[-80:]
    record["status"] = "approved"
    record["publish_at"] = main_slot
    state["episodes"][record["slug"]] = record
    save_state(state)
    github.comment(issue_no, "\n".join(lines))
    github.close(issue_no)
    return True


def reject(record: dict, issue_no: int) -> None:
    state = load_state()
    record = state["episodes"][record["slug"]]
    if record.get("status") == "rejected":
        github.comment(issue_no, "Bu video zaten iptal edilmişti.")
        return
    mine = [record.get("publish_at")] + [sh.get("publish_at") for sh in record.get("shorts", [])]
    if api_mode():
        for vid in [record.get("video_id")] + [sh.get("video_id") for sh in record.get("shorts", [])]:
            if vid:
                youtube.unschedule(vid)
    state["scheduled"] = [x for x in state["scheduled"] if x not in mine]
    record["status"] = "rejected"
    record.pop("publish_at", None)
    save_state(state)
    msg = "❌ İptal edildi. Video yayınlanmayacak."
    if record.get("video_id"):
        msg += " YouTube'daki zamanlama kaldırıldı, video gizli kaldı."
    github.comment(issue_no, msg)
    github.close(issue_no)


def cmd_review(args) -> None:
    state = load_state()
    record = _record_for_issue(state, args.issue)
    if not record:
        github.comment(args.issue, "Bu konuya bağlı bölüm bulunamadı.")
        return
    command = args.command.strip().lower()
    if command.startswith("/iptal"):
        reject(record, args.issue)
    elif command.startswith("/onay"):
        if record.get("status") in ("approved", "rejected"):
            github.comment(args.issue, f"Bu video zaten {'onaylanmış' if record['status'] == 'approved' else 'iptal edilmiş'}; değişiklik yapılmadı.")
            return
        if api_mode():  # uploads go through the daily job so we never exceed YouTube's daily quota
            record["approve_requested"] = True
            state["episodes"][record["slug"]] = record
            save_state(state)
            github.comment(args.issue, "👍 Onaylandı. Yarın sabah 09:30'da yüklenip yayın takvimine eklenecek.")
            return
        approve(record, args.issue)


def _owner_cancelled(issue_no: int) -> bool:
    return any(c.get("author_association") == "OWNER" and (c.get("body") or "").strip().lower().startswith("/iptal")
               for c in github.comments(issue_no))


def cmd_auto_approve(_args) -> None:
    """Daily: approve the oldest waiting video whose review window has passed (1 per day = YouTube quota)."""
    if not api_mode():
        log("upload_mode is not 'api' yet: nothing is published automatically")
        return
    state = load_state()
    limit = dt.timedelta(hours=CFG["approval"]["auto_approve_hours"])
    now = dt.datetime.now(dt.timezone.utc)
    waiting = sorted((r for r in state["episodes"].values()
                      if r.get("status") in ("produced", "uploaded") and r.get("review_issue") and r.get("release_tag")),
                     key=lambda r: r.get("created", ""))
    for rec in waiting:
        created = dt.datetime.fromisoformat(rec["created"])
        if rec.get("approve_requested") or now - created >= limit:
            if _owner_cancelled(rec["review_issue"]):  # a /iptal whose run was dropped
                reject(rec, rec["review_issue"])
                continue
            approve(rec, rec["review_issue"], auto=not rec.get("approve_requested"))
            return
    # finish thumbnails/captions that failed earlier (e.g. quota)
    for rec in state["episodes"].values():
        if (rec.get("status") == "approved" and rec.get("video_id") and rec.get("fix_attempts", 0) < 3
                and not (rec.get("thumbnail_set") and rec.get("captions_set", True))):
            rec["fix_attempts"] = rec.get("fix_attempts", 0) + 1
            _upload_from_release(rec)
            return
    log("nothing to auto-approve")


# ---------------------------------------------------------------- compilation
TITLES = ["{h} Hours of Ordinary Lives in History to Fall Asleep To",
          "Fall Asleep in the Past: {h} Hours of Everyday Life in History",
          "{h} Hours of Calm History for Sleep | How Ordinary People Lived"]


def cmd_compile(_args) -> None:
    state = load_state()
    cc = CFG["compilation"]
    pool = [e for e in state["episodes"].values()
            if e.get("type") == "episode" and e.get("status") == "approved" and e.get("release_tag") and not e.get("compiled")]
    pool.sort(key=lambda e: e.get("created", ""))
    chosen, minutes = [], 0.0
    for e in pool:
        chosen.append(e)
        minutes += e.get("minutes", 0)
        if minutes >= cc["target_minutes"]:
            break
    if len(chosen) < cc["min_episodes"]:
        log(f"not enough approved episodes for a compilation ({len(chosen)})")
        return
    slug = f"{dt.date.today():%Y%m%d}-sleep-compilation"
    folder = WORK / slug
    parts, starts, t = [], [], 0.0
    for i, e in enumerate(chosen):
        sub = folder / f"src{i}"
        github.download_release(e["release_tag"], "final.mp4", sub)
        if i == 0:
            try:
                github.download_release(e["release_tag"], "thumb_raw.png", folder)
            except Exception:  # noqa: BLE001 - older releases have no raw image
                github.download_release(e["release_tag"], "thumbnail.jpg", folder)
                (folder / "thumbnail.jpg").replace(folder / "thumb_raw.png")
        starts.append(t)
        t += duration(sub / "final.mp4")
        parts.append(sub / "final.mp4")
    listfile = folder / "list.txt"
    listfile.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    final = folder / "final.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(final)])
    hours = max(1, round(t / 3600))
    title = TITLES[len([e for e in state["episodes"].values() if e.get("type") == "compilation"]) % len(TITLES)].format(h=hours)
    from PIL import Image
    thumbnail.overlay(Image.open(folder / "thumb_raw.png"), "Fall asleep in the past", folder / "thumbnail.jpg", darken=0.6)
    desc = "\n".join(["Settle in, dim the lights and drift back through the lives of ordinary people in the past. "
                      "This calm compilation gathers several full episodes of The Long Ago.", "", "Episodes:"]
                     + [f"{ts(s)} {e['title']}" for s, e in zip(starts, chosen)]
                     + ["", "Sources for each episode are listed in the original videos.", "", AI_NOTE, "", "#history #sleep #thelongago"])
    record = {"slug": slug, "type": "compilation", "title": title, "minutes": round(t / 60, 1), "description": desc,
              "tags": ["history for sleep", "sleep documentary", "boring history"] + CFG["channel"]["default_tags"],
              "shorts": [], "status": "produced", "created": dt.datetime.now(dt.timezone.utc).isoformat(),
              "structure": "compilation"}
    (folder / "metadata.md").write_text(f"# {title}\n\n## Description\n\n{desc}\n\n## Tags\n\n{', '.join(record['tags'])}\n", encoding="utf-8")
    files = [folder / "thumbnail.jpg", folder / "metadata.md"]
    size = final.stat().st_size
    if size < 1.8e9:
        files.insert(0, final)
    else:  # GitHub release assets must be < 2 GB: split losslessly, join in any editor / upload part by part
        n = math.ceil(size / 1.6e9)
        run(["ffmpeg", "-y", "-i", str(final), "-c", "copy", "-map", "0", "-f", "segment",
             "-segment_time", f"{t / n:.0f}", "-reset_timestamps", "1", str(folder / "part%02d.mp4")])
        files = sorted(folder.glob("part*.mp4")) + files
    release_url = github.release(f"comp-{slug}", title, "Uyku derlemesi", files)
    record["release_tag"] = f"comp-{slug}"
    github.ensure_label(REVIEW_LABEL, "d93f0b")
    record["review_issue"] = github.create_issue(f"🌙 Onay bekliyor: {title}", review_body(record, release_url), [REVIEW_LABEL])["number"]
    state = load_state()
    for e in chosen:
        state["episodes"][e["slug"]]["compiled"] = True
    state["episodes"][slug] = record
    save_state(state)


# ---------------------------------------------------------------- utilities
def _merge(base, mine, theirs):
    """3-way merge of JSON values: take our change where we changed something, else theirs."""
    if isinstance(base, dict) and isinstance(mine, dict) and isinstance(theirs, dict):
        out = {}
        for k in set(mine) | set(theirs) | set(base):
            b, m, t = base.get(k), mine.get(k), theirs.get(k)
            if k not in mine and k in base:      # we deleted
                continue
            if k not in theirs and k in base and m == b:  # they deleted, we did not touch
                continue
            out[k] = _merge(b, m, t) if k in mine and k in theirs else (m if k in mine else t)
        return out
    if isinstance(mine, list) and isinstance(theirs, list):
        if mine == base:
            return theirs
        if theirs == base:
            return mine
        base_l = base if isinstance(base, list) else []
        kept = [x for x in theirs if not (x in base_l and x not in mine)]   # honour our removals
        return kept + [x for x in mine if x not in base_l and x not in theirs]  # add our additions
    return theirs if mine == base else mine


def cmd_merge_state(args) -> None:
    merged = _merge(read_json(args.base), read_json(args.mine), read_json(args.theirs))
    json.dump(merged, sys.stdout, indent=2, ensure_ascii=False)


def cmd_check(_args) -> None:
    """Smoke test of every service with tiny real calls. Run once after adding the secrets."""
    import requests
    from . import images, tts
    from .llm import candidates, generate_text
    ok = True

    def report(name, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        log(f"{'OK  ' if good else 'FAIL'} {name} {detail}")

    for role in ("writer", "researcher", "light"):
        try:
            report(f"Gemini {role}", generate_text("Reply with the single word: ready", role=role, temperature=0) != "",
                   f"(chain: {candidates(role)})")
        except Exception as exc:  # noqa: BLE001
            report(f"Gemini {role}", False, str(exc)[:200])
    probe = WORK / "check"
    probe.mkdir(parents=True, exist_ok=True)
    for prov in CFG["images"]["providers"] + ["thumbnail"]:
        if prov == "thumbnail":
            continue
        img = images.generate("a medieval peasant family eating soup by a hearth", probe / f"{prov}.png",
                              providers=[prov])
        if prov == "gemini" and img is None:
            log("INFO gemini images unavailable (optional, paid) - Cloudflare will be used")
            continue
        report(f"image provider {prov}", img is not None)
    from . import colab_remote
    if colab_remote.available():
        try:
            specs = colab_remote._Client().call("list_runtime_specs", {}, timeout=120).get("runtimeSpecs", [])
            ok_specs = [s["key"]["accelerator"] for s in specs if s.get("eligible") and s.get("key")]
            report("Colab API", any(a in ("A100", "L4") for a in ok_specs), f"(uygun: {ok_specs})")
        except Exception as exc:  # noqa: BLE001
            report("Colab API", False, str(exc)[:300])
    else:
        log("INFO Colab API secrets not set (optional) - images come from Cloudflare or a manually run notebook")
    try:
        wav = tts.synthesize("The village woke before the sun, and the mill wheel began to turn.", probe / "voice.wav")
        report(f"voice {CFG['voice']['provider']}", duration(wav) > 1)
    except Exception as exc:  # noqa: BLE001
        report(f"voice {CFG['voice']['provider']}", False, str(exc)[:300])
    if CFG["voice"]["provider"] == "elevenlabs":
        r = requests.get(f"https://api.elevenlabs.io/v1/voices/{CFG['voice']['elevenlabs_voice_id']}",
                         headers={"xi-api-key": env("ELEVENLABS_API_KEY")}, timeout=30)
        report("ElevenLabs voice", r.status_code == 200)
    if api_mode() or env("YT_REFRESH_TOKEN", required=False):
        try:
            ch = youtube.service().channels().list(part="snippet", mine=True).execute()
            report("YouTube channel", True, ch["items"][0]["snippet"]["title"])
        except Exception as exc:  # noqa: BLE001
            report("YouTube", False, str(exc)[:200])
    log(f"upload mode: {upload_mode()}")
    if not ok:
        sys.exit("Some checks failed")


# ---------------------------------------------------------------- selftest
def cmd_selftest(_args) -> None:
    if not MOCK:
        sys.exit("selftest must run with MOCK=1")
    record = produce("Life of a Medieval Miller", [])
    folder = WORK / record["slug"]
    for name in ["final.mp4", "thumbnail.jpg", "captions.srt", "metadata.md"] + [s["file"] for s in record["shorts"]]:
        assert (folder / name).exists(), name
    d = duration(folder / "final.mp4")
    write_json(folder / "selftest.json", {"duration": d, "record": record})
    log(f"SELFTEST OK: {d:.1f}s video, {len(record['shorts'])} shorts in {folder}")
    print(record["description"])
    print(next_slots(CFG["channel"]["publish_slots"], 3, dt.datetime.now(dt.timezone.utc), set()))


def main() -> None:
    p = argparse.ArgumentParser(prog="pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("topics")
    sub.add_parser("produce")
    sub.add_parser("colab-cleanup")
    r = sub.add_parser("review")
    r.add_argument("--issue", type=int, required=True)
    r.add_argument("--command", required=True)
    sub.add_parser("compile")
    sub.add_parser("selftest")
    sub.add_parser("check")
    sub.add_parser("auto-approve")
    ms = sub.add_parser("merge-state")
    ms.add_argument("base")
    ms.add_argument("mine")
    ms.add_argument("theirs")
    args = p.parse_args()
    {"colab-cleanup": lambda _a: __import__("pipeline.colab_remote", fromlist=["x"]).cleanup_all(),
     "topics": cmd_topics, "produce": cmd_produce, "review": cmd_review, "compile": cmd_compile,
     "selftest": cmd_selftest, "check": cmd_check, "auto-approve": cmd_auto_approve, "merge-state": cmd_merge_state}[args.cmd](args)


if __name__ == "__main__":
    main()
