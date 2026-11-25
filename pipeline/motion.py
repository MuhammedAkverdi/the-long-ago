"""Motion format renderer: turns storyboard beats into animated scenes and renders them with
HyperFrames (open-source HTML -> MP4, Apache-2.0, https://github.com/heygen-com/hyperframes).

One HyperFrames project per chapter (keeps each page small), rendered to MP4 and joined.
Everything on screen is a function of timeline time, so renders are repeatable.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import pathlib
import shutil
import wave

import requests

from .config import CFG, ROOT, log, run

W, H = 1920, 1080
HF_VERSION = "0.9.91"
GSAP_URL = "https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"
GEO_URLS = {
    "land": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_land.geojson",
    "minor": "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_minor_islands.geojson",
}
CACHE = pathlib.Path(os.environ.get("TLA_CACHE", pathlib.Path.home() / ".cache" / "the-long-ago"))


def _brand(name: str, default: str) -> str:
    return str((CFG.get("brand") or {}).get(name, default))


INK, PAPER, ACC, DEEP = _brand("ink", "#1C1A17"), _brand("paper", "#F3EADB"), _brand("accent", "#E9A53C"), _brand("deep", "#7B2F2A")
SEA, NIGHT = "#3a6ea5", "#1b1d25"


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def _rng(*parts) -> float:
    """Deterministic 0..1 from any key (no Math.random in the page)."""
    h = hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


# ------------------------------------------------------------------ downloads (cached)
def _fetch(url: str, dest: pathlib.Path) -> pathlib.Path:
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(r.content)
    tmp.replace(dest)
    return dest


_RINGS: list | None = None


def _rings() -> list:
    """All land rings with their bounding boxes (Natural Earth 10m, public domain)."""
    global _RINGS
    if _RINGS is None:
        rings = []
        for name, url in GEO_URLS.items():
            data = json.loads(_fetch(url, CACHE / "geo" / f"{name}.geojson").read_text(encoding="utf-8"))
            for ft in data["features"]:
                g = ft["geometry"]
                polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
                for poly in polys:
                    for ring in poly:
                        xs = [p[0] for p in ring]
                        ys = [p[1] for p in ring]
                        rings.append((min(xs), max(xs), min(ys), max(ys), ring))
        _RINGS = rings
    return _RINGS


SPANS = {"local": 3.0, "region": 9.0, "country": 22.0, "continent": 70.0}  # degrees of longitude on screen


class Proj:
    def __init__(self, lat: float, lon: float, span_lon: float):
        self.k = math.cos(math.radians(max(-70.0, min(70.0, lat))))
        self.s = W / (span_lon * self.k)
        self.lat, self.lon = lat, lon

    def __call__(self, lon: float, lat: float) -> tuple[float, float]:
        return W / 2 + (lon - self.lon) * self.k * self.s, H / 2 - (lat - self.lat) * self.s


def land_path(p: Proj, span_lon: float, min_step: float = 1.2) -> str:
    half_x = span_lon / 2 + 1
    half_y = span_lon * H / W / p.k / 2 + 1
    x0, x1, y0, y1 = p.lon - half_x, p.lon + half_x, p.lat - half_y, p.lat + half_y
    out = []
    for a, b, c, d, ring in _rings():
        if b < x0 or a > x1 or d < y0 or c > y1:
            continue
        pts, last = [], None
        for x, y in ring:
            q = p(x, y)
            if last is None or abs(q[0] - last[0]) + abs(q[1] - last[1]) > min_step:
                pts.append(q)
                last = q
        if len(pts) >= 3:
            out.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z")
    return "".join(out)


# ------------------------------------------------------------------ doodles
def figure(gid: str, x: float, y: float, s: float, pose: str = "think", color: str = INK, coat: bool = True) -> str:
    """The channel's recurring narrator: round head, flat cap, stick body, red coat."""
    arms = {"think": "M-34,70 L-50,150 M34,70 L60,40 L22,6",
            "shock": "M-34,70 L-80,-10 M34,70 L80,-10",
            "point": "M-34,70 L-52,150 M34,70 L150,40",
            "walk": "M-34,70 L-62,138 M34,70 L44,150",
            "cold": "M-34,70 L18,112 M34,70 L-18,112",
            "happy": "M-34,70 L-76,0 M34,70 L76,0",
            "tired": "M-34,70 L-44,170 M34,70 L44,170"}.get(pose, "M-34,70 L-52,150 M34,70 L52,150")
    mouth = {"shock": f'<ellipse cx="0" cy="20" rx="8" ry="11" fill="{color}" stroke="none"/>',
             "happy": '<path d="M-14,14 Q0,30 14,14" fill="none"/>',
             "tired": '<path d="M-12,22 L12,22" fill="none"/>',
             "cold": '<path d="M-14,22 L-7,16 L0,22 L7,16 L14,22" fill="none" stroke-width="4"/>'}.get(
        pose, '<path d="M-10,20 Q0,25 10,20" fill="none"/>')
    brows = {"shock": "M-22,-14 L-6,-18 M6,-18 L22,-14", "think": "M-22,-12 L-6,-10 M6,-16 L22,-12",
             "tired": "M-22,-8 L-6,-12 M6,-12 L22,-8"}.get(pose, "")
    body = (f'<path d="M-40,58 L-52,190 L52,190 L40,58 Z" fill="{DEEP}"/>'
            f'<circle cx="0" cy="100" r="4" fill="{PAPER}" stroke="none"/><circle cx="0" cy="135" r="4" fill="{PAPER}" stroke="none"/>'
            if coat else '<path d="M0,50 L0,190"/>')
    return (f'<g transform="translate({x:.0f},{y:.0f}) scale({s})"><g id="{gid}" stroke="{color}" stroke-width="7" '
            f'stroke-linecap="round" stroke-linejoin="round" fill="none">{body}'
            f'<path d="{arms}"/>'
            f'<g class="legs"><path class="lg1" d="M-20,190 L-30,290"/><path class="lg2" d="M20,190 L30,290"/></g>'
            f'<circle cx="0" cy="0" r="40" fill="{PAPER}"/>'
            f'<circle cx="-13" cy="2" r="4.5" fill="{color}" stroke="none"/><circle cx="13" cy="2" r="4.5" fill="{color}" stroke="none"/>'
            f'{mouth}' + (f'<path d="{brows}" stroke-width="4"/>' if brows else '')
            + f'<path d="M-42,-22 Q0,-62 42,-22 Z M-54,-20 L54,-20" fill="{INK}"/>'
            f'</g></g>')


# ------------------------------------------------------------------ scene builder
class Scene:
    """Collects the markup and tweens of one beat. Times passed in are absolute (chapter clock)."""

    def __init__(self, sid: str, start: float, dur: float, bg: str = "paper"):
        self.sid, self.start, self.dur, self.bg = sid, start, dur, bg
        self.svg: list[str] = []
        self.ov: list[str] = []
        self.js: list[str] = []
        self.sfx: list[tuple[str, float]] = [("whoosh", start)]

    def at(self, frac_or_sec: float, sec: bool = True) -> float:
        """Absolute time of an offset inside the beat, kept inside it."""
        off = frac_or_sec if sec else self.dur * frac_or_sec
        return round(self.start + max(0.0, min(off, self.dur - 0.35)), 3)

    def text(self, tid: str, label: str, x: float, y: float, size: float, color: str, t0: float,
             font: str = "title", anim: str = "slam", width: float | None = None, align: str = "left",
             extra: str = "", max_w: float | None = None) -> None:
        fam = "Anton" if font == "title" else "Patrick Hand"
        label = str(label)
        if max_w:  # shrink long text so it fits on one line
            per = 0.47 if font == "title" else 0.42
            size = min(size, max_w / max(1, per * len(label)))
        w = f"width:{width}px;text-align:{align};" if width else ""
        self.ov.append(f'<div id="{tid}" class="tx" style="left:{x:.0f}px;top:{y:.0f}px;{w}font-family:\'{fam}\';'
                       f'font-size:{size:.0f}px;color:{color};{extra}">{esc(label)}</div>')
        self.anim(f"#{tid}", t0, anim)

    def anim(self, sel: str, t0: float, kind: str) -> None:
        if kind == "slam":
            self.js.append(f'tl.fromTo("{sel}",{{opacity:0,scale:1.6}},{{opacity:1,scale:1,duration:0.22,ease:"back.out(2.2)"}},{t0});')
            self.sfx.append(("thump", t0))
        elif kind == "up":
            self.js.append(f'tl.fromTo("{sel}",{{opacity:0,y:40}},{{opacity:1,y:0,duration:0.35,ease:"power3.out"}},{t0});')
        elif kind == "type":
            self.js.append(f'tl.fromTo("{sel}",{{opacity:1,clipPath:"inset(0 100% 0 0)"}},{{opacity:1,clipPath:"inset(0 0% 0 0)",duration:0.8,ease:"steps(14)"}},{t0});')
            self.sfx.append(("type", t0))
        elif kind == "stamp":
            self.js.append(f'tl.fromTo("{sel}",{{opacity:0,scale:2.4,rotation:-14}},{{opacity:1,scale:1,rotation:-9,duration:0.16,ease:"power4.in"}},{t0});')
            self.sfx.append(("stamp", t0 + 0.12))
        elif kind == "pop":
            self.js.append(f'tl.fromTo("{sel}",{{opacity:0,scale:0.4,transformOrigin:"50% 50%"}},{{opacity:1,scale:1,duration:0.3,ease:"back.out(2)"}},{t0});')
        elif kind == "fade":
            self.js.append(f'tl.fromTo("{sel}",{{opacity:0}},{{opacity:1,duration:0.4}},{t0});')

    def draw(self, sel: str, t0: float, dur: float = 0.6) -> None:
        self.js.append(f'tl.fromTo("{sel}",{{strokeDashoffset:1}},{{strokeDashoffset:0,duration:{dur},ease:"power2.inOut",stagger:0.05}},{t0});')

    def html(self) -> str:
        bg = f'<div class="bg {self.bg}"></div>'
        return (f'<div id="{self.sid}" class="clip scene" data-start="{self.start:.3f}" data-duration="{self.dur:.3f}" '
                f'data-track-index="1">{bg}<svg class="art" viewBox="0 0 {W} {H}" width="{W}" height="{H}">'
                + "".join(self.svg) + f'</svg><div class="ov">{"".join(self.ov)}</div></div>')

    def camera(self) -> None:
        # a slow push on every scene so nothing is ever completely still
        self.js.insert(0, f'tl.fromTo("#{self.sid} .art",{{scale:1}},{{scale:1.05,transformOrigin:"50% 50%",duration:{self.dur:.3f},ease:"none"}},{self.start:.3f});')


def _bg_for(i: int, kind: str) -> str:
    return "night" if kind in ("question",) else "paper"


def sc_picture(sc: Scene, p: dict, pic: str | None, i: int) -> None:
    if not pic:
        return sc_kinetic(sc, {"lines": [p.get("label") or "..."], "accent": 0}, i)
    v = int(_rng(sc.sid, "kb") * 4)
    x0, y0, s0, x1, y1, s1 = [(0, 0, 1.02, -40, -20, 1.14), (-40, 10, 1.14, 30, -10, 1.04),
                              (30, 20, 1.06, -30, -20, 1.16), (0, -20, 1.16, 0, 20, 1.05)][v]
    sc.ov.append(f'<img id="{sc.sid}-img" class="pic" src="{esc(pic)}" alt=""/>')
    sc.js.append(f'tl.fromTo("#{sc.sid}-img",{{x:{x0},y:{y0},scale:{s0}}},{{x:{x1},y:{y1},scale:{s1},duration:{sc.dur:.3f},ease:"none"}},{sc.start:.3f});')
    if p.get("label"):
        sc.text(f"{sc.sid}-lb", p["label"].upper(), 80, 820, 110, PAPER, sc.at(0.25), max_w=1500,
                extra=f"background:{INK}e6;padding:14px 34px 6px;")


def sc_kinetic(sc: Scene, p: dict, i: int) -> None:
    lines = p["lines"]
    dark = _rng(sc.sid, "bg") < 0.4
    sc.bg = "night" if dark else "paper"
    base = PAPER if dark else INK
    n = len(lines)
    size = 230 if n == 1 else 190 if n == 2 else 160
    top = (H - n * size * 1.05) / 2
    step = min(0.45, max(0.18, (sc.dur - 0.8) / max(1, n)))
    for k, line in enumerate(lines):
        col = ACC if k == p.get("accent", n - 1) else base
        stroke = "" if dark or col != ACC else f"-webkit-text-stroke:3px {INK};"
        sc.text(f"{sc.sid}-l{k}", line.upper(), 140, top + k * size * 1.05, size, col, sc.at(0.1 + k * step),
                max_w=1640, extra=stroke)
    k = p.get("accent", n - 1)
    uy = top + k * size * 1.05 + size * 1.02
    sc.svg.append(f'<path class="d" pathLength="1" d="M140,{uy:.0f} q300,-18 600,0 t600,0" stroke="{DEEP if not dark else ACC}" '
                  f'stroke-width="12" fill="none" stroke-linecap="round"/>')
    sc.draw(f"#{sc.sid} .d", sc.at(0.25 + k * step), 0.4)


def sc_date(sc: Scene, p: dict, i: int) -> None:
    if p.get("month") or p.get("day"):
        sc.svg.append(f'<g id="{sc.sid}-cal"><rect x="660" y="190" width="600" height="700" rx="18" fill="#fffaf0" stroke="{INK}" stroke-width="8"/>'
                      f'<rect x="660" y="190" width="600" height="150" rx="18" fill="{DEEP}" stroke="{INK}" stroke-width="8"/>'
                      f'<circle cx="780" cy="190" r="18" fill="{INK}"/><circle cx="1140" cy="190" r="18" fill="{INK}"/></g>')
        sc.anim(f"#{sc.sid}-cal", sc.at(0), "pop")
        sc.text(f"{sc.sid}-m", (p.get("month") or "").upper(), 660, 205, 100, PAPER, sc.at(0.15), anim="up", width=600, align="center")
        big = p.get("day") or p["year"]
        sc.text(f"{sc.sid}-d", big, 660, 340, 380 if len(big) <= 2 else 200, INK, sc.at(0.4), width=600, align="center", max_w=560)
        if p.get("day"):
            sc.text(f"{sc.sid}-y", p["year"], 1320, 700, 170, DEEP, sc.at(0.9), font="hand", anim="type")
    else:
        sc.svg.append(f'<circle class="d" pathLength="1" cx="960" cy="540" r="330" fill="none" stroke="{DEEP}" stroke-width="14"/>'
                      f'<circle class="d" pathLength="1" cx="960" cy="540" r="300" fill="none" stroke="{DEEP}" stroke-width="5"/>')
        sc.draw(f"#{sc.sid} .d", sc.at(0), 0.7)
        sc.text(f"{sc.sid}-y", p["year"], 560, 420, 260, INK, sc.at(0.3), width=800, align="center", max_w=600)


def sc_map(sc: Scene, p: dict, i: int) -> None:
    span = SPANS.get(p.get("zoom"), 9.0)
    wide = span * 3.0
    proj = Proj(p["lat"], p["lon"], wide)
    try:
        d = land_path(proj, wide, 1.2 / 3.0)
    except Exception as exc:  # noqa: BLE001 - no map data: show the place name instead
        log(f"map data unavailable ({str(exc)[:80]}), using a word card")
        return sc_kinetic(sc, {"lines": [p["place"]], "accent": 0}, i)
    sc.svg.append(f'<rect width="{W}" height="{H}" fill="#c9d8e3" opacity=".45"/>'
                  f'<g id="{sc.sid}-map"><path d="{d}" fill="#d9ccb2" stroke="{INK}" stroke-width="1.4" stroke-linejoin="round"/>')
    tx, ty = proj(p["lon"], p["lat"])
    if "from_lat" in p:
        fx, fy = proj(p["from_lon"], p["from_lat"])
        mx, my = (fx + tx) / 2, min(fy, ty) - abs(fx - tx) * 0.25 / 3
        sc.svg.append(f'<path id="{sc.sid}-route" d="M{fx:.1f},{fy:.1f} Q{mx:.1f},{my:.1f} {tx:.1f},{ty:.1f}" pathLength="1" '
                      f'stroke="{DEEP}" stroke-width="1.4" stroke-dasharray="0.03 0.02" fill="none"/>'
                      f'<circle cx="{fx:.1f}" cy="{fy:.1f}" r="2.2" fill="{INK}"/>')
    sc.svg.append(f'<circle id="{sc.sid}-dot" cx="{tx:.1f}" cy="{ty:.1f}" r="3" fill="{ACC}" stroke="{INK}" stroke-width="1"/></g>')
    zoom_t = min(2.2, max(1.0, sc.dur * 0.45))
    sc.js.append(f'tl.fromTo("#{sc.sid}-map",{{scale:1,svgOrigin:"{tx:.1f} {ty:.1f}"}},{{scale:3,svgOrigin:"{tx:.1f} {ty:.1f}",duration:{zoom_t:.2f},ease:"power3.inOut"}},{sc.at(0.05)});')
    sc.js.append(f'tl.fromTo("#{sc.sid}-dot",{{scale:0,transformOrigin:"50% 50%"}},{{scale:2,duration:0.4,ease:"back.out(3)"}},{sc.at(zoom_t * 0.7)});')
    if "from_lat" in p:
        sc.js.append(f'tl.fromTo("#{sc.sid}-route",{{strokeDashoffset:1}},{{strokeDashoffset:0,duration:1.0}},{sc.at(zoom_t)});')
    sc.text(f"{sc.sid}-pl", p["place"].upper(), 0, 640, 96, DEEP, sc.at(zoom_t * 0.8), anim="up", width=W, align="center",
            max_w=1500, extra=f"-webkit-text-stroke:2px {PAPER};paint-order:stroke;")


def sc_counter(sc: Scene, p: dict, i: int) -> None:
    num = p["number"]
    dec = len(num.split(".")[1]) if "." in num else 0
    val = float(num)
    sc.ov.append(f'<div id="{sc.sid}-n" class="tx" style="left:0;top:250px;width:{W}px;text-align:center;font-family:Anton;'
                 f'font-size:330px;color:{INK}">{esc(p.get("prefix", ""))}0{esc(p.get("suffix", ""))}</div>')
    pre, suf = json.dumps(p.get("prefix", "")), json.dumps(p.get("suffix", ""))
    t0 = sc.at(0.1)
    count = min(1.6, max(0.6, sc.dur * 0.4))
    sc.js.append(f'tl.fromTo("#{sc.sid}-n",{{opacity:0}},{{opacity:1,duration:0.15}},{t0});')
    sc.js.append(f'(function(){{var o={{v:0}};var el=document.getElementById("{sc.sid}-n");'
                 f'tl.fromTo(o,{{v:0}},{{v:{val},duration:{count:.2f},ease:"power2.out",onUpdate:function(){{'
                 f'el.textContent={pre}+o.v.toLocaleString("en-US",{{minimumFractionDigits:{dec},maximumFractionDigits:{dec}}})+{suf};}}}},{t0});}})();')
    sc.sfx.append(("tick", t0))
    if p.get("label"):
        sc.text(f"{sc.sid}-lb", p["label"].upper(), 0, 640, 110, DEEP, sc.at(count * 0.8), anim="up", width=W, align="center", max_w=1600)
    sc.svg.append(f'<path class="d" pathLength="1" d="M560,620 q200,-20 400,0 t400,0" stroke="{ACC}" stroke-width="14" fill="none" stroke-linecap="round"/>')
    sc.draw(f"#{sc.sid} .d", sc.at(count), 0.4)


def sc_list(sc: Scene, p: dict, i: int) -> None:
    items = p["items"]
    if p.get("title"):
        sc.text(f"{sc.sid}-t", p["title"].upper(), 160, 110, 120, INK, sc.at(0.05), anim="up", max_w=1600)
    step = max(0.3, (sc.dur - 0.9) / len(items))
    for k, it in enumerate(items):
        y = 300 + k * 170
        sc.svg.append(f'<g id="{sc.sid}-b{k}"><rect x="170" y="{y}" width="90" height="90" rx="10" fill="#fffaf0" stroke="{INK}" stroke-width="7"/>'
                      f'<path class="ck{k}" pathLength="1" d="M188,{y + 48} L212,{y + 72} L246,{y + 14}" stroke="{DEEP}" stroke-width="12" fill="none" '
                      f'stroke-linecap="round" style="stroke-dasharray:1"/></g>')
        t = sc.at(0.4 + k * step)
        sc.anim(f"#{sc.sid}-b{k}", t, "pop")
        sc.draw(f"#{sc.sid} .ck{k}", t + 0.15, 0.25)
        sc.text(f"{sc.sid}-i{k}", it, 310, y + 2, 84, INK, t, font="hand", anim="up", max_w=1450)


def sc_quote(sc: Scene, p: dict, i: int) -> None:
    sc.svg.append(f'<g id="{sc.sid}-pg"><rect x="300" y="130" width="1320" height="820" rx="16" fill="#fffaf0" stroke="{INK}" stroke-width="9"/>'
                  + "".join(f'<path d="M340,{290 + k * 110} L1580,{290 + k * 110}" stroke="#9fb6cf" stroke-width="3"/>' for k in range(6))
                  + f'<path d="M420,130 L420,950" stroke="#d98c8c" stroke-width="4"/></g>')
    sc.anim(f"#{sc.sid}-pg", sc.at(0), "pop")
    step = max(0.5, (sc.dur - 1.2) / len(p["lines"]))
    for k, line in enumerate(p["lines"]):
        sc.text(f"{sc.sid}-q{k}", line, 460, 200 + k * 220, 104, INK, sc.at(0.35 + k * step), font="hand", anim="type", max_w=1100)
    if p.get("source"):
        sc.text(f"{sc.sid}-src", "— " + p["source"], 460, 840, 60, DEEP, sc.at(0.5), font="hand", anim="up", max_w=1100)


def sc_stamp(sc: Scene, p: dict, i: int) -> None:
    sc.svg.append(f'<rect id="{sc.sid}-card" x="200" y="240" width="1520" height="600" rx="20" fill="#fffaf0" stroke="{INK}" stroke-width="9"/>')
    sc.anim(f"#{sc.sid}-card", sc.at(0), "pop")
    sc.text(f"{sc.sid}-tx", p["text"], 260, 360, 120, INK, sc.at(0.15), anim="up", width=1400, max_w=1400,
            extra="white-space:normal;line-height:1.1;")
    red = "#c0392b"
    sc.text(f"{sc.sid}-st", p["stamp"], 900, 560, 200, red, sc.at(min(1.3, sc.dur * 0.45)), anim="stamp",
            extra=f"border:14px solid {red};padding:0 34px;background:#fffaf0cc;", max_w=760)


def sc_compare(sc: Scene, p: dict, i: int) -> None:
    sc.svg.append(f'<path class="d" pathLength="1" d="M960,90 L960,990" stroke="{INK}" stroke-width="7" stroke-dasharray="0.02 0.015"/>')
    sc.draw(f"#{sc.sid} .d", sc.at(0), 0.4)
    half = sc.at(max(0.6, sc.dur * 0.45))
    sc.text(f"{sc.sid}-ll", (p.get("left_label") or "").upper(), 0, 190, 110, DEEP, sc.at(0.1), width=960, align="center", max_w=860)
    sc.text(f"{sc.sid}-lv", p["left"], 0, 430, 120, INK, sc.at(0.3), width=960, align="center", max_w=860, font="hand", anim="up")
    sc.text(f"{sc.sid}-rl", (p.get("right_label") or "").upper(), 960, 190, 110, DEEP, half, width=960, align="center", max_w=860)
    sc.text(f"{sc.sid}-rv", p["right"], 960, 430, 120, INK, half + 0.2, width=960, align="center", max_w=860, font="hand", anim="up")


def sc_character(sc: Scene, p: dict, i: int) -> None:
    pose = p.get("pose", "think")
    cold = pose == "cold"
    sc.bg = "night" if cold else "paper"
    col = PAPER if cold else INK
    sc.svg.append(f'<path d="M0,900 Q960,860 1920,900 L1920,1080 L0,1080 Z" fill="{"#2a2d38" if cold else "#d9ccb2"}" stroke="{col}" stroke-width="6"/>')
    x = 620 if p.get("bubble") else 960
    sc.svg.append(figure(f"{sc.sid}-f", x, 470, 1.35, pose, color=col, coat=not cold))
    sc.anim(f"#{sc.sid}-f", sc.at(0), "pop")
    if pose == "walk":
        sc.js.append(f'tl.fromTo("#{sc.sid}-f .lg1",{{rotation:-12,svgOrigin:"-20 190"}},{{rotation:12,svgOrigin:"-20 190",duration:0.25,ease:"steps(1)",yoyo:true,repeat:{int(sc.dur * 4)}}},{sc.start:.3f});')
        sc.js.append(f'tl.fromTo("#{sc.sid}-f",{{x:-120}},{{x:120,duration:{sc.dur:.2f},ease:"none"}},{sc.start:.3f});')
    elif pose in ("cold", "shock"):
        sc.js.append(f'tl.fromTo("#{sc.sid}-f",{{x:-5}},{{x:5,duration:0.07,yoyo:true,repeat:{int(sc.dur * 12)},ease:"steps(1)"}},{sc.start:.3f});')
    elif pose == "happy":
        sc.js.append(f'tl.fromTo("#{sc.sid}-f",{{y:0}},{{y:-30,duration:0.3,yoyo:true,repeat:{int(sc.dur * 3)},ease:"sine.inOut"}},{sc.start:.3f});')
    if cold:
        sc.svg.append("".join(f'<circle class="sn" cx="{(k * 397) % W}" cy="{(k * 263) % H}" r="{4 + (k % 3) * 2}" fill="{PAPER}"/>' for k in range(60)))
        sc.js.append(f'tl.fromTo("#{sc.sid} .sn",{{x:0,y:0}},{{x:-400,y:220,duration:{sc.dur:.2f},ease:"none"}},{sc.start:.3f});')
    if pose == "shock":
        sc.svg.append(f'<g id="{sc.sid}-lines" stroke="{DEEP}" stroke-width="8" stroke-linecap="round">'
                      f'<path d="M{x - 150},250 L{x - 200},200"/><path d="M{x + 150},250 L{x + 200},200"/><path d="M{x},180 L{x},120"/></g>')
        sc.anim(f"#{sc.sid}-lines", sc.at(0.15), "pop")
    if pose == "tired":
        sc.text(f"{sc.sid}-z", "z z z", x + 70, 300, 90, DEEP, sc.at(0.3), font="hand", anim="up")
    if p.get("bubble"):
        think = pose == "think"
        sc.svg.append(f'<g id="{sc.sid}-bub"><ellipse cx="1290" cy="330" rx="520" ry="200" fill="#fffaf0" stroke="{INK}" stroke-width="8"/>'
                      + (f'<circle cx="860" cy="520" r="26" fill="#fffaf0" stroke="{INK}" stroke-width="6"/><circle cx="800" cy="580" r="16" fill="#fffaf0" stroke="{INK}" stroke-width="5"/>'
                         if think else f'<path d="M900,440 L760,560 L990,470" fill="#fffaf0" stroke="{INK}" stroke-width="8" stroke-linejoin="round"/>')
                      + '</g>')
        sc.anim(f"#{sc.sid}-bub", sc.at(0.25), "pop")
        sc.text(f"{sc.sid}-bt", p["bubble"], 820, 280, 96, INK, sc.at(0.4), font="hand", anim="up", width=940, align="center", max_w=900)


def sc_question(sc: Scene, p: dict, i: int) -> None:
    sc.bg = "night"
    sc.text(f"{sc.sid}-q", p["text"].upper(), 120, 330, 160, PAPER, sc.at(0.1), width=1400, align="left",
            extra="white-space:normal;line-height:1.05;")
    sc.text(f"{sc.sid}-qm", "?", 1580, 200, 560, ACC, sc.at(0.5))


BUILDERS = {"kinetic": sc_kinetic, "date": sc_date, "map": sc_map, "counter": sc_counter, "list": sc_list,
            "quote": sc_quote, "stamp": sc_stamp, "compare": sc_compare, "character": sc_character,
            "question": sc_question}


CSS = f"""
@font-face {{ font-family: 'Anton'; src: url('assets/fonts/Anton-Regular.ttf'); }}
@font-face {{ font-family: 'Patrick Hand'; src: url('assets/fonts/PatrickHand-Regular.ttf'); }}
* {{ margin:0; padding:0; box-sizing:border-box; }}
html,body {{ width:{W}px; height:{H}px; overflow:hidden; background:{PAPER}; }}
#root {{ position:relative; width:{W}px; height:{H}px; overflow:hidden; }}
.scene {{ position:absolute; inset:0; overflow:hidden; }}
.bg {{ position:absolute; inset:0; background-size:cover; }}
.bg.paper {{ background-image:url('assets/paper.jpg'); }}
.bg.night {{ background-image:url('assets/night.jpg'); }}
.art {{ position:absolute; left:0; top:0; }}
.ov {{ position:absolute; inset:0; }}
.pic {{ position:absolute; left:0; top:0; width:{W}px; height:{H}px; object-fit:cover; }}
.tx {{ position:absolute; white-space:nowrap; line-height:1; opacity:0; }}
.d {{ stroke-dasharray:1; stroke-dashoffset:0; }}
.part {{ position:absolute; left:60px; top:50px; padding:14px 30px 8px; background:{INK}; color:{PAPER};
         font-family:'Anton'; font-size:54px; opacity:0; z-index:40; }}
"""


def textures(dest: pathlib.Path) -> None:
    """Paper and night backgrounds (made once, deterministic)."""
    import numpy as np
    from PIL import Image, ImageFilter
    rng = np.random.default_rng(7)

    def tex(base, amp, out):
        n = rng.normal(0, 1, (270, 480))
        img = Image.fromarray(((n - n.min()) / (n.max() - n.min()) * 255).astype("uint8")).resize((W, H), Image.BICUBIC)
        lo = np.asarray(img.filter(ImageFilter.GaussianBlur(3)), dtype=float) / 255 - 0.5
        v = lo * amp + rng.normal(0, 1, (H, W)) * 0.35 * amp * 0.5
        yy, xx = np.mgrid[0:H, 0:W]
        vig = ((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2
        a = np.array(base, dtype=float)[None, None, :] * (1 + v[..., None]) * (1 - 0.10 * vig[..., None])
        Image.fromarray(np.clip(a, 0, 255).astype("uint8")).save(out, quality=92)

    dest.mkdir(parents=True, exist_ok=True)
    hexrgb = lambda c: tuple(int(c[k:k + 2], 16) for k in (1, 3, 5))  # noqa: E731
    if not (dest / "paper.jpg").exists():
        tex(hexrgb(PAPER), 0.07, dest / "paper.jpg")
    if not (dest / "night.jpg").exists():
        tex(hexrgb(NIGHT), 0.25, dest / "night.jpg")


def chapter_html(beats: list[dict], times: list[tuple[float, float]], span: float, pics: dict[int, str],
                 heading: str | None, part_no: int, key: str) -> tuple[str, list[tuple[str, float]]]:
    """One chapter page. times[i] = (start, length) of beat i inside the chapter; span = chapter length."""
    body, js, sfx = [], [], []
    for i, (b, (s, _d)) in enumerate(zip(beats, times)):
        end = times[i + 1][0] if i + 1 < len(times) else span
        dur = max(0.4, end - s)
        p = b["scene"]
        sc = Scene(f"{key}b{i}", s, dur, _bg_for(i, p["t"]))
        if p["t"] == "picture":
            sc_picture(sc, p, pics.get(i), i)
        else:
            BUILDERS[p["t"]](sc, p, i)
        sc.camera()
        body.append(sc.html())
        js += sc.js
        sfx += sc.sfx
    if heading and part_no > 0:
        body.append(f'<div id="{key}part" class="clip part" data-start="0.2" data-duration="2.8" data-track-index="2">'
                    f'PART {part_no + 1} · {esc(heading.upper())}</div>')
        js.append(f'tl.fromTo("#{key}part",{{opacity:0,x:-60}},{{opacity:1,x:0,duration:0.35,ease:"power3.out"}},0.2);')
    # hand-drawn "boil": the line art trembles slightly at 8 fps, like a cartoon
    js.append(f'tl.fromTo(".art",{{rotation:-0.2}},{{rotation:0.2,duration:0.125,ease:"steps(1)",yoyo:true,repeat:{int(span * 8)}}},0);')
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8"/><meta name="viewport" content="width={W}, height={H}"/>
<script src="assets/gsap.min.js"></script>
<style>{CSS}</style></head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{span:.3f}" data-width="{W}" data-height="{H}">
{''.join(body)}
</div>
<script>
window.__timelines = window.__timelines || {{}};
const tl = gsap.timeline({{ paused: true }});
{chr(10).join(js)}
window.__timelines["main"] = tl;
</script>
</body></html>"""
    return doc, sfx


def _hf_cmd() -> list[str]:
    exe = shutil.which("hyperframes")
    return [exe] if exe else ["npx", "--yes", f"hyperframes@{HF_VERSION}"]


def render_chapter(project: pathlib.Path, out: pathlib.Path, fps: int) -> None:
    if out.exists() and out.stat().st_size > 10000:
        return  # resume: this chapter is already rendered
    tmp = out.with_suffix(".part.mp4")
    run(_hf_cmd() + ["render", "-o", str(tmp), "--fps", str(fps), "--crf", str(CFG["video"].get("crf", 23)),
                     "--browser-timeout", "300"], cwd=project)
    tmp.replace(out)


def prepare_assets(project: pathlib.Path) -> None:
    a = project / "assets"
    (a / "fonts").mkdir(parents=True, exist_ok=True)
    for f in ("Anton-Regular.ttf", "PatrickHand-Regular.ttf"):
        src = ROOT / "assets" / "fonts" / f
        if not (a / "fonts" / f).exists():
            shutil.copy(src, a / "fonts" / f)
    textures(CACHE / "tex")
    for f in ("paper.jpg", "night.jpg"):
        if not (a / f).exists():
            shutil.copy(CACHE / "tex" / f, a / f)
    if not (a / "gsap.min.js").exists():
        shutil.copy(_gsap_file(), a / "gsap.min.js")


def _gsap_file() -> pathlib.Path:
    """GSAP ships inside the HyperFrames package; the CDN is only a fallback."""
    vendored = pathlib.Path("dist/vendor/npm/gsap@3.15.0/dist/gsap.min.js")
    roots = []
    exe = shutil.which("hyperframes")
    if exe:
        roots += list(pathlib.Path(os.path.realpath(exe)).parents)[:4]
    try:
        roots.append(pathlib.Path(run(["npm", "root", "-g"]).strip()) / "hyperframes")
    except Exception:  # noqa: BLE001 - npm missing: fall back to the CDN
        pass
    for r in roots:
        if (r / vendored).exists():
            return r / vendored
    return _fetch(GSAP_URL, CACHE / "gsap-3.15.0.min.js")


def render_episode(board: list[list[dict]], beat_times: list[list[tuple[float, float]]], spans: list[float],
                   pictures: list[dict[int, pathlib.Path]], headings: list[str], folder: pathlib.Path,
                   out: pathlib.Path) -> list[tuple[str, float]]:
    """Render all chapters and join them into `out` (silent). Returns sound-effect cues (episode clock)."""
    fps = int(CFG["video"].get("fps", 25))
    mdir = folder / "motion"
    parts, cues, offset = [], [], 0.0
    for ci, beats in enumerate(board):
        project = mdir / f"ch{ci:02d}"
        prepare_assets(project)
        pics = {}
        for bi, src in pictures[ci].items():
            dst = project / "assets" / "pics" / f"{bi:03d}{src.suffix}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy(src, dst)
            pics[bi] = f"assets/pics/{dst.name}"
        doc, sfx = chapter_html(beats, beat_times[ci], spans[ci], pics, headings[ci], ci, f"c{ci}")
        (project / "index.html").write_text(doc, encoding="utf-8")
        clip = mdir / f"ch{ci:02d}.mp4"
        log(f"motion: rendering chapter {ci + 1}/{len(board)} ({spans[ci]:.0f} s, {len(beats)} scenes)")
        render_chapter(project, clip, fps)
        parts.append(clip)
        cues += [(k, t + offset) for k, t in sfx]
        offset += spans[ci]
    lst = mdir / "parts.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(out)])
    return cues


# ------------------------------------------------------------------ sound effects
def sfx_track(cues: list[tuple[str, float]], total: float, out: pathlib.Path, sr: int = 44100) -> pathlib.Path:
    """Soft whooshes on cuts, thumps on slams/stamps, typewriter clicks (all synthesised, no licences)."""
    import numpy as np
    rng = np.random.default_rng(11)
    n = int(total * sr) + sr
    y = np.zeros(n)

    def smooth(x, k):
        return np.convolve(x, np.ones(k) / k, mode="same")

    def put(x, at, g):
        i = int(at * sr)
        if 0 <= i < n:
            j = min(n, i + len(x))
            y[i:j] += x[: j - i] * g

    L = int(0.4 * sr)
    tt = np.arange(L) / sr
    noise = rng.normal(0, 1, L)
    whoosh = (smooth(noise, 6) - smooth(noise, 40)) * np.sin(np.pi * tt / 0.4) ** 2
    L2 = int(0.35 * sr)
    t2 = np.arange(L2) / sr
    thump = np.sin(2 * np.pi * (55 + 70 * np.exp(-t2 * 30)) * t2) * np.exp(-t2 * 9)
    stamp = thump + (rng.normal(0, 1, L2) - smooth(rng.normal(0, 1, L2), 8)) * np.exp(-t2 * 60) * 0.5
    L3 = int(0.03 * sr)
    click = (rng.normal(0, 1, L3) - smooth(rng.normal(0, 1, L3), 3)) * np.exp(-np.arange(L3) / sr * 300)
    tick = np.sin(2 * np.pi * 1800 * np.arange(L3) / sr) * np.exp(-np.arange(L3) / sr * 200)
    last = {}
    for kind, t in sorted(cues, key=lambda c: c[1]):
        if t - last.get(kind, -9) < 0.25:
            continue  # never stack the same sound
        last[kind] = t
        if kind == "whoosh":
            put(whoosh, t - 0.18, 0.18)
        elif kind == "thump":
            put(thump, t, 0.35)
        elif kind == "stamp":
            put(stamp, t, 0.7)
        elif kind == "type":
            for q in range(7):
                put(click, t + q * 0.09 + rng.uniform(0, 0.02), 0.3)
        elif kind == "tick":
            for q in range(8):
                put(tick, t + q * 0.12, 0.12)
    peak = float(np.max(np.abs(y))) or 1.0
    y = (y / peak * 0.5 * 32767).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(y.tobytes())
    return out
