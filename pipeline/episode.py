"""Produce one full episode (+ its Shorts) and package / upload it."""
from __future__ import annotations

import datetime as dt
import pathlib

from . import brand, colab, colab_remote, commons, images, manual, render, thumbnail, tts, youtube
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


def build_description(script: dict, chapter_starts: list[float], archival: list[dict] | None = None) -> str:
    parts = [script["description"].strip(), "", "Chapters:"]
    parts += [f"{ts(0 if i == 0 else t)} {c['heading']}" for i, (c, t) in enumerate(zip(script["chapters"], chapter_starts))]
    if script.get("sources"):
        parts += ["", "Sources and further reading:"]
        parts += [f"- {s['title']}" + (f": {s['url']}" if s.get("url") else "") for s in script["sources"]]
    if archival:
        parts += ["", "Period pictures (real historical images, Wikimedia Commons):"] + commons.credits(archival)
    note = AI_NOTE if not archival else AI_NOTE.replace(
        "illustrations are AI-generated artistic reconstructions, not historical images,",
        "most illustrations are AI-generated artistic reconstructions (the period pictures listed above are real),")
    parts += ["", note, "", "#history #dailylife #thelongago"]
    return "\n".join(parts)


CARD_WEIGHT = 0.3  # a word card stays on screen ~a third as long as a picture (one ~4 s shot)


def _segments(chapter_imgs: list[list[pathlib.Path]], starts: list[float], durs: list[float], total: float,
              weights: list[list[float]] | None = None):
    segs = []
    for i, imgs in enumerate(chapter_imgs):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(starts) else total
        wts = weights[i] if weights else [1.0] * len(imgs)
        span, acc = end - start, 0.0
        tot = sum(wts) or 1.0
        for k, img in enumerate(imgs):
            a = start + span * acc / tot
            acc += wts[k]
            segs.append((img, a, end if k == len(imgs) - 1 else start + span * acc / tot))
    return segs


def chapter_visuals(script: dict, chapter_imgs: list[list[pathlib.Path]], archival: list[dict],
                    folder: pathlib.Path) -> tuple[list[list[pathlib.Path]], list[list[float]]]:
    """Mix generated pictures with word cards and archival pictures. Returns (pictures, weights) per chapter."""
    cards_on = bool(CFG["video"].get("word_cards"))
    out_imgs, out_w = [], []
    for i, (c, imgs) in enumerate(zip(script["chapters"], chapter_imgs)):
        items: list[tuple[pathlib.Path, float]] = [(p, 1.0) for p in imgs]
        old = [f for f in archival if f["chapter"] == i]
        for j, f in enumerate(old):  # spread archival pictures through the chapter (never first)
            pos = min(len(items), 1 + (j + 1) * len(items) // (len(old) + 1))
            items.insert(pos, (folder / "archival" / f["path"], 1.0))
        if cards_on:
            for k, card in enumerate(sorted(c.get("cards") or [], key=lambda x: -x["after_image"])):
                path = brand.word_card(card["text"], folder / "cards" / f"ch{i:02d}_{k}.png")
                pos = min(len(items), max(1, card["after_image"] + 1))  # never the chapter's first shot
                items.insert(pos, (path, CARD_WEIGHT))
        out_imgs.append([p for p, _ in items])
        out_w.append([w for _, w in items])
    return out_imgs, out_w


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
    imgs = images.generate_many([str(p) for p in prompts][:4], sdir / "img", aspect="9:16", fallback=fallback)
    step = d / len(imgs)
    segs = [(img, k * step, d if k == len(imgs) - 1 else (k + 1) * step) for k, img in enumerate(imgs)]
    silent = sdir / "silent.mp4"
    render.slideshow(segs, silent, 1080, 1920)
    muxed = sdir / "muxed.mp4"
    render.mux(silent, wav, muxed, music=True)
    srt = sdir / "captions.srt"
    render.write_srt(render.cues_lines(short["narration"], 0, d, max_chars=35), srt)
    final = folder / f"short{idx}.mp4"
    render.burn_subtitles(muxed, srt, final)
    title = short["title"] if "#shorts" in short["title"].lower() else f"{short['title'][:88]} #shorts"
    return {"file": final.name, "title": title[:100]}


def pick_style(recent_styles: list[str]) -> str:
    """Rotate through video.style_rotation (an A/B test of picture styles); default painterly."""
    rotation = [s for s in CFG["video"].get("style_rotation") or [] if s in (CFG["video"].get("styles") or {})]
    if not rotation:
        return "painterly"
    last = recent_styles[-1] if recent_styles else None
    return rotation[(rotation.index(last) + 1) % len(rotation)] if last in rotation else rotation[0]


def apply_style(script: dict) -> None:
    """Use this episode's picture style for every generated image (also the Colab path)."""
    style = script.get("visual_style")
    styles = CFG["video"].get("styles") or {}
    if style in styles:
        CFG["video"]["image_style"] = " ".join(str(styles[style]).split())


def produce(topic: str, recent_structures: list[str], slug: str | None = None,
            recent_styles: list[str] | None = None) -> dict:
    """Returns an episode record. All files live in WORK/<slug>/."""
    slug = slug or f"{dt.date.today():%Y%m%d}-{slugify(topic, 40)}"  # a fixed slug lets a paused episode resume
    folder = WORK / slug
    folder.mkdir(parents=True, exist_ok=True)
    w, h = CFG["video"]["width"], CFG["video"]["height"]

    script_path = folder / "script.json"
    if script_path.exists():
        script = read_json(script_path)
    else:
        script = build(topic, pick_structure(recent_structures), cache=folder / "steps",
                       style=pick_style(recent_styles or []))
        write_json(script_path, script)
    apply_style(script)

    manual.apply(folder, slug, script)  # hand-made opening pictures / thumbnail, if any

    remote = None
    if (colab.offer(folder, slug, script) and colab.pending_paths(folder)
            and "images.zip" not in colab._assets(f"colab-{slug}")):  # Colab draws while narration is voiced
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

    archival: list[dict] = []
    if CFG["video"].get("archival_images") and script.get("archival_requests"):
        log("archival pictures")
        archival = commons.fetch(script["archival_requests"], folder / "archival")
    visuals, weights = chapter_visuals(script, chapter_imgs, archival, folder)

    log("render")
    silent = folder / "silent.mp4"
    segs = _segments(visuals, starts, durs, total, weights)
    groups = [i for i, imgs in enumerate(visuals) for _ in imgs]
    titles = ({i: c["heading"] for i, c in enumerate(script["chapters"]) if i > 0}
              if CFG["video"].get("chapter_titles", True) else {})
    render.slideshow(segs, silent, w, h, groups=groups, titles=titles)
    hook = manual.hook(slug)
    if hook:
        hooked = folder / "silent_hook.mp4"
        if render.overlay_hook(silent, hook, hooked, w, h):
            hooked.replace(silent)
            log(f"hook clip laid over the opening ({hook.name})")
    final = folder / "final.mp4"
    reveals = [s + d * c["reveal"] for c, s, d in zip(script["chapters"], starts, durs)
               if c.get("reveal") is not None] if CFG["video"].get("music_reveals") else []
    render.mux(silent, narration, final, chapter_starts=starts, reveal_times=reveals)

    cues = []
    for c, s, d in zip(script["chapters"], starts, durs):
        cues += render.cues_for(c["narration"], s, d)
    render.write_srt(cues, folder / "captions.srt")

    all_imgs = [p for imgs in chapter_imgs for p in imgs]
    thumbnail.make(script["thumbnail_prompt"], script["thumbnail_text"], folder,
                   fallback=all_imgs[0] if all_imgs else None)
    description = build_description(script, starts, archival)
    (folder / "metadata.md").write_text(
        f"# {script['title']}\n\n"
        + ("## Title options\n\n" + "".join(f"- {t}\n" for t in script["title_options"]) + "\n"
           if script.get("title_options") else "")
        + f"## Description\n\n{description}\n\n## Tags\n\n{', '.join(script['tags'])}\n",
        encoding="utf-8")

    shorts = []
    for i, s in enumerate(script.get("shorts", [])):
        try:
            shorts.append(make_short(s, i, folder, fallback=all_imgs[i::max(1, len(script["shorts"]))][:6] or None))
        except Exception as exc:  # noqa: BLE001 - a broken Short must not kill the episode
            log(f"short {i} failed: {exc}")

    record = {"slug": slug, "type": "episode", "topic": topic, "title": script["title"],
              "title_options": script.get("title_options", []), "structure": script["structure"], "minutes": round(total / 60, 1),
              "visual_style": script.get("visual_style", "painterly"), "hook_options": script.get("hook_options", []),
              "archival": len(archival),
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
