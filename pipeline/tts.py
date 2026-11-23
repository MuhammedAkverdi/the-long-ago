"""Narration audio: ElevenLabs (default) or Gemini TTS. Output: 44.1 kHz mono WAV."""
from __future__ import annotations

import pathlib
import re
import wave

import requests

from .config import CFG, MOCK, PermanentError, env, log, retry, run

MAX_CHARS = 2400


def chunk_text(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks, cur = [], ""
    for s in sentences:
        while len(s) > max_chars:  # pathological very long sentence
            cut = s.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(s[:cut].strip())
            s = s[cut:].strip()
        if len(cur) + len(s) + 1 > max_chars and cur:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]


def _elevenlabs(text: str, prev: str, nxt: str, out: pathlib.Path) -> None:
    v = CFG["voice"]
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{v['elevenlabs_voice_id']}"

    def call():
        r = requests.post(url, params={"output_format": "mp3_44100_128"}, timeout=300,
                          headers={"xi-api-key": env("ELEVENLABS_API_KEY"), "Accept": "audio/mpeg"},
                          json={"text": text, "model_id": v["elevenlabs_model"],
                                "previous_text": prev[-1000:] or None, "next_text": nxt[:1000] or None,
                                "voice_settings": {"stability": v["stability"],
                                                   "similarity_boost": v["similarity_boost"],
                                                   "style": v.get("style", 0.0),
                                                   "use_speaker_boost": True}})
        if r.status_code in (400, 401, 402, 403, 404, 422):
            raise PermanentError(f"ElevenLabs HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code != 200:
            raise RuntimeError(f"ElevenLabs HTTP {r.status_code}: {r.text[:300]}")
        if len(r.content) < 1000:
            raise RuntimeError("ElevenLabs returned too little audio")
        return r.content

    out.write_bytes(retry(call, what="elevenlabs tts"))


def _gemini(text: str, out: pathlib.Path) -> None:
    from google.genai import types
    from .llm import client

    def call():
        resp = client().models.generate_content(
            model=CFG["models"]["tts"][0],
            contents=f"Read this in a calm, warm, unhurried documentary narrator voice:\n\n{text}",
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=CFG["voice"]["gemini_voice"])))))
        for part in resp.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                return part.inline_data.data
        raise RuntimeError("no audio in Gemini response")

    pcm = retry(call, what="gemini tts")
    with wave.open(str(out), "wb") as w:  # Gemini TTS returns 24 kHz 16-bit mono PCM
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm)


_kokoro_pipe = None


def _kokoro(text: str, out: pathlib.Path) -> None:
    """Kokoro-82M (Apache-2.0), runs on the CPU of the GitHub runner. 24 kHz output."""
    global _kokoro_pipe
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline
    v = CFG["voice"]
    if _kokoro_pipe is None:
        _kokoro_pipe = KPipeline(lang_code=v.get("kokoro_lang", "b"), repo_id="hexgrad/Kokoro-82M")
    pieces = []
    for result in _kokoro_pipe(text, voice=v.get("kokoro_voice", "bm_george"),
                               speed=float(v.get("kokoro_speed", 0.92)), split_pattern=r"\n+"):
        audio = result[2] if isinstance(result, tuple) else getattr(result, "audio", None)
        if audio is None:
            continue
        audio = audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio)
        pieces.append(audio)
        pieces.append(np.zeros(int(24000 * 0.25), dtype=audio.dtype))  # short breath between sentences groups
    if not pieces:
        raise RuntimeError("kokoro produced no audio")
    sf.write(str(out), np.concatenate(pieces), 24000)


def synthesize(text: str, out_wav: pathlib.Path) -> pathlib.Path:
    """Text -> WAV (44.1 kHz mono). Long text is split and stitched."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if out_wav.exists() and out_wav.stat().st_size > 1000:
        return out_wav  # resume support
    tmp = out_wav.parent / f"_{out_wav.stem}_parts"
    tmp.mkdir(exist_ok=True)
    if MOCK:
        secs = max(2.0, len(text.split()) / (CFG["script"]["words_per_minute"] / 60))
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=44100",
             "-t", f"{secs:.2f}", "-af", "volume=0.05", "-ac", "1", str(out_wav)])
        return out_wav
    chunks = chunk_text(text)
    parts = []
    for i, chunk in enumerate(chunks):
        provider = CFG["voice"]["provider"]
        raw = tmp / (f"{i:03d}.mp3" if provider == "elevenlabs" else f"{i:03d}.raw.wav")
        if not raw.exists():
            if provider == "elevenlabs":
                _elevenlabs(chunk, chunks[i - 1] if i else "", chunks[i + 1] if i + 1 < len(chunks) else "", raw)
            elif provider == "kokoro":
                _kokoro(chunk, raw)
            else:
                _gemini(chunk, raw)
        wav = tmp / f"{i:03d}.wav"
        run(["ffmpeg", "-y", "-i", str(raw), "-ar", "44100", "-ac", "1", str(wav)])
        parts.append(wav)
    listfile = tmp / "list.txt"
    listfile.write_text("".join(f"file '{p.name}'\n" for p in parts))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(out_wav)])
    log(f"tts: {out_wav.name} from {len(chunks)} chunk(s)")
    return out_wav
