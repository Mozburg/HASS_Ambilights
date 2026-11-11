"""Generate the boot splash image (PyInstaller --splash) from the app's own palette.

Run at build time, like make_assets.py: nothing binary is committed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
W, H = 620, 360
BG = (18, 22, 28)
CARD = (26, 32, 40)
INK = (232, 238, 245)
MUTED = (138, 152, 168)
DOTS = ((255, 86, 86), (255, 176, 66), (86, 210, 140), (86, 160, 255))

FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
SMALL_CANDIDATES = (
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def _font(size: int, candidates) -> ImageFont.ImageFont:
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def make_splash(path: Path, version: str = "") -> Path:
    img = Image.new("RGBA", (W, H), BG + (255,))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle((16, 16, W - 16, H - 16), radius=22, fill=CARD + (255,))

    # the four dots from the app icon, in reading order
    dot, gap = 16, 12
    total = len(DOTS) * dot + (len(DOTS) - 1) * gap
    x = (W - total) // 2
    for colour in DOTS:
        draw.ellipse((x, 92, x + dot, 92 + dot), fill=colour + (255,))
        x += dot + gap

    title = _font(40, FONT_CANDIDATES)
    draw.text((W // 2, 178), "HASS AMBILIGHTS", font=title, fill=INK + (255,), anchor="mm")

    subtitle = _font(17, SMALL_CANDIDATES)
    draw.text((W // 2, 226), "mirroring your screen onto Home Assistant lights",
              font=subtitle, fill=MUTED + (255,), anchor="mm")

    # a progress rail, purely decorative - it is gone in a second or two
    rail = (W // 2 - 150, 286, W // 2 + 150, 292)
    draw.rounded_rectangle(rail, radius=3, fill=(44, 52, 63, 255))
    draw.rounded_rectangle((rail[0], rail[1], rail[0] + 120, rail[3]), radius=3,
                           fill=INK[:3] + (200,))

    if version:
        tiny = _font(13, SMALL_CANDIDATES)
        draw.text((W // 2, H - 34), f"version {version} · starting up",
                  font=tiny, fill=MUTED + (255,), anchor="mm")

    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, format="PNG")
    return path


def version() -> str:
    sys.path.insert(0, str(HERE.parent))
    try:
        from hass_ambilights import __version__
    finally:
        sys.path.pop(0)
    return __version__


def main() -> None:
    target = make_splash(HERE / "splash.png", version())
    print(f"wrote {target} ({target.stat().st_size} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
