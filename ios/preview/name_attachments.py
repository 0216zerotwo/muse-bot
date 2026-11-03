"""Copy the screenshots exported from the result bundle under their own names (01-chat.png …).
Xcode's own attachments (element descriptions on a failed tap) get a safe file name, since
artifact paths may not contain quotes and the like.

    python3 preview/name_attachments.py <exported dir> <out dir>
"""
import json
import re
import shutil
import sys
from pathlib import Path

source, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
extra = 0
for test in json.loads((source / "manifest.json").read_text()):
    for item in test.get("attachments", []):
        exported = item["exportedFileName"]
        name = (item.get("suggestedHumanReadableName") or exported).split("_0_")[0]
        if re.fullmatch(r"\d\d-[a-z0-9-]+(\.png)?", name):
            name = name if name.endswith(".png") else name + ".png"
        else:
            extra += 1
            name = f"zz-xcode-{extra:02d}" + Path(exported).suffix
        target = out / name
        if not target.exists():
            shutil.copy(source / exported, target)
print(len(list(out.glob("[0-9]*.png"))), "screenshots,", extra, "other attachments")
