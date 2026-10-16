"""Draws ORACLE's app icons (no image libraries needed).

The mark: a circle split down the middle. The blue half is CLIO (history, resistance), the orange half
is OMEN (current events, push), and the white bar between them is ORACLE's verdict. It sits on a
near-black rounded square, and everything stays inside the central safe zone so Android's "maskable"
icon cropping never cuts it off.

Usage:  python tools/make_icons.py      (writes docs/icon-512.png, docs/icon-192.png, docs/apple-touch-icon.png)
"""
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs"

BG = (13, 13, 13)
CLIO = (57, 135, 229)
OMEN = (217, 89, 38)
BAR = (255, 255, 255)


def png(path, w, h, pixels):
    """Write an RGB PNG. pixels: list of rows, each a bytes object of length w*3."""
    raw = b"".join(b"\x00" + row for row in pixels)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw(size):
    c = (size - 1) / 2
    r = size * 0.30            # circle radius: inside the 80% maskable safe zone
    bar_half = size * 0.022    # verdict bar half-width
    bar_len = r * 1.18         # bar extends a little past the circle
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            dx, dy = x - c, y - c
            dist = (dx * dx + dy * dy) ** 0.5
            col = BG
            cov = max(0.0, min(1.0, r - dist + 0.5))          # anti-aliased circle edge
            if cov > 0:
                col = mix(BG, CLIO if dx < 0 else OMEN, cov)
            bx = max(0.0, min(1.0, bar_half - abs(dx) + 0.5))  # anti-aliased bar edges
            by = max(0.0, min(1.0, bar_len - abs(dy) + 0.5))
            if bx > 0 and by > 0:
                col = mix(col, BAR, bx * by)
            row += bytes(col)
        rows.append(bytes(row))
    return rows


def main():
    OUT.mkdir(exist_ok=True)
    for name, size in (("icon-512.png", 512), ("icon-192.png", 192), ("apple-touch-icon.png", 180)):
        png(OUT / name, size, size, draw(size))
        print(f"wrote docs/{name}")


if __name__ == "__main__":
    main()
