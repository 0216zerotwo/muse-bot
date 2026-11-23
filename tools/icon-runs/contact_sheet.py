"""A comparison sheet of icon candidates: each large, then at home-screen size with the app's name.

    python tools/icon-runs/contact_sheet.py <folder> name1,name2,... "label1|label2|..."  → <folder>/candidates.png
"""
from __future__ import annotations

import sys
from pathlib import Path

USAGE = """Usage:
  python tools/icon-runs/contact_sheet.py <folder> name1,name2,... "label1|label2|..."

Writes <folder>/candidates.png comparing each candidate at large and home-screen icon sizes.
"""

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _load_font(size: int):
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def rounded(image, size: int):
    from PIL import Image, ImageDraw

    small_image = image.resize((size, size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * 4 - 1, size * 4 - 1), radius=size * 4 * 0.225, fill=255)
    small_image.putalpha(mask.resize((size, size), Image.LANCZOS))
    return small_image


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        print(USAGE.rstrip())
        return 0
    if len(args) != 3 or any(a.startswith("-") for a in args):
        print(USAGE.rstrip(), file=sys.stderr)
        return 1

    from PIL import Image, ImageDraw

    folder, names, labels = Path(args[0]), args[1].split(","), args[2].split("|")
    big, small = 300, 64
    sheet = Image.new("RGB", (40 + len(names) * 340, 470), (244, 242, 238))
    draw = ImageDraw.Draw(sheet)
    font = _load_font(24)
    tiny = _load_font(15)

    for i, (name, label) in enumerate(zip(names, labels)):
        with Image.open(folder / f"{name}.png") as raw:
            image = raw.convert("RGB")
        x = 40 + i * 340
        large = rounded(image, big)
        sheet.paste(large, (x, 30), large)
        draw.text((x, 345), label, fill=(40, 40, 40), font=font)
        icon = rounded(image, small)
        sheet.paste(icon, (x + 4, 385), icon)
        draw.text((x + 80, 405), "Muse Bot", fill=(60, 60, 60), font=tiny)

    out = folder / "candidates.png"
    sheet.save(out)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
