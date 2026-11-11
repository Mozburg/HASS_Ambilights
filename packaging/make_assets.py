"""Generate the Windows icon and version resource from __version__ (no binaries in git)."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "hass_ambilights.py"
HERE = Path(__file__).resolve().parent


def version() -> str:
    text = MODULE.read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("could not find __version__ in hass_ambilights.py")
    return match.group(1)


def make_icon(path: Path) -> None:
    from PIL import Image, ImageDraw

    images = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        img = Image.new("RGBA", (size, size), (13, 17, 23, 255))
        draw = ImageDraw.Draw(img)
        pad = max(1, size // 16)
        r = int(size * 0.28)
        colours = [(255, 92, 92), (68, 209, 122), (68, 147, 248), (255, 210, 38)]
        for i, colour in enumerate(colours):
            x = pad + (i % 2) * (r + pad * 2)
            y = pad + (i // 2) * (r + pad * 2)
            draw.ellipse((x, y, x + r, y + r), fill=colour + (255,))
        images.append(img)
    images[-1].save(path, format="ICO",
                    sizes=[(im.width, im.height) for im in images])


VERSION_RESOURCE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={nums}, prodvers={nums},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('040904B0', [
        StringStruct('CompanyName', 'HASS Ambilights'),
        StringStruct('FileDescription', 'HASS Ambilights'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'HassAmbilights'),
        StringStruct('OriginalFilename', 'HassAmbilights.exe'),
        StringStruct('ProductName', 'HASS Ambilights'),
        StringStruct('ProductVersion', '{version}'),
        StringStruct('LegalCopyright', 'MIT Licence')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def make_version_info(path: Path, ver: str) -> None:
    major, minor, patch = (int(part) for part in ver.split("."))
    nums = f"({major}, {minor}, {patch}, 0)"
    path.write_text(VERSION_RESOURCE.format(nums=nums, version=ver), encoding="utf-8")


def main() -> None:
    ver = version()
    icon = HERE / "icon.ico"
    info = HERE / "version_info.txt"
    make_icon(icon)
    make_version_info(info, ver)
    print(f"icon: {icon} ({icon.stat().st_size} bytes)")
    print(f"version resource: {info} ({ver})")


if __name__ == "__main__":
    sys.exit(main())
