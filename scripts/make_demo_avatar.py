"""Generate a simple original placeholder character (no third-party art) in every
supported avatar format, for testing the showcase:

    idle.gif  listening.apng  speaking.webm (VP9 + alpha)  default.webp  speaking.mp4

    python scripts/make_demo_avatar.py <output folder>
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

S = 384
FPS = 24
yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)


def disc(cx, cy, r, soft=1.5):
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    return np.clip((r - d) / soft + 0.5, 0, 1)


def ellipse(cx, cy, rx, ry, soft=1.5):
    d = np.sqrt(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2)
    return np.clip((1 - d) * min(rx, ry) / soft + 0.5, 0, 1)


def paint(img, mask, rgb):
    a = mask[..., None]
    img[..., :3] = img[..., :3] * (1 - a) + np.array(rgb, np.float32) / 255.0 * a
    img[..., 3] = np.maximum(img[..., 3], mask)


def frame(t: float, state: str) -> np.ndarray:
    img = np.zeros((S, S, 4), np.float32)
    bob = 4 * np.sin(t * 2 * np.pi / 2.4)
    tilt = 10 * np.sin(t * 2 * np.pi / 1.6) if state == "listening" else 0
    cx, cy = S / 2 + tilt, S / 2 + 18 + bob
    paint(img, ellipse(cx, cy + 128, 112, 70), (92, 84, 170))           # shoulders
    paint(img, disc(cx, cy - 6, 112), (64, 48, 92))                     # hair back
    paint(img, ellipse(cx, cy + 8, 92, 100), (250, 226, 210))          # face
    paint(img, ellipse(cx, cy - 74, 100, 46) * (yy < cy - 40), (64, 48, 92))  # fringe
    blink = (t % 3.2) < 0.12
    eye_h = 3 if blink else (17 if state == "listening" else 14)
    for sx in (-36, 36):
        paint(img, ellipse(cx + sx, cy + 10, 12, eye_h), (40, 36, 70))
        if not blink:
            paint(img, disc(cx + sx + 4, cy + 4, 4), (255, 255, 255))
        paint(img, ellipse(cx + sx * 1.35, cy + 40, 13, 7), (255, 170, 180))  # cheeks
    if state == "speaking":
        open_ = 0.5 + 0.5 * np.abs(np.sin(t * 2 * np.pi * 2.7)) * (0.6 + 0.4 * np.sin(t * 5.1))
        paint(img, ellipse(cx, cy + 58, 16, 4 + 14 * open_), (150, 60, 80))
    else:
        paint(img, ellipse(cx, cy + 58, 14, 3), (150, 60, 80))
    if state == "listening":  # little sound arcs near the ear
        for k, r in enumerate((26, 40)):
            ring = disc(cx + 118, cy + 6, r) - disc(cx + 118, cy + 6, r - 4)
            ring *= (xx > cx + 118) * (0.6 + 0.4 * np.sin(t * 6 - k))
            paint(img, np.clip(ring, 0, 1), (94, 234, 212))
    return (np.clip(img, 0, 1) * 255).astype(np.uint8)


def render(state: str, seconds: float, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(int(seconds * FPS)):
        (folder / f"{i:04d}.rgba").write_bytes(frame(i / FPS, state).tobytes())
    raw = folder / "all.rgba"
    with open(raw, "wb") as f:
        for p in sorted(folder.glob("0*.rgba")):
            f.write(p.read_bytes())
    return raw


def ff(raw: Path, out: Path, args: list[str]) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{S}x{S}", "-r", str(FPS), "-i", str(raw), *args, str(out)]
    subprocess.check_call(cmd)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo_avatar")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        idle = render("idle", 3.2, Path(td) / "idle")
        listen = render("listening", 3.2, Path(td) / "listen")
        speak = render("speaking", 2.0, Path(td) / "speak")
        ff(idle, out / "idle.gif", ["-filter_complex", "split[a][b];[a]palettegen=reserve_transparent=1[p];[b][p]paletteuse=alpha_threshold=128", "-loop", "0"])
        ff(listen, out / "listening.apng", ["-plays", "0", "-f", "apng"])
        ff(speak, out / "speaking.webm", ["-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "32", "-auto-alt-ref", "0"])
        ff(idle, out / "default.webp", ["-c:v", "libwebp_anim", "-loop", "0", "-lossless", "0", "-q:v", "80"])
        ff(speak, out / "speaking.mp4", ["-vf", "format=yuv420p", "-c:v", "libx264", "-crf", "24", "-movflags", "+faststart"])
    print("written to", out)


if __name__ == "__main__":
    main()
