"""ffmpeg rendering: narration assembly, Ken Burns slideshow, music mix, subtitles."""
from __future__ import annotations

import pathlib
import random
import re

from .config import CFG, ROOT, duration, log, run

GAP = 1.0  # seconds of silence between chapters


def assemble_narration(wavs: list[pathlib.Path], out: pathlib.Path) -> tuple[list[float], list[float]]:
    """Concatenate chapter WAVs with gaps, loudness-normalise. Returns (starts, durations)."""
    silence = out.parent / "_gap.wav"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", str(GAP), str(silence)])
    starts, durs, t, seq = [], [], 0.0, []
    for i, w in enumerate(wavs):
        d = duration(w)
        starts.append(t)
        durs.append(d)
        seq.append(w)
        t += d
        if i < len(wavs) - 1:
            seq.append(silence)
            t += GAP
    listfile = out.parent / "_narration_list.txt"
    listfile.write_text("".join(f"file '{p.resolve()}'\n" for p in seq))
    raw = out.parent / "_narration_raw.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(raw)])
    run(["ffmpeg", "-y", "-i", str(raw), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-ac", "1", str(out)])
    raw.unlink(missing_ok=True)
    return starts, durs


# Camera moves. 0-3 are centred; 4-7 drift toward a different part of the picture, so two shots
# cut from the same image look like different framings (a close look at the left, then the right...).
_FOCI = [(0.2, 0.35), (0.8, 0.35), (0.3, 0.7), (0.7, 0.65)]


def _zoompan(variant: int, frames: int) -> tuple[str, str, str]:
    p = f"(on/{max(frames - 1, 1)})"
    if variant == 0:   # slow zoom in
        return f"1+0.12*{p}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if variant == 1:   # slow zoom out
        return f"1.12-0.12*{p}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if variant == 2:   # pan left -> right
        return "1.12", f"(iw-iw/zoom)*{p}", "ih/2-(ih/zoom/2)"
    if variant == 3:   # pan right -> left
        return "1.12", f"(iw-iw/zoom)*(1-{p})", "ih/2-(ih/zoom/2)"
    fx, fy = _FOCI[(variant - 4) % len(_FOCI)]  # closer framing that slowly pushes toward a focus point
    return f"1.25+0.1*{p}", f"(iw-iw/zoom)*{fx}", f"(ih-ih/zoom)*{fy}"


N_VARIANTS = 8


def shots(segments: list[tuple[pathlib.Path, float, float]], groups: list[int],
          shot_seconds: float) -> tuple[list[tuple[pathlib.Path, float, float]], list[int]]:
    """Cut each image's time into shots of about `shot_seconds`, so the picture changes framing
    every ~10 s even when a new image only comes every ~30 s."""
    out, out_groups = [], []
    for (img, s, e), g in zip(segments, groups):
        n = max(1, round((e - s) / shot_seconds)) if shot_seconds > 0 else 1
        step = (e - s) / n
        for k in range(n):
            out.append((img, s + k * step, e if k == n - 1 else s + (k + 1) * step))
            out_groups.append(g)
    return out, out_groups


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"


def _title_style() -> tuple[str, str, str]:
    """(font file, text colour, border colour) for chapter titles: the brand's when configured."""
    if CFG.get("brand"):
        from . import brand
        return brand.font_path("title") or FONT, brand.ffcolor("paper", "#F6E7C1"), brand.ffcolor("ink", "#1A1208")
    return FONT, "0xF6E7C1", "0x1A1208"


def _enc() -> list[str]:
    v = CFG["video"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(v["crf"]), "-maxrate", str(v.get("maxrate", "6M")),
            "-bufsize", "12M", "-pix_fmt", "yuv420p", "-g", str(v["fps"] * 4), "-video_track_timescale", "90000"]


def ken_burns(img: pathlib.Path, frames: int, out: pathlib.Path, w: int, h: int, variant: int) -> None:
    fps = CFG["video"]["fps"]
    ss = CFG["video"]["zoom_supersample"]
    grain = CFG["video"].get("grain", 0)
    z, x, y = _zoompan(variant, frames)
    vf = (f"scale={w * ss}:{h * ss}:force_original_aspect_ratio=increase,crop={w * ss}:{h * ss},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps},vignette=angle=PI/6")
    if grain:
        vf += f",noise=alls={grain}:allf=t"
    run(["ffmpeg", "-y", "-i", str(img), "-vf", vf + ",format=yuv420p", "-frames:v", str(frames), "-r", str(fps),
         *_enc(), str(out)])


def _render_group(clips: list[pathlib.Path], frames: list[int], xf: int, out: pathlib.Path,
                  title: str | None, fade_in: bool, fade_out: bool) -> None:
    """Cross-dissolve clips (each clip except the last is xf frames longer), add title and edge fades."""
    fps = CFG["video"]["fps"]
    total = sum(frames)
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    chain, label, offset = [], "[0:v]", 0
    for k in range(1, len(clips)):
        offset += frames[k - 1]
        chain.append(f"{label}[{k}:v]xfade=transition=fade:duration={xf / fps:.4f}:offset={offset / fps:.4f}[x{k}]")
        label = f"[x{k}]"
    post = []
    font, fg, border = _title_style()
    if title and pathlib.Path(font).exists():
        tf = out.with_suffix(".title.txt")
        tf.write_text(title, encoding="utf-8")
        alpha = "if(lt(t,0.6),0,if(lt(t,1.6),t-0.6,if(lt(t,4.6),1,if(lt(t,5.6),5.6-t,0))))"
        post.append(f"drawtext=fontfile='{font}':textfile={tf.name}:fontsize=64:fontcolor={fg}:"
                    f"borderw=4:bordercolor={border}:x=90:y=h-210:expansion=none:alpha='{alpha}'")
    if fade_in:
        post.append("fade=t=in:st=0:d=0.6")
    if fade_out:
        post.append(f"fade=t=out:st={max(total / fps - 0.6, 0):.3f}:d=0.6")
    post.append("format=yuv420p")
    graph = ";".join(chain + [f"{label}{','.join(post)}[v]"])
    run(["ffmpeg", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-frames:v", str(total),
         "-r", str(fps), *_enc(), out.name], cwd=out.parent)


def slideshow(segments: list[tuple[pathlib.Path, float, float]], out: pathlib.Path, w: int, h: int,
              groups: list[int] | None = None, titles: dict[int, str] | None = None) -> None:
    """segments: (image, start_s, end_s) covering the timeline back-to-back.
    groups: group id per segment (e.g. chapter); dissolves inside a group, dip-to-black between groups."""
    fps = CFG["video"]["fps"]
    groups = groups or [0] * len(segments)
    titles = titles or {}
    segments, groups = shots(segments, groups, float(CFG["video"].get("shot_seconds", 0)))
    clip_dir = out.parent / f"_clips_{out.stem}"
    clip_dir.mkdir(exist_ok=True)
    rnd = random.Random(str(out))
    # exact frame budget per segment from the cumulative timeline (no audio/video drift)
    frames = [round(e * fps) - round(s * fps) for _, s, e in segments]
    order: list[int] = []
    for g in groups:
        if g not in order:
            order.append(g)
    parts, last_variant, last_img = [], -1, None
    for gi, g in enumerate(order):
        idx = [i for i, gg in enumerate(groups) if gg == g and frames[i] > 0]
        if not idx:
            continue
        fr = [frames[i] for i in idx]
        xf = max(1, int(min(CFG["video"].get("crossfade", 0.8) * fps, min(fr) // 3))) if len(idx) > 1 else 0
        clips = []
        for j, i in enumerate(idx):
            img = segments[i][0]
            pool = range(N_VARIANTS) if img != last_img else [v for v in range(N_VARIANTS) if (v >= 4) != (last_variant >= 4)]
            if img.parent.name == "cards":  # word cards: centred zoom only, so the word is never cropped
                pool = [0, 1]
            variant = rnd.choice([v for v in pool if v != last_variant] or list(pool))
            last_variant, last_img = variant, img
            clip = clip_dir / f"{i:04d}.mp4"
            ken_burns(img, fr[j] + (xf if j < len(idx) - 1 else 0), clip, w, h, variant)
            clips.append(clip)
        part = clip_dir / f"group{gi:03d}.mp4"
        _render_group(clips, fr, xf, part, titles.get(g), fade_in=True, fade_out=True)
        parts.append(part)
        log(f"render: group {gi + 1}/{len(order)}")
    listfile = clip_dir / "list.txt"
    listfile.write_text("".join(f"file '{c.name}'\n" for c in parts))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(out)])


def overlay_hook(video: pathlib.Path, clip: pathlib.Path, out: pathlib.Path, w: int, h: int,
                 max_seconds: float = 15.0) -> bool:
    """Lay a hand-made clip over the first seconds of the silent episode video, then dissolve back.
    Returns False (and leaves `out` unwritten) if anything goes wrong."""
    try:
        d = min(duration(clip), max_seconds, duration(video) - 1)
        if d < 2:
            return False
        fade = min(1.0, d / 4)
        graph = (f"[1:v]trim=0:{d:.3f},setpts=PTS-STARTPTS,fps={CFG['video']['fps']},"
                 f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,format=yuva420p,"
                 f"fade=t=out:st={d - fade:.3f}:d={fade:.3f}:alpha=1[hk];"
                 "[0:v][hk]overlay=eof_action=pass:shortest=0,format=yuv420p[v]")
        run(["ffmpeg", "-y", "-i", str(video), "-i", str(clip), "-filter_complex", graph, "-map", "[v]", "-an",
             *_enc(), str(out)])
        return out.exists() and abs(duration(out) - duration(video)) < 1.0
    except Exception as exc:  # noqa: BLE001 - the hook is a bonus: keep the plain video
        log(f"hook clip skipped: {str(exc)[:200]}")
        return False


def pick_music() -> pathlib.Path | None:
    files = [p for p in (ROOT / "assets" / "music").glob("*") if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg"}]
    return random.choice(files) if files else None


def mux(video: pathlib.Path, narration: pathlib.Path, out: pathlib.Path, music: bool = True,
        chapter_starts: list[float] | None = None, reveal_times: list[float] | None = None) -> None:
    """Video + narration + a quiet music bed (a file from assets/music if present, else a synthesised one).
    The bed is also ducked a little further whenever the narrator speaks."""
    track, premixed = (pick_music(), False) if music else (None, False)
    if music and track is None and CFG["video"].get("generated_music", True):
        from .music import make_bed
        try:
            track = make_bed(duration(narration), chapter_starts or [], out.parent / "_music.wav", seed=str(out.parent),
                             reveals=reveal_times or [])
            premixed = True
        except Exception as exc:  # noqa: BLE001 - music is optional, never lose the episode over it
            log(f"music bed failed, continuing without music: {str(exc)[:200]}")
            track = None
    cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(narration)]
    if track:
        vol = 1.0 if premixed else CFG["video"]["music_volume"]
        cmd += ([] if premixed else ["-stream_loop", "-1"]) + ["-i", str(track), "-filter_complex",
                f"[1:a]asplit=2[v][key];[2:a]volume={vol},aresample=44100,aformat=channel_layouts=stereo[m];"
                "[m][key]sidechaincompress=threshold=0.05:ratio=2:attack=200:release=1500[md];"
                "[v]aformat=channel_layouts=stereo[vs];"
                "[vs][md]amix=inputs=2:duration=first:normalize=0[a]",
                "-map", "0:v", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v", "-map", "1:a"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-shortest", "-movflags", "+faststart", str(out)]
    try:
        run(cmd)
    finally:
        if premixed and track:
            track.unlink(missing_ok=True)  # ~300 MB WAV: never leave it in the work cache


def _fmt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def cues_for(text: str, start: float, dur: float, max_words: int = 12) -> list[tuple[float, float, str]]:
    """Split text into short cues timed proportionally to character count."""
    pieces = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        words = sentence.split()
        for i in range(0, len(words), max_words):
            pieces.append(" ".join(words[i:i + max_words]))
    pieces = [p for p in pieces if p]
    total = sum(len(p) for p in pieces) or 1
    cues, t = [], start
    for p in pieces:
        d = dur * len(p) / total
        cues.append((t, t + d, p))
        t += d
    return cues


def cues_lines(text: str, start: float, dur: float, max_chars: int = 35) -> list[tuple[float, float, str]]:
    """One-line cues of at most `max_chars` characters, broken at sentence and clause ends where possible."""
    pieces: list[str] = []
    for clause in re.split(r"(?<=[.!?;:,])\s+", text.strip()):
        cur = ""
        for word in clause.split():
            if cur and len(cur) + 1 + len(word) > max_chars:
                pieces.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
        if cur:
            pieces.append(cur)
    total = sum(len(p) for p in pieces) or 1
    cues, t = [], start
    for p in pieces:
        d = dur * len(p) / total
        cues.append((t, t + d, p))
        t += d
    return cues


def write_srt(cues: list[tuple[float, float, str]], out: pathlib.Path) -> None:
    out.write_text("\n".join(f"{i}\n{_fmt(a)} --> {_fmt(b)}\n{txt}\n" for i, (a, b, txt) in enumerate(cues, 1)),
                   encoding="utf-8")


def _ass_colour(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def burn_subtitles(video: pathlib.Path, srt: pathlib.Path, out: pathlib.Path) -> None:
    style = ("FontName=DejaVu Sans,FontSize=13,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
             "BorderStyle=1,Outline=3,Shadow=0,Alignment=2,MarginV=70")
    extra = ""
    if CFG.get("brand"):  # the channel's font and accent colour, one bold line
        from . import brand
        fp = brand.font_path("title")
        if fp and not fp.startswith("/usr/share"):
            from PIL import ImageFont
            family = ImageFont.truetype(fp, 20).getname()[0]
            tmp_fonts = srt.parent / "_fonts"
            tmp_fonts.mkdir(exist_ok=True)
            (tmp_fonts / pathlib.Path(fp).name).write_bytes(pathlib.Path(fp).read_bytes())
            extra = ":fontsdir=_fonts"
            style = (f"FontName={family},FontSize=16,PrimaryColour={_ass_colour(brand.color('accent', '#E9A53C'))},"
                     f"OutlineColour={_ass_colour(brand.color('ink', '#1C1A17'))},BorderStyle=1,Outline=3,"
                     "Shadow=1,BackColour=&H80000000,Alignment=2,MarginV=110")
    # run inside the srt's folder so the filter path needs no escaping
    run(["ffmpeg", "-y", "-i", str(video.resolve()), "-vf", f"subtitles={srt.name}{extra}:force_style='{style}'",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "copy",
         "-movflags", "+faststart", str(out.resolve())], cwd=srt.parent)
