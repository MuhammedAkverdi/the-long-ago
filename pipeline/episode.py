"""Produce one full episode (+ its Shorts) and package / upload it."""
from __future__ import annotations

import datetime as dt
import pathlib

from . import colab, colab_remote, images, render, thumbnail, tts, youtube
from .config import CFG, WORK, duration, log, read_json, run, slugify, write_json
from .script import build, pick_structure

AI_NOTE = ("Made with AI: illustrations are AI-generated artistic reconstructions, not historical images, "
           "and the narration is AI-voiced. The script is AI-assisted, based on published historical "
           "scholarship and checked by an automated fact-check pass; it may still contain mistakes, so please "
           "check important details in published scholarship and tell us in the comments if you spot an error.")


def ts(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_description(script: dict, chapter_starts: list[float]) -> str:
    parts = [script["description"].strip(), "", "Chapters:"]
    parts += [f"{ts(0 if i == 0 else t)} {c['heading']}" for i, (c, t) in enumerate(zip(script["chapters"], chapter_starts))]
    if script.get("sources"):
        parts += ["", "Sources and further reading:"]
        parts += [f"- {s['title']}" + (f": {s['url']}" if s.get("url") else "") for s in script["sources"]]
    parts += ["", AI_NOTE, "", "#history #dailylife #thelongago"]
    return "\n".join(parts)


def _segments(chapter_imgs: list[list[pathlib.Path]], starts: list[float], durs: list[float], total: float):
    segs = []
    for i, imgs in enumerate(chapter_imgs):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(starts) else total
        step = (end - start) / len(imgs)
        for k, img in enumerate(imgs):
            segs.append((img, start + k * step, end if k == len(imgs) - 1 else start + (k + 1) * step))
    return segs


def make_short(short: dict, idx: int, folder: pathlib.Path, fallback: list[pathlib.Path] | None = None) -> dict:
    sdir = folder / f"short{idx}"
    sdir.mkdir(parents=True, exist_ok=True)
    wav = tts.synthesize(short["narration"], sdir / "voice.wav")
    d = duration(wav)
    if d > 58:  # keep Shorts under a minute when a light speed-up is enough
        tempo = min(d / 58, 1.12)
        fast = sdir / "voice_fast.wav"
        run(["ffmpeg", "-y", "-i", str(wav), "-af", f"atempo={tempo:.3f}", str(fast)])
        wav, d = fast, duration(fast)
    d = min(d, 170.0)
    prompts = short.get("image_prompts") or ["an ordinary household scene"]
    imgs = images.generate_many([str(p) for p in prompts][:6], sdir / "img", aspect="9:16", fallback=fallback)
    step = d / len(imgs)
    segs = [(img, k * step, d if k == len(imgs) - 1 else (k + 1) * step) for k, img in enumerate(imgs)]
    silent = sdir / "silent.mp4"
    render.slideshow(segs, silent, 1080, 1920)
    muxed = sdir / "muxed.mp4"
    render.mux(silent, wav, muxed, music=True)
    srt = sdir / "captions.srt"
    render.write_srt(render.cues_for(short["narration"], 0, d, max_words=5), srt)
    final = folder / f"short{idx}.mp4"
    render.burn_subtitles(muxed, srt, final)
    title = short["title"] if "#shorts" in short["title"].lower() else f"{short['title'][:88]} #shorts"
    return {"file": final.name, "title": title[:100]}


def produce(topic: str, recent_structures: list[str], slug: str | None = None) -> dict:
    """Returns an episode record. All files live in WORK/<slug>/."""
    slug = slug or f"{dt.date.today():%Y%m%d}-{slugify(topic, 40)}"  # a fixed slug lets a paused episode resume
    folder = WORK / slug
    folder.mkdir(parents=True, exist_ok=True)
    w, h = CFG["video"]["width"], CFG["video"]["height"]

    script_path = folder / "script.json"
    if script_path.exists():
        script = read_json(script_path)
    else:
        script = build(topic, pick_structure(recent_structures), cache=folder / "steps")
        write_json(script_path, script)

    remote = None
    if colab.offer(folder, slug, script) and colab.pending_paths(folder):  # Colab draws while narration is voiced
        remote = colab_remote.start()  # headless Colab via the Colab API, when its secrets are set
    try:
        return _produce_rest(topic, script, folder, slug, w, h, remote)
    finally:
        if remote is not None:
            remote.stop()  # never leave a paid GPU running, whatever happened above


def _produce_rest(topic: str, script: dict, folder: pathlib.Path, slug: str, w: int, h: int, remote) -> dict:

    log("narration")
    wavs = [tts.synthesize(c["narration"], folder / "audio" / f"ch{i:02d}.wav") for i, c in enumerate(script["chapters"])]
    narration = folder / "narration.wav"
    starts, durs = render.assemble_narration(wavs, narration)
    total = duration(narration)

    colab.collect(folder, slug, remote)

    log("thumbnail")  # made before the chapter images so it gets the day's image allowance first
    thumbnail.raw_image(script["thumbnail_prompt"], folder)

    log("images")
    chapter_imgs = []
    for i, c in enumerate(script["chapters"]):
        chapter_imgs.append(images.generate_many(c["image_prompts"], folder / "img" / f"ch{i:02d}"))

    log("render")
    silent = folder / "silent.mp4"
    segs = _segments(chapter_imgs, starts, durs, total)
    groups = [i for i, imgs in enumerate(chapter_imgs) for _ in imgs]
    titles = ({i: c["heading"] for i, c in enumerate(script["chapters"]) if i > 0}
              if CFG["video"].get("chapter_titles", True) else {})
    render.slideshow(segs, silent, w, h, groups=groups, titles=titles)
    final = folder / "final.mp4"
    render.mux(silent, narration, final)

    cues = []
    for c, s, d in zip(script["chapters"], starts, durs):
        cues += render.cues_for(c["narration"], s, d)
    render.write_srt(cues, folder / "captions.srt")

    all_imgs = [p for imgs in chapter_imgs for p in imgs]
    thumbnail.make(script["thumbnail_prompt"], script["thumbnail_text"], folder,
                   fallback=all_imgs[0] if all_imgs else None)
    description = build_description(script, starts)
    (folder / "metadata.md").write_text(
        f"# {script['title']}\n\n## Description\n\n{description}\n\n## Tags\n\n{', '.join(script['tags'])}\n",
        encoding="utf-8")

    shorts = []
    for i, s in enumerate(script.get("shorts", [])):
        try:
            shorts.append(make_short(s, i, folder, fallback=all_imgs[i::max(1, len(script["shorts"]))][:6] or None))
        except Exception as exc:  # noqa: BLE001 - a broken Short must not kill the episode
            log(f"short {i} failed: {exc}")

    record = {"slug": slug, "type": "episode", "topic": topic, "title": script["title"],
              "structure": script["structure"], "minutes": round(total / 60, 1),
              "fact_check": script.get("fact_check", "-"),
              "description": description, "tags": script["tags"] + CFG["channel"]["default_tags"],
              "shorts": shorts, "status": "produced", "created": dt.datetime.now(dt.timezone.utc).isoformat()}
    write_json(folder / "record.json", record)
    log(f"episode ready: {script['title']} ({record['minutes']} min, {len(shorts)} shorts)")
    return record


def upload_all(record: dict, folder: pathlib.Path, persist=lambda: None) -> dict:
    """API mode: upload main video + shorts as PRIVATE with AI disclosure. Skips anything already uploaded."""
    if not record.get("video_id"):
        record["video_id"] = youtube.upload(folder / "final.mp4", record["title"], record["description"], record["tags"])
        persist()
    if not record.get("thumbnail_set") and (folder / "thumbnail.jpg").exists():
        record["thumbnail_set"] = youtube.set_thumbnail(record["video_id"], folder / "thumbnail.jpg")
        persist()
    if not record.get("captions_set") and (folder / "captions.srt").exists():
        record["captions_set"] = youtube.upload_captions(record["video_id"], folder / "captions.srt")
        persist()
    vid = record["video_id"]
    for s in record.get("shorts", []):
        if s.get("video_id"):
            continue
        desc = (f"Full episode: https://youtu.be/{vid}\n\n{record['title']}\n\n{AI_NOTE}\n\n#history #shorts")
        s["video_id"] = youtube.upload(folder / s["file"], s["title"], desc, record["tags"][:10])
        persist()
    record["status"] = "uploaded"
    return record
