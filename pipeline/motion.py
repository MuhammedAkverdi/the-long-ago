"""2.5D camera moves on still pictures: depth parallax plus light atmosphere (dust, embers, mist, flicker).

The picture's depth is estimated once with Depth Anything V2 *Small* (Apache-2.0; the Base/Large
checkpoints are non-commercial and must not be used). Each frame the camera zooms/pans as before,
but near things move more than far things, so the still reads as a 3D scene. Everything runs on CPU.
If the depth model cannot be loaded the same renderer runs with flat depth (a plain camera move),
and if this module fails altogether render.py falls back to the old ffmpeg Ken Burns.
"""
from __future__ import annotations

import pathlib
import random
import subprocess

import numpy as np

from .config import CFG, log

DEPTH_MODEL = "depth-anything/Depth-Anything-V2-Small-hf"  # Apache-2.0. Never Base/Large (CC-BY-NC).
_depth_pipe = None
_depth_off = False

# (zoom start, zoom end, centre x start/end, centre y start/end, parallax sweep, dolly)
# centre values are fractions of the free room around the view (0 = left/top edge, 1 = right/bottom)
MOVES = [
    (1.00, 1.10, .5, .5, .5, .5, 1, .10),    # slow push in, dolly
    (1.10, 1.00, .5, .5, .5, .5, -1, -.08),  # slow pull out
    (1.08, 1.08, .1, .9, .5, .5, 1, 0),      # pan left -> right
    (1.08, 1.08, .9, .1, .5, .5, -1, 0),     # pan right -> left
    (1.18, 1.26, .2, .25, .35, .3, 1, .08),  # closer look, upper left
    (1.18, 1.26, .8, .75, .35, .3, -1, .08), # closer look, upper right
    (1.18, 1.26, .3, .35, .7, .65, 1, .06),  # closer look, lower left
    (1.18, 1.26, .7, .65, .65, .7, -1, .06), # closer look, lower right
]

ATMOS_WORDS = {
    "embers": ("forge", "furnace", "kiln", "smith", "fire", "hearth", "glassblow", "molten", "torch", "bonfire"),
    "flicker": ("candle", "lamp", "fire", "hearth", "furnace", "torch", "firelight", "night", "lantern", "forge"),
    "mist": ("dawn", "mist", "fog", "river", "sea", "lagoon", "canal", "harbour", "harbor", "marsh", "morning", "rain"),
    "dust": ("workshop", "barn", "street", "market", "road", "sunbeam", "sunlight", "mill", "dust", "field", "kitchen"),
}


def atmosphere_for(prompt: str) -> set[str]:
    p = (prompt or "").lower()
    kinds = {k for k, words in ATMOS_WORDS.items() if any(w in p for w in words)}
    return kinds or {"dust"}


def _load_depth():
    global _depth_pipe, _depth_off
    if _depth_pipe is None and not _depth_off:
        try:
            from transformers import pipeline as hf_pipeline
            _depth_pipe = hf_pipeline("depth-estimation", model=DEPTH_MODEL, device="cpu")
            log(f"motion: depth model {DEPTH_MODEL} loaded")
        except Exception as exc:  # noqa: BLE001 - no depth model: flat camera moves instead
            _depth_off = True
            log(f"motion: depth model unavailable, using flat moves ({str(exc)[:160]})")
    return _depth_pipe


def depth(img: pathlib.Path) -> np.ndarray | None:
    """Near = 1, far = 0, float32 at the picture's size. Cached next to the picture as .depth.png."""
    import cv2
    cache = img.with_name(img.stem + ".depth.png")
    if cache.exists():
        d = cv2.imread(str(cache), cv2.IMREAD_UNCHANGED)
        if d is not None:
            return d.astype(np.float32) / 65535.0
    if not CFG["video"].get("parallax", True):
        return None
    pipe = _load_depth()
    if pipe is None:
        return None
    try:
        from PIL import Image
        pic = Image.open(img).convert("RGB")
        out = np.asarray(pipe(pic)["depth"], dtype=np.float32)
        if out.ndim == 3:
            out = out[..., 0]
        out = cv2.resize(out, pic.size, interpolation=cv2.INTER_CUBIC)
        lo, hi = np.percentile(out, 2), np.percentile(out, 98)
        out = np.clip((out - lo) / max(hi - lo, 1e-6), 0, 1)
        sigma = max(2.0, 0.012 * max(pic.size))  # soften edges: stretched, never torn
        out = cv2.GaussianBlur(out, (0, 0), sigma)
        cv2.imwrite(str(cache), (out * 65535).astype(np.uint16))
        return out
    except Exception as exc:  # noqa: BLE001
        log(f"motion: depth failed for {img.name} ({str(exc)[:120]})")
        return None


def _ease(t: np.ndarray | float):
    return t * t * (3 - 2 * t)


class _Particles:
    def __init__(self, kind: str, w: int, h: int, rnd: random.Random):
        self.kind, self.w, self.h = kind, w, h
        n = 70 if kind == "dust" else 28
        g = np.random.default_rng(rnd.randint(0, 1 << 30))
        self.x = g.uniform(0, w, n)
        self.y = g.uniform(0, h, n) if kind == "dust" else g.uniform(h * .45, h * 1.05, n)
        self.r = g.uniform(1.0, 2.6, n) * (w / 1920)
        self.a = g.uniform(.15, .5, n)
        self.vx = g.normal(0, .25, n) * (w / 1920)
        self.vy = (g.normal(0, .15, n) if kind == "dust" else -g.uniform(.8, 2.2, n)) * (w / 1920)
        self.ph = g.uniform(0, 6.28, n)
        self.life = g.uniform(0, 1, n)
        self.g = g

    def step(self, layer: np.ndarray, f: int) -> None:
        import cv2
        self.x += self.vx + .3 * np.sin(f * .05 + self.ph) * (self.w / 1920)
        self.y += self.vy
        if self.kind == "embers":
            self.life += .01
            gone = (self.y < -10) | (self.life > 1)
            self.y[gone] = self.g.uniform(self.h * .6, self.h * 1.02, gone.sum())
            self.x[gone] = self.g.uniform(0, self.w, gone.sum())
            self.life[gone] = 0
        self.x %= self.w
        self.y = np.where(self.y > self.h + 10, -5, self.y)
        for x, y, r, a, ph, life in zip(self.x, self.y, self.r, self.a, self.ph, self.life):
            tw = .6 + .4 * np.sin(f * .08 + ph)
            if self.kind == "embers":
                fade = min(1.0, life * 6) * (1 - life)
                col = (40 * a * fade * 4, 140 * a * fade * 4, 255 * a * fade * 4)  # BGR orange
            else:
                v = 200 * a * tw
                col = (v * .85, v * .95, v)
            cv2.circle(layer, (int(x), int(y)), max(1, int(r)), col, -1, lineType=cv2.LINE_AA)


def render(img: pathlib.Path, frames: int, out: pathlib.Path, w: int, h: int, variant: int,
           atmos: set[str] | None = None, seed: str = "") -> None:
    """Write `frames` frames of a 2.5D camera move over `img` to `out` (same encoder settings as render.py)."""
    import cv2
    from .render import _enc
    fps = CFG["video"]["fps"]
    rnd = random.Random(f"{img}{seed}{variant}")
    pic = cv2.imread(str(img), cv2.IMREAD_COLOR)
    if pic is None:
        raise RuntimeError(f"cannot read {img}")
    over = 1.25  # source is prepared 25% larger than the frame: room for parallax and zoom
    sw, sh = int(w * over), int(h * over)
    scale = max(sw / pic.shape[1], sh / pic.shape[0])
    rw, rh = max(sw, round(pic.shape[1] * scale)), max(sh, round(pic.shape[0] * scale))
    src = cv2.resize(pic, (rw, rh), interpolation=cv2.INTER_LANCZOS4 if scale > 1 else cv2.INTER_AREA)
    ox, oy = (rw - sw) // 2, (rh - sh) // 2
    src = np.ascontiguousarray(src[oy:oy + sh, ox:ox + sw])
    d = depth(img)
    if d is not None:
        d = cv2.resize(d, (rw, rh), interpolation=cv2.INTER_LINEAR)[oy:oy + sh, ox:ox + sw]
    else:
        d = np.full((sh, sw), .5, np.float32)
    d = np.ascontiguousarray(d - float(np.median(d)), dtype=np.float32)  # focus plane = typical depth

    z0, z1, cx0, cx1, cy0, cy1, sweep, dolly = MOVES[variant % len(MOVES)]
    amp = float(CFG["video"].get("parallax_strength", 0.018)) * sw  # px of extra shift for nearest vs focus
    # maps are computed at half size and upscaled (smooth fields; 4x less arithmetic)
    hw, hh = w // 2, h // 2
    gu, gv = np.meshgrid((np.arange(hw, dtype=np.float32) + .5) / hw - .5,
                         (np.arange(hh, dtype=np.float32) + .5) / hh - .5)
    view_w, view_h = w, h  # at zoom 1 the frame shows w x h source pixels (source is 25% larger)

    atmos = atmos or set()
    parts = [_Particles(k, w, h, rnd) for k in ("dust", "embers") if k in atmos]
    mist = None
    if "mist" in atmos:
        g = np.random.default_rng(rnd.randint(0, 1 << 30))
        small = g.random((h // 48 + 2, (2 * w) // 48 + 2)).astype(np.float32)
        mist = cv2.resize(small, (2 * w, h), interpolation=cv2.INTER_CUBIC)
        mist = cv2.GaussianBlur(mist, (0, 0), 25)
        mist = (np.clip((mist - .35) * 1.6, 0, 1) * 38).astype(np.uint8)
        mist = cv2.cvtColor(mist, cv2.COLOR_GRAY2BGR)
    flick = "flicker" in atmos
    fph = [rnd.uniform(0, 6.28) for _ in range(3)]

    vf = "vignette=angle=PI/6"
    grain = CFG["video"].get("grain", 0)
    if grain:
        vf += f",noise=alls={grain}:allf=t"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
           "-r", str(fps), "-i", "-", "-vf", vf + ",format=yuv420p", "-frames:v", str(frames), *_enc(), str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        layer = np.zeros((h, w, 3), np.uint8)
        for f in range(frames):
            t = _ease(f / max(frames - 1, 1))
            z = z0 + (z1 - z0) * t
            vw, vh = view_w / z, view_h / z
            cx = (sw - vw) * (cx0 + (cx1 - cx0) * t) + vw / 2
            cy = (sh - vh) * (cy0 + (cy1 - cy0) * t) + vh / 2
            bx = cx + gu * vw
            by = cy + gv * vh
            dd = cv2.remap(d, bx, by, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            s = sweep * (t - .5) * 2  # -1 .. 1 across the shot
            k = 1 + dolly * t * dd * 2  # near things grow a little more on a dolly
            mx = cx + gu * vw / k + amp * dd * s
            my = cy + gv * vh / k + amp * .35 * dd * s
            mx = cv2.resize(mx, (w, h), interpolation=cv2.INTER_LINEAR)
            my = cv2.resize(my, (w, h), interpolation=cv2.INTER_LINEAR)
            frame = cv2.remap(src, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
            if flick:
                tt = f / fps
                gain = 1 + .025 * np.sin(tt * 7.1 + fph[0]) + .015 * np.sin(tt * 13.7 + fph[1]) \
                    + .02 * np.sin(tt * 1.3 + fph[2])
                frame = cv2.convertScaleAbs(frame, alpha=gain, beta=0)
            if mist is not None:
                off = int((f * .6) % w)
                frame = cv2.add(frame, mist[:, off:off + w])
            if parts:
                layer[:] = 0
                for p in parts:
                    p.step(layer, f)
                layer = cv2.GaussianBlur(layer, (0, 0), 1.2)
                frame = cv2.add(frame, layer)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed for {out.name}")
    except BaseException:
        proc.kill()
        proc.wait()
        raise
