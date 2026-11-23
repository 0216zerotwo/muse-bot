"""One 1024x1024 app icon → every size the three apps use.

    python tools/make_app_icons.py [path/to/icon-1024.png]

Writes (replacing what is there):
- iOS: ios/Companion/Resources/Assets.xcassets/AppIcon.appiconset/icon-*.png (opaque RGB; iOS rounds the corners)
- Android: android/app/src/main/res/mipmap-*/ic_launcher.png (legacy, rounded square) and
  ic_launcher_foreground.png (adaptive, full bleed: the launcher keeps about the middle two thirds, so
  the face must sit there), with the background colour in drawable/ic_launcher_background.xml
- Web: web/public/brand/companion_{apple_touch,favicon_64,icon_192,icon_512,icon_maskable}.png
  and companion_mark.svg (the PNG inside an SVG, for the tab icon)
"""
from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "tools/icon-runs/chosen/muse-bot-icon.png"
IOS = ROOT / "ios/Companion/Resources/Assets.xcassets/AppIcon.appiconset"
ANDROID = ROOT / "android/app/src/main/res"
WEB = ROOT / "web/public/brand"
DENSITIES = {"mdpi": 1.0, "hdpi": 1.5, "xhdpi": 2.0, "xxhdpi": 3.0, "xxxhdpi": 4.0}

USAGE = f"""Usage: python tools/make_app_icons.py [path/to/icon-1024.png]

Generates all iOS, Android, and Web app icon assets from a single 1024x1024 source PNG.
Defaults to {DEFAULT_SOURCE.relative_to(ROOT).as_posix()} if omitted.
"""


def square(path: Path):
    from PIL import Image

    with Image.open(path) as raw:
        image = raw.convert("RGB")
    side = min(image.size)
    left, top = (image.width - side) // 2, (image.height - side) // 2
    return image.crop((left, top, left + side, top + side)).resize((1024, 1024), Image.LANCZOS)


def corner_colour(image) -> tuple[int, int, int]:
    """The background the artwork sits on: the middle of its four corners (one may be the character)."""
    points = [image.getpixel((x, y)) for x in (8, 1015) for y in (8, 1015)]
    return tuple(sorted(p[i] for p in points)[1] for i in range(3))


def rounded(image, size: int, radius: float = 0.225):
    from PIL import Image, ImageDraw

    small = image.resize((size, size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * 4 - 1, size * 4 - 1), radius=size * 4 * radius, fill=255)
    small.putalpha(mask.resize((size, size), Image.LANCZOS))
    return small


def ios(image) -> None:
    from PIL import Image

    IOS.mkdir(parents=True, exist_ok=True)
    sizes = {
        "icon-20@2x": 40, "icon-20@3x": 60, "icon-29@2x": 58, "icon-29@3x": 87,
        "icon-40@2x": 80, "icon-40@3x": 120, "icon-60@2x": 120, "icon-60@3x": 180,
        "icon-1024": 1024,
    }
    for name, size in sizes.items():
        image.resize((size, size), Image.LANCZOS).save(IOS / f"{name}.png", optimize=True)


def android(image) -> None:
    from PIL import Image

    for density, factor in DENSITIES.items():
        folder = ANDROID / f"mipmap-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        rounded(image, round(48 * factor)).save(folder / "ic_launcher.png", optimize=True)
        # Adaptive: 108dp, of which the launcher shows about the middle 66% in its own shape.
        # Slightly smaller than the layer (82%): more of the picture shows, its edges still never do.
        side, inner = round(108 * factor), round(108 * factor * 0.82)
        layer = Image.new("RGB", (side, side), corner_colour(image))
        layer.paste(image.resize((inner, inner), Image.LANCZOS), ((side - inner) // 2, (side - inner) // 2))
        layer.save(folder / "ic_launcher_foreground.png", optimize=True)
    r, g, b = corner_colour(image)
    drawable_dir = ANDROID / "drawable"
    drawable_dir.mkdir(parents=True, exist_ok=True)
    (drawable_dir / "ic_launcher_background.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!-- The adaptive icon's background: the app icon's own background colour (tools/make_app_icons.py). -->\n"
        '<shape xmlns:android="http://schemas.android.com/apk/res/android" android:shape="rectangle">\n'
        f'    <solid android:color="#{r:02X}{g:02X}{b:02X}" />\n'
        "</shape>\n",
        encoding="utf-8",
    )


def web(image) -> None:
    from PIL import Image

    WEB.mkdir(parents=True, exist_ok=True)
    image.resize((180, 180), Image.LANCZOS).save(WEB / "companion_apple_touch.png", optimize=True)
    rounded(image, 64).save(WEB / "companion_favicon_64.png", optimize=True)
    rounded(image, 192).save(WEB / "companion_icon_192.png", optimize=True)
    rounded(image, 512).save(WEB / "companion_icon_512.png", optimize=True)
    image.resize((512, 512), Image.LANCZOS).save(WEB / "companion_icon_maskable.png", optimize=True)
    data = io.BytesIO()
    rounded(image, 128).save(data, "PNG", optimize=True)
    encoded = base64.b64encode(data.getvalue()).decode()
    (WEB / "companion_mark.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 128 128">'
        f'<image width="128" height="128" href="data:image/png;base64,{encoded}"/></svg>\n',
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        print(USAGE.rstrip())
        return 0
    if len(args) > 1 or (args and args[0].startswith("-")):
        print(USAGE.rstrip(), file=sys.stderr)
        return 1

    src = Path(args[0]) if args else DEFAULT_SOURCE
    if not src.is_file():
        print(f"Source icon not found: {src}", file=sys.stderr)
        return 1

    image = square(src)
    ios(image)
    android(image)
    web(image)
    print("icons written for iOS, Android and the web")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
