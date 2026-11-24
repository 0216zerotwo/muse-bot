"""Draws the app's icon set with the image model set in 模型 (Codex's gpt-image), in the default
companion's style, into web/public/icons/<name>.webp (transparent, 192px).

    hermes-env/Scripts/python.exe -X utf8 tools/draw_icons.py [name ...]   # all, or just these

The emoji each icon stands for is in web/src/companion/icons.ts.
"""
from __future__ import annotations

import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "public" / "icons"

SUBJECTS = {
    "robot": "a friendly little robot head with round eyes",
    "search": "a magnifying glass",
    "money": "a money bag with a coin",
    "write": "a pencil writing on a sheet of paper",
    "compass": "a travel compass",
    "cart": "a shopping cart",
    "globe": "a globe with a speech bubble",
    "books": "a small stack of books",
    "folders": "colorful file folders",
    "chart": "a rising line chart on a little board",
    "cooking": "a frying pan with a sunny-side-up egg",
    "run": "a running shoe with motion lines",
    "palette": "a painter's palette with a brush",
    "coder": "a laptop showing code brackets",
    "mailbox": "a mailbox with a letter",
    "calendar": "a desk calendar page",
    "smile": "a smiling round face, like a happy emoji",
    "laptop": "an open laptop",
    "news": "a folded newspaper",
    "leaf": "a fresh green leaf sprout",
    "plane": "a small airplane",
    "clapper": "a movie clapperboard",
    "ball": "a soccer ball",
    "home": "a cozy little house",
    "people": "two friends side by side",
    "target": "a target with an arrow in the center",
    "memo": "a notepad with a pen",
}

STYLE = (
    "A single cute kawaii sticker-style app icon of {subject}. Flat illustration with soft cel shading, "
    "warm palette (cream, soft orange, with one gentle accent color), chunky rounded shapes, "
    "a thin warm dark-brown outline, small highlights, friendly and cozy, in the same style as a cute "
    "orange tabby cat mascot. Centered, filling about 80% of the frame, on a plain pure white background. "
    "No text, no letters, no border, no drop shadow on the background."
)

USAGE = f"""Usage: python tools/draw_icons.py [icon_name ...]

Draws transparent 192x192 WebP sticker icons into web/public/icons/<name>.webp.
Available names ({len(SUBJECTS)}): {", ".join(SUBJECTS)}
"""


def _load_drawing():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("companion_drawing", ROOT / "companion" / "modules" / "drawing.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        print(USAGE.rstrip())
        return 0
    names = args or list(SUBJECTS)
    unknown = [n for n in names if n not in SUBJECTS]
    if unknown:
        print(f"Unknown icon name(s): {', '.join(unknown)}\n\n{USAGE.rstrip()}", file=sys.stderr)
        return 1

    drawing = _load_drawing()

    def draw(name: str) -> str:
        raw, why = drawing.generate(STYLE.format(subject=SUBJECTS[name]))
        if raw is None:
            return f"{name}: failed ({why[:120]})"
        OUT.mkdir(parents=True, exist_ok=True)
        drawing.cut_out(raw, 192).save(OUT / f"{name}.webp", "WEBP", quality=90)
        return f"{name}: ok"

    with ThreadPoolExecutor(3) as pool:
        for line in pool.map(draw, names):
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
