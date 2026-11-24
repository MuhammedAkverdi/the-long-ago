"""Quiet ambient music bed, synthesised here so it is free of any copyright claim.

Slow pad chords (sine partials with gentle detune), a little filtered "air", a soft chime at
each chapter start, then a light echo/low-pass in ffmpeg and loudness set well under the voice.
A seed per episode changes key, chord order and timing so episodes do not sound identical.
"""
from __future__ import annotations

import pathlib
import random
import wave

import numpy as np

from .config import CFG, log, run

SR = 44100
# chord progressions as semitone offsets from the key root (minor / modal, calm)
PROGRESSIONS = [
    [[0, 3, 7], [-4, 0, 3], [-7, -3, 0], [-2, 2, 5]],      # i  VI  III(low)  VII
    [[0, 3, 7], [5, 8, 12], [-4, 0, 3], [-2, 2, 5]],       # i  iv  VI  VII
    [[0, 3, 7, 10], [-4, 0, 3, 7], [5, 8, 12], [3, 7, 10]],
    [[0, 7, 12], [-2, 5, 10], [-4, 3, 8], [-5, 2, 7]],     # open fifths, modal
]


def _hz(semi: float, root: float) -> float:
    return root * 2 ** (semi / 12)


def _pad(freqs: list[float], n: int, rnd: random.Random) -> np.ndarray:
    t = np.arange(n) / SR
    out = np.zeros(n, dtype=np.float64)
    for f in freqs:
        for det in (-0.12, 0.0, 0.13):           # three slightly detuned voices -> soft chorus
            ph = rnd.random() * 6.283
            for k, amp in ((1, 1.0), (2, 0.28), (3, 0.09)):
                out += amp * np.sin(2 * np.pi * (f * k + det) * t + ph * k)
    # slow breathing of the chord
    out *= 0.75 + 0.25 * np.sin(2 * np.pi * t / (9 + 4 * rnd.random()) + rnd.random() * 6.283)
    return out / (len(freqs) * 3 * 1.4)


def _chime(root: float, rnd: random.Random) -> np.ndarray:
    n = int(SR * 4.5)
    t = np.arange(n) / SR
    f = _hz(rnd.choice([12, 19, 24]), root)
    tone = sum(a * np.sin(2 * np.pi * f * r * t) for r, a in ((1, 1.0), (2.76, 0.35), (5.4, 0.12)))
    return 0.55 * tone * np.exp(-t * 1.3) * np.minimum(1, t * 200)


def make_bed(total: float, chapter_starts: list[float], out: pathlib.Path, seed: str) -> pathlib.Path:
    """Write a stereo WAV `total` seconds long, already mixed to sit ~20-24 dB under the narration."""
    rnd = random.Random(seed)
    root = 110 * 2 ** (rnd.randint(-4, 3) / 12)   # around A2
    prog = rnd.choice(PROGRESSIONS)
    chord_s = rnd.uniform(14, 20)
    fade = 3.0
    n_total = int(total * SR) + SR
    raw = out.with_suffix(".raw.wav")
    starts = [int(s * SR) for s in chapter_starts[1:]]
    with wave.open(str(raw), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        block = int(chord_s * SR)
        nf = int(fade * SR)
        tail = np.zeros(nf)
        carry = np.zeros(0)
        pos, i = 0, 0
        while pos < n_total:
            freqs = [_hz(s, root) for s in prog[i % len(prog)]]
            if rnd.random() < 0.3:
                freqs.append(_hz(prog[i % len(prog)][0] - 12, root))  # occasional low root
            seg = _pad(freqs, block + nf, rnd)
            env = np.ones(block + nf)
            env[:nf] = np.linspace(0, 1, nf)
            env[-nf:] = np.linspace(1, 0, nf)
            seg *= env
            seg[:nf] += tail                    # crossfade with the previous chord
            tail = seg[block:].copy()
            body = seg[:block]
            # soft air: low-passed noise (moving average)
            noise = np.convolve(np.random.default_rng(rnd.randint(0, 1 << 30)).standard_normal(block),
                                np.ones(64) / 64, mode="same") * 0.6
            body = body + noise
            body[:len(carry)] += carry[:block]
            carry = carry[block:]
            for s in sorted(x for x in starts if pos <= x < pos + block):
                c = _chime(root, rnd)
                k = s - pos
                m = min(len(c), block - k)
                body[k:k + m] += c[:m]
                rest = c[m:]
                if len(rest):  # the chime rings on into the next block
                    if len(carry) < len(rest):
                        carry = np.pad(carry, (0, len(rest) - len(carry)))
                    carry[:len(rest)] += rest
            if pos == 0:  # fade the very start in
                body[: SR * 4] *= np.linspace(0, 1, SR * 4)
            pan = 0.15 * np.sin(2 * np.pi * (pos + np.arange(block)) / SR / 23)
            stereo = np.stack([body * (1 - pan), body * (1 + pan)], axis=1)
            wf.writeframes((np.clip(stereo * 0.25, -1, 1) * 32767).astype("<i2").tobytes())
            pos += block
            i += 1
    level = float(CFG["video"].get("music_lufs", -38))
    run(["ffmpeg", "-y", "-i", str(raw), "-af",
         f"aecho=0.8:0.7:120|260:0.35|0.22,lowpass=f=2600,highpass=f=60,"
         f"afade=t=out:st={max(total - 6, 0):.2f}:d=6,loudnorm=I={level}:TP=-9:LRA=7",
         "-t", f"{total:.3f}", "-ar", str(SR), "-ac", "2", str(out)])
    raw.unlink(missing_ok=True)
    log(f"music bed: {total:.0f}s, root {root:.1f} Hz, chord every {chord_s:.1f}s")
    return out
