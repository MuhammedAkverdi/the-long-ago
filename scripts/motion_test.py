"""Render short 2.5D test shots of one picture (real depth model) for a visual check: work/motion_test/*.mp4"""
import pathlib
import sys
import time

from pipeline import motion
from pipeline.config import ROOT

img = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "assets" / "manual" / "59f8716e" / "open_01.jpg"
out = ROOT / "work" / "motion_test"
out.mkdir(parents=True, exist_ok=True)
pic = out / "test.jpg"
pic.write_bytes(img.read_bytes())
t = time.time()
d = motion.depth(pic)
print(f"depth: {'ok' if d is not None else 'NONE'} in {time.time() - t:.1f}s")
for v, atmos in [(0, {"embers", "flicker"}), (2, {"dust", "mist"}), (6, {"dust"})]:
    t = time.time()
    motion.render(pic, 200, out / f"shot_v{v}.mp4", 1920, 1080, v, atmos)
    print(f"variant {v}: {time.time() - t:.1f}s for 200 frames")
