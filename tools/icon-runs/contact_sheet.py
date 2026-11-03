"""A comparison sheet of icon candidates: each large, then at home-screen size with the app's name.

    python contact_sheet.py <folder> name1,name2,... "label1|label2|..."  → <folder>/candidates.png
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

folder, names, labels = Path(sys.argv[1]), sys.argv[2].split(","), sys.argv[3].split("|")
big, small = 300, 64
sheet = Image.new("RGB", (40 + len(names) * 340, 470), (244, 242, 238))
draw = ImageDraw.Draw(sheet)
font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 24)
tiny = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 15)


def rounded(image: Image.Image, size: int) -> Image.Image:
    small_image = image.resize((size, size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * 4 - 1, size * 4 - 1), radius=size * 4 * 0.225, fill=255)
    small_image.putalpha(mask.resize((size, size), Image.LANCZOS))
    return small_image


for i, (name, label) in enumerate(zip(names, labels)):
    image = Image.open(folder / f"{name}.png").convert("RGB")
    x = 40 + i * 340
    large = rounded(image, big)
    sheet.paste(large, (x, 30), large)
    draw.text((x, 345), label, fill=(40, 40, 40), font=font)
    icon = rounded(image, small)
    sheet.paste(icon, (x + 4, 385), icon)
    draw.text((x + 80, 405), "Muse Bot", fill=(60, 60, 60), font=tiny)
sheet.save(folder / "candidates.png")
print(folder / "candidates.png")
