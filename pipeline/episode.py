"""Produce one full episode (+ its Shorts) and package / upload it."""
from __future__ import annotations

import datetime as dt
import pathlib
import wave

from . import brand, colab, colab_remote, commons, images, manual, motion, render, storyboard, thumbnail, tts, youtube
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
        if cards_on:  # cards first: their positions count generated pictures only
            for k, card in enumerate(sorted(c.get("cards") or [], key=lambda x: -x["after_image"])):
                path = brand.word_card(card["text"], folder / "cards" / f"ch{i:02d}_{k}.png")
                pos = min(len(items), max(1, card["after_image"] + 1))  # never the chapter's first shot
                items.insert(pos, (path, CARD_WEIGHT))
        old = [f for f in archival if f["chapter"] == i]
        for j, f in enumerate(old):  # spread archival pictures through the chapter (never first)
            pos = min(len(items), 1 + (j + 1) * len(items) // (len(old) + 1))
            items.insert(pos, (folder / "archival" / f["path"], 1.0))
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


def motion_mode() -> bool:
    """The fast animated format (HyperFrames scenes), see pipeline/motion.py."""
    return CFG["video"].get("engine") == "motion"


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
                       style="doodle" if motion_mode() else pick_style(recent_styles or []))
        write_json(script_path, script)
    apply_style(script)

    manual.apply(folder, slug, script, openings=not motion_mode())  # hand-made pictures / thumbnail, if any
    if motion_mode():
        return _produce_motion(topic, script, folder, slug)

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
    return _package(topic, script, folder, slug, starts, total, archival, all_imgs)


def _package(topic: str, script: dict, folder: pathlib.Path, slug: str, starts: list[float], total: float,
             archival: list[dict], all_imgs: list[pathlib.Path]) -> dict:
    """Thumbnail, metadata, Shorts and the episode record (final.mp4 and captions.srt already exist)."""
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
              "archival": len(archival), "format": "motion" if motion_mode() else "slideshow",
              "fact_check": script.get("fact_check", "-"),
              "description": description, "tags": script["tags"] + CFG["channel"]["default_tags"],
              "shorts": shorts, "status": "produced", "created": dt.datetime.now(dt.timezone.utc).isoformat()}
    write_json(folder / "record.json", record)
    log(f"episode ready: {script['title']} ({record['minutes']} min, {len(shorts)} shorts)")
    return record


BEAT_GAP, QUESTION_GAP = 0.05, 0.3  # extra pause between spoken beats (Kokoro already ends each with ~0.25 s)


def _wav_frames(path: pathlib.Path) -> tuple[bytes, tuple]:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes()), (w.getnchannels(), w.getsampwidth(), w.getframerate())


def _chapter_audio(beats: list[dict], ci: int, folder: pathlib.Path) -> tuple[pathlib.Path, list[tuple[float, float]]]:
    """Voice every beat separately (exact scene timing), then join them. Returns (wav, [(start, length)])."""
    out = folder / "audio" / f"ch{ci:02d}.wav"
    times_path = folder / "audio" / f"ch{ci:02d}.times.json"
    if out.exists() and times_path.exists():
        return out, [tuple(t) for t in read_json(times_path)]
    frames, params, times, t = [], None, [], 0.0
    for bi, b in enumerate(beats):
        wav = tts.synthesize(b["text"], folder / "audio" / f"ch{ci:02d}" / f"b{bi:03d}.wav")
        data, prm = _wav_frames(wav)
        if params is None:
            params = prm
        elif prm != params:  # never expected (tts always writes 44.1 kHz mono 16-bit), but never mix formats
            fixed = wav.with_suffix(".fix.wav")
            run(["ffmpeg", "-y", "-i", str(wav), "-ar", str(params[2]), "-ac", str(params[0]), "-sample_fmt", "s16", str(fixed)])
            data, _ = _wav_frames(fixed)
        bytes_per_sec = params[0] * params[1] * params[2]
        d = len(data) / bytes_per_sec
        times.append((round(t, 3), round(d, 3)))
        gap = QUESTION_GAP if b["text"].rstrip().endswith("?") else BEAT_GAP
        pad = b"\x00" * (int(gap * params[2]) * params[0] * params[1])
        frames += [data, pad]
        t += d + gap
    tmp = out.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(params[0])
        w.setsampwidth(params[1])
        w.setframerate(params[2])
        w.writeframes(b"".join(frames))
    tmp.replace(out)
    write_json(times_path, times)
    return out, times


def _assign_archival(board: list[list[dict]], archival: list[dict]) -> dict[tuple[int, int], pathlib.Path]:
    """Put each real period picture on a picture beat of its chapter (never the chapter's first one)."""
    taken: dict[tuple[int, int], pathlib.Path] = {}
    for ci, beats in enumerate(board):
        slots = [bi for bi, b in enumerate(beats) if b["scene"]["t"] == "picture"][1:]
        mine = [f for f in archival if f["chapter"] == ci]
        for j, f in enumerate(mine[:len(slots)]):
            bi = slots[(j + 1) * len(slots) // (len(mine) + 1)] if len(mine) < len(slots) else slots[j]
            if (ci, bi) not in taken:
                taken[(ci, bi)] = f["_path"]
    return taken


def _produce_motion(topic: str, script: dict, folder: pathlib.Path, slug: str) -> dict:
    board_path = folder / "storyboard.json"
    if board_path.exists():
        board = read_json(board_path)
    else:
        log("storyboard")
        board = storyboard.build(script)
        write_json(board_path, board)

    log("narration (beat by beat)")
    wavs, beat_times = [], []
    for ci, beats in enumerate(board):
        wav, times = _chapter_audio(beats, ci, folder)
        wavs.append(wav)
        beat_times.append(times)
    narration = folder / "narration.wav"
    starts, durs = render.assemble_narration(wavs, narration)
    total = duration(narration)
    spans = [(starts[i + 1] if i + 1 < len(starts) else total) - starts[i] for i in range(len(starts))]

    log("thumbnail")  # before the scene pictures so it gets the day's image allowance first
    thumbnail.raw_image(script["thumbnail_prompt"], folder)

    archival: list[dict] = []
    if CFG["video"].get("archival_images") and script.get("archival_requests"):
        log("archival pictures")
        archival = commons.fetch(script["archival_requests"], folder / "archival")
    for f in archival:
        f["_path"] = folder / "archival" / f["path"]
    old = _assign_archival(board, archival)

    log("pictures")
    pictures: list[dict[int, pathlib.Path]] = []
    all_imgs: list[pathlib.Path] = []
    for ci, (c, beats) in enumerate(zip(script["chapters"], board)):
        prompts = storyboard.picture_prompts(c, beats)
        idx = [bi for bi, b in enumerate(beats) if b["scene"]["t"] == "picture"]
        need = [(bi, p) for bi, p in zip(idx, prompts) if (ci, bi) not in old]
        made = images.generate_many([p for _, p in need], folder / "img" / f"ch{ci:02d}") if need else []
        pics = {bi: path for (bi, _), path in zip(need, made)}
        pics.update({bi: path for (cj, bi), path in old.items() if cj == ci})
        pictures.append(pics)
        all_imgs += made
    used_archival = [f for f in archival if any(f["_path"] == p for p in old.values())]
    for f in archival:
        f.pop("_path", None)

    log("render (motion)")
    silent = folder / "silent.mp4"
    headings = [c["heading"] for c in script["chapters"]] if CFG["video"].get("chapter_titles", True) else [None] * len(board)
    cues = motion.render_episode(board, beat_times, spans, pictures, headings, folder, silent)
    voice = narration
    if (CFG.get("motion") or {}).get("sound_effects", True):
        fx = motion.sfx_track(cues, total, folder / "sfx.wav")
        voice = folder / "narration_fx.wav"
        vol = float((CFG.get("motion") or {}).get("sfx_volume", 0.3))
        run(["ffmpeg", "-y", "-i", str(narration), "-i", str(fx), "-filter_complex",
             f"[1:a]volume={vol}[s];[0:a][s]amix=inputs=2:duration=first:normalize=0[a]", "-map", "[a]",
             "-ar", "44100", "-ac", "1", str(voice)])
    final = folder / "final.mp4"
    reveals = [s + d * c["reveal"] for c, s, d in zip(script["chapters"], starts, durs)
               if c.get("reveal") is not None] if CFG["video"].get("music_reveals") else []
    render.mux(silent, voice, final, chapter_starts=starts, reveal_times=reveals)

    cues_srt = []
    for ci, beats in enumerate(board):
        for b, (bs, bd) in zip(beats, beat_times[ci]):
            cues_srt += render.cues_for(b["text"], starts[ci] + bs, bd)
    render.write_srt(cues_srt, folder / "captions.srt")
    return _package(topic, script, folder, slug, starts, total, [{k: v for k, v in f.items()} for f in used_archival],
                    all_imgs)


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
