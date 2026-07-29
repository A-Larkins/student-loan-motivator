"""Generate the app icon with no third-party dependencies.

Writes icon.png (1024x1024), then hands it to the macOS `sips` / `iconutil`
pair to produce icon.icns. Pillow isn't required - PNG is simple enough to
encode directly, and keeping the dependency list empty means the app stays a
stdlib-only project.

    python3 make_icon.py

The art is the app's thesis in one image: a bar chart of debt falling away,
ember red on the left through to green on the right.
"""

import math
import struct
import subprocess
import sys
import zlib
from pathlib import Path

SIZE = 1024
HERE = Path(__file__).resolve().parent

BG_TOP = (0x2B, 0x12, 0x16)
BG_BOTTOM = (0x12, 0x0A, 0x0C)
RED = (0xEF, 0x44, 0x44)
AMBER = (0xF5, 0x9E, 0x0B)
GREEN = (0x22, 0xC5, 0x5E)
BASELINE = (0x7A, 0x2B, 0x36)


def lerp(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def ramp(t):
    """Red -> amber -> green, matching the app's progress accent."""
    return lerp(RED, AMBER, t * 2) if t < 0.5 else lerp(AMBER, GREEN, (t - 0.5) * 2)


def write_png(path: Path, pixels) -> None:
    raw = b"".join(b"\x00" + bytes(c for px in row for c in px) for row in pixels)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def coverage(x, y, inside, samples=3):
    """Supersampled coverage of `inside` over one pixel, for smooth edges."""
    hits = 0
    step = 1.0 / (samples + 1)
    for i in range(1, samples + 1):
        for j in range(1, samples + 1):
            if inside(x + i * step, y + j * step):
                hits += 1
    return hits / (samples * samples)


def rounded_rect(x0, y0, x1, y1, radius):
    def inside(px, py):
        if not (x0 <= px <= x1 and y0 <= py <= y1):
            return False
        cx = min(max(px, x0 + radius), x1 - radius)
        cy = min(max(py, y0 + radius), y1 - radius)
        return math.hypot(px - cx, py - cy) <= radius
    return inside


def build():
    margin = SIZE * 0.085
    squircle = rounded_rect(margin, margin, SIZE - margin, SIZE - margin, SIZE * 0.225)

    # Five bars, tall on the left and nearly gone on the right.
    heights = [0.72, 0.58, 0.43, 0.28, 0.13]
    count = len(heights)
    field_l, field_r = SIZE * 0.20, SIZE * 0.80
    field_w = field_r - field_l
    gap = field_w * 0.045
    bar_w = (field_w - gap * (count - 1)) / count
    floor_y = SIZE * 0.775
    bar_radius = bar_w * 0.22

    bars = []
    for i, frac in enumerate(heights):
        left = field_l + i * (bar_w + gap)
        top = floor_y - field_w * frac
        bars.append((rounded_rect(left, top, left + bar_w, floor_y, bar_radius), ramp(i / (count - 1))))

    base = rounded_rect(field_l, floor_y + SIZE * 0.028, field_r, floor_y + SIZE * 0.055, SIZE * 0.014)

    rows = []
    for y in range(SIZE):
        bg = lerp(BG_TOP, BG_BOTTOM, y / SIZE)
        row = []
        for x in range(SIZE):
            outer = coverage(x, y, squircle)
            if outer <= 0:
                row.append((0, 0, 0, 0))
                continue

            r, g, b = bg
            for shape, color in bars:
                c = coverage(x, y, shape)
                if c > 0:
                    r, g, b = lerp((r, g, b), color, c)
            c = coverage(x, y, base)
            if c > 0:
                r, g, b = lerp((r, g, b), BASELINE, c)

            row.append((r, g, b, round(255 * outer)))
        rows.append(row)
    return rows


def main() -> int:
    png = HERE / "icon.png"
    print(f"rendering {SIZE}x{SIZE}...")
    write_png(png, build())
    print(f"  wrote {png.name} ({png.stat().st_size:,} bytes)")

    iconset = HERE / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 64, 128, 256, 512, 1024):
        for scale, suffix in ((1, ""), (2, "@2x")):
            px = size * scale
            if px > 1024:
                continue
            name = f"icon_{size}x{size}{suffix}.png"
            subprocess.run(
                ["sips", "-z", str(px), str(px), str(png), "--out", str(iconset / name)],
                check=True, capture_output=True,
            )
    icns = HERE / "icon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    print(f"  wrote {icns.name} ({icns.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
