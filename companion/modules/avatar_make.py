"""换形象 by describing it, as Muse does: the user says what the companion should look like (in the
main chat, "把虚拟形象改为……"), and the companion draws it with the image model set in 模型.

First four candidates are drawn at once and shown in the chat as a card of pictures; the user picks
one (``avatar_pick``), and the rest is made from that one. One character, full-body on a plain background in the app's style (soft, rounded,
a simple friendly face, like the default cat Adam); then the same character in two more moods,
drawn from that first picture so it stays recognisably the same (Muse keeps one look across every
expression the same way, from a single reference picture). The background is cut away, the three
pictures become one look in the avatar library (``avatars``, kind "image", with ``working`` and
``happy`` moods the app shows while it works and when it is pleased), copies go to 资源库, and the
companion takes the new look; its colors follow (the app samples them from the picture).

``avatar_reset`` puts the default (Adam, the orange cat) back.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from . import avatars
from .drawing import cut_out, generate
from .companion_state import DEFAULT_COMPANION_AVATAR, update_companion_state

logger = logging.getLogger(__name__)

TOOLSET = "companion"

_LOOK = ("A cute full-body character for a personal AI companion app: {description}. "
         "Soft, plush and rounded, with a simple friendly face (small dot eyes, a little smile, rosy cheeks), "
         "standing and facing the viewer. Kawaii flat illustration with soft cel shading and a thin warm "
         "dark outline, warm and cozy colors. Centered with some space around it, on a plain pure white "
         "background. No text, no border, no props unless described, no shadow on the background.")
_MOODS = {
    "working": "The same character, exactly the same design and colors, busy and focused: typing on a tiny laptop. "
               "Same style, plain pure white background, no text.",
    "happy": "The same character, exactly the same design and colors, delighted: eyes closed in a big smile, "
             "one paw raised in a cheerful wave. Same style, plain pure white background, no text.",
}


CANDIDATES = 4
# The pick card of the last candidates drawn, until a reply has shown it (see ``with_pick_card``).
_unshown_card: Optional[str] = None
_CANDIDATE_DIR = "_candidates"
_KEEP_CANDIDATES_S = 7 * 24 * 3600


def make(description: str, name: str = "", reference: str = "", edit_current: bool = False) -> dict[str, Any]:
    """Draws four candidates for the user to pick from (``pick``). Blocking: a minute or two.
    ``reference``: a picture the user sent to base it on (a local file). ``edit_current``: change the
    look it wears now (only what ``description`` asks), drawn from its current picture."""
    description = " ".join(str(description or "").split())[:300]
    if not description:
        raise ValueError("要说清楚想要什么样子")
    if edit_current:
        with tempfile.TemporaryDirectory() as tmp:
            base = _current_picture(Path(tmp))
            prompt = (f"Redraw exactly the same character as in the reference picture — same design, proportions, "
                      f"colors and style — changing only this: {description}. Full body, standing, facing the viewer, "
                      "centered with some space around it, on a plain pure white background. No text, no border, "
                      "no shadow on the background.")
            return _candidates(prompt, str(base), description, name)
    if reference:
        from .secrets_guard import is_secret_path

        if is_secret_path(reference):
            raise ValueError("不能用这个文件做参考图")
    reference = reference if reference and Path(reference).is_file() else ""
    if reference:
        # First what the picture shows, in words (as Muse does): the drawing keeps to the details.
        details = _details_of(reference)
        if details:
            description = f"{description}. Details from the reference picture: {details}"[:1200]
    prompt = _LOOK.format(description=description)
    if reference:
        prompt = ("Based on the character in the reference picture (keep its look, colors and features), "
                  "redraw it as: " + prompt)
    return _candidates(prompt, reference or None, description, name)


def _candidates_root() -> Path:
    return avatars.avatars_root() / _CANDIDATE_DIR


def _card(card: dict[str, Any]) -> str:
    return "```card\n" + json.dumps(card, ensure_ascii=False) + "\n```"


def _candidates(prompt: str, reference: Optional[str], description: str, name: str) -> dict[str, Any]:
    """Draws the candidates at once and keeps them (the full picture too, to make the rest from)."""
    with ThreadPoolExecutor(CANDIDATES) as pool:
        drawn = list(pool.map(lambda _: generate(prompt, reference=reference), range(CANDIDATES)))
    good = [raw for raw, _ in drawn if raw is not None]
    if not good:
        logger.info("companion: drawing a look failed (%s)", drawn[0][1])
        raise ValueError("画形象没成功，检查一下「模型」里的图像模型")
    _tidy_candidates()
    batch = secrets.token_hex(4)
    folder = _candidates_root() / batch
    folder.mkdir(parents=True)
    for number, raw in enumerate(good, 1):
        (folder / f"{number}.png").write_bytes(raw)
        cut_out(raw, 384).save(folder / f"{number}.webp", "WEBP", quality=86)
    (folder / "batch.json").write_text(json.dumps({"description": description, "name": name, "count": len(good),
                                                   "created": time.time()}, ensure_ascii=False), encoding="utf-8")
    card = {"type": "pictures", "title": "选一个形象",
            "items": [{"file": f"{batch}/{n}.webp", "label": str(n), "send": f"就选第 {n} 张"} for n in range(1, len(good) + 1)]}
    return {"batch": batch, "count": len(good), "card": _card(card)}


def _tidy_candidates() -> None:
    """Candidates nobody picked go after a week."""
    root = _candidates_root()
    for folder in root.iterdir() if root.is_dir() else []:
        if folder.is_dir() and time.time() - folder.stat().st_mtime > _KEEP_CANDIDATES_S:
            shutil.rmtree(folder, ignore_errors=True)


def _latest_batch() -> str:
    root = _candidates_root()
    folders = [f for f in root.iterdir() if (f / "batch.json").is_file()] if root.is_dir() else []
    return max(folders, key=lambda f: f.stat().st_mtime).name if folders else ""


def pick(choice: int, batch: str = "") -> dict[str, Any]:
    """Makes the picked candidate the look: its moods, filing, wearing it, its motion."""
    batch = batch if re.fullmatch(r"[0-9a-f]{8}", batch or "") else _latest_batch()
    folder = _candidates_root() / batch if batch else None
    if folder is None or not (folder / "batch.json").is_file():
        raise ValueError("没有待选的形象，先用 avatar_create 画几张")
    info = json.loads((folder / "batch.json").read_text(encoding="utf-8"))
    picture = folder / f"{int(choice)}.png"
    if not picture.is_file():
        raise ValueError(f"只能选第 1 到 {info.get('count')} 张")
    made = _finish(picture.read_bytes(), "", str(info.get("description") or ""), str(info.get("name") or ""))
    shutil.rmtree(folder, ignore_errors=True)
    return made


def with_pick_card(text: str) -> str:
    """The companion's reply after drawing candidates, with their pick card: the model is asked to
    copy it in, and when it doesn't (or mangles it), the card is added at the end."""
    global _unshown_card
    card, _unshown_card = _unshown_card, None
    if card is None or '"pictures"' in text:
        return text
    return f"{text.rstrip()}\n\n{card}" if text.strip() else card


def sign_picture_cards(text: str, sign: Any) -> str:
    """Swaps each candidate's ``file`` in a pictures card for a signed link the app can show."""
    from .widgets import _FENCE

    def replace(match: re.Match) -> str:
        try:
            card = json.loads(match.group(1))
        except ValueError:
            return match.group(0)
        if not isinstance(card, dict) or card.get("type") != "pictures":
            return match.group(0)
        items = []
        for item in card.get("items") or []:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("file") or "")
            if re.fullmatch(r"[0-9a-f]{8}/[1-9][.]webp", rel) and (_candidates_root() / rel).is_file():
                items.append({**{k: v for k, v in item.items() if k != "file"}, "url": sign(f"{_CANDIDATE_DIR}/{rel}")})
        return _card({**card, "items": items}) if items else ""

    return _FENCE.sub(replace, text)


def _current_picture(folder: Path) -> Path:
    """The look it wears now, on white, as a picture file (Adam: the first frame of his sprite sheet)."""
    from PIL import Image

    from .companion_state import read_companion_state

    kind, _, avatar_id = str(read_companion_state().get("avatar") or "").partition(":")
    if kind == "image" and avatars.is_character(avatar_id):
        with Image.open(avatars.avatars_root() / avatar_id / "image.webp") as raw_img:
            picture = raw_img.convert("RGBA")
    else:
        with Image.open(_builtin_sheet()) as raw_sheet:
            picture = raw_sheet.convert("RGBA").crop((0, 0, avatars.CELL_WIDTH, avatars.CELL_HEIGHT))
        picture = picture.resize((picture.width * 4, picture.height * 4), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", picture.size, "white")
    canvas.paste(picture, mask=picture)
    path = folder / "current.png"
    canvas.save(path)
    return path


def _builtin_sheet() -> Path:
    """The built-in orange cat's sprite sheet: in the web app this server serves (a server's own copy,
    COMPANION_WEB_DIST), else in the repository's web app."""
    import os

    root = Path(__file__).resolve().parents[2]
    for folder in (os.environ.get("COMPANION_WEB_DIST"), root / "web" / "dist", root / "web" / "public"):
        sheet = Path(folder) / "pets" / "orange-cat" / "spritesheet.webp" if folder else None
        if sheet is not None and sheet.is_file():
            return sheet
    return root / "web" / "public" / "pets" / "orange-cat" / "spritesheet.webp"


def _finish(main: Optional[bytes], why: str, description: str, name: str) -> dict[str, Any]:
    """The rest of a new look from its main picture: the two moods, filing, wearing it, its motion."""
    if main is None:
        logger.info("companion: drawing a look failed (%s)", why)
        raise ValueError("画形象没成功，检查一下「模型」里的图像模型")
    pictures = {"image": cut_out(main, 768)}
    with tempfile.TemporaryDirectory() as tmp:
        reference = Path(tmp) / "reference.png"
        reference.write_bytes(main)
        for mood, prompt in _MOODS.items():
            raw, why = generate(prompt, reference=str(reference))
            if raw is not None:
                pictures[mood] = cut_out(raw, 768)
            else:  # the look still works with its one picture
                logger.info("companion: the %s picture of a new look failed (%s)", mood, why)
    meta = avatars.save_character(pictures, name=(name or description)[:40])
    _file_in_creations(meta, description)
    _wear(avatars.avatar_value(meta))
    # Then in motion: one short clip per state, made in the background by the video model.
    from .avatar_motion import animate_later

    moving = animate_later(meta["id"])
    return {"avatar": avatars.avatar_value(meta), "moods": [m for m in pictures if m != "image"], "moving": moving}


def _details_of(picture: str) -> str:
    """The character in a reference picture, described for drawing it again."""
    from model_tools import _run_async
    from tools.vision_tools import vision_analyze_tool

    ask = ("Describe the main character in this picture for an illustrator who must redraw it faithfully: "
           "species or type, body shape, hair (color, style), eyes, face, outfit and its colors, accessories, "
           "distinctive marks. English, one compact paragraph, no background, no guesses.")
    try:
        result = json.loads(_run_async(vision_analyze_tool(image_url=picture, user_prompt=ask)))
    except Exception as exc:
        logger.info("companion: could not look at the reference picture (%s)", exc)
        return ""
    text = result.get("analysis") if isinstance(result, dict) and result.get("success") else ""
    return " ".join(str(text or "").split())[:800]


def _wear(value: str) -> None:
    """Puts a look on; a drawn one it wore before goes (its pictures stay in 资源库)."""
    from .companion_state import read_companion_state

    before = str(read_companion_state().get("avatar") or "")
    update_companion_state({"avatar": value})
    kind, _, avatar_id = before.partition(":")
    if kind == "image" and before != value and avatars.is_character(avatar_id):
        avatars.delete(avatar_id)


def _file_in_creations(meta: dict[str, Any], description: str) -> None:
    from .creations import import_file

    folder = avatars.avatars_root() / meta["id"]
    labels = {"image": "形象", "working": "形象·忙碌", "happy": "形象·开心"}
    for mood, file_name in meta["files"].items():
        try:
            import_file(folder / file_name, key=f"avatar:{meta['id']}:{mood}", title=f"{labels.get(mood, '形象')}：{description[:20]}",
                        origin="generated", prompt=description)
        except Exception:
            logger.exception("companion: could not file the new look in 资源库")


# ── the companion's tools ─────────────────────────────────────────────────────────────────────────

def _handle_create(args: dict[str, Any], **_: Any) -> str:
    try:
        drawn = make(str(args.get("description") or ""), str(args.get("name") or ""),
                     str(args.get("reference_image") or ""), bool(args.get("edit_current")))
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    global _unshown_card
    _unshown_card = drawn["card"]
    return json.dumps({"success": True, "batch": drawn["batch"], "card": drawn["card"],
                       "note": f"画好了 {drawn['count']} 张候选，还没换上。回复里先用一句话请用户挑一张，再把 card 原样（整个 ```card 代码块，"
                               "一字不改）放进回复；用户选了以后（比如“就选第 2 张”）调用 avatar_pick。"},
                      ensure_ascii=False)


def _handle_pick(args: dict[str, Any], **_: Any) -> str:
    try:
        made = pick(int(args.get("choice") or 0), str(args.get("batch") or ""))
    except (ValueError, TypeError) as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    moving = ("它的动作（待机、思考、干活、等回复）正在后台做，几分钟后形象会动起来，做好了聊天里会提示。" if made.get("moving")
              else "要让形象动起来，需要在「模型」里添加一个视频模型。")
    return json.dumps({"success": True, **made,
                       "note": f"新形象已经换上了（三张图也放进了资源库）。{moving}用一两句话跟用户说说新样子，不要贴图片链接。"},
                      ensure_ascii=False)


def _handle_reset(args: dict[str, Any], **_: Any) -> str:
    _wear(DEFAULT_COMPANION_AVATAR)
    return json.dumps({"success": True, "note": "已经换回默认形象 Adam（橘猫）。"}, ensure_ascii=False)


_TOOLS = (
    ("avatar_create", "用户想给你换个样子（比如“把虚拟形象改为一只戴围巾的小企鹅”）时用：按描述画出四张候选形象（要一两分钟），"
                      "返回一张选图卡片给用户挑；用户挑好后用 avatar_pick 换上。描述太笼统时可以先问一句颜色、物种或风格，再画。",
     {"description": {"type": "string", "description": "新形象的样子：是什么、颜色、特征、配饰，用中文或英文写具体"},
      "name": {"type": "string", "description": "给这个形象起的名字（可不填）"},
      "edit_current": {"type": "boolean",
                       "description": "true：在现在的形象上改（只改 description 说的地方，比如“加一顶帽子”“头发换成粉色”）；"
                                      "false（默认）：按 description 画一个新的"},
      "reference_image": {"type": "string",
                          "description": "用户发来的参考图（消息里给出的图片本地路径）：照着它画；没有就不填"}},
     ["description"], _handle_create, "🎨"),
    ("avatar_pick", "用户从 avatar_create 的候选图里选了一张（“就选第 2 张”）时用：把这张做成你的形象并换上（再画忙碌、开心两张，"
                    "然后在后台做动作），要一两分钟。",
     {"choice": {"type": "integer", "description": "选的是第几张（1 到 4）"},
      "batch": {"type": "string", "description": "avatar_create 返回的 batch（不填就是最近一次的候选）"}},
     ["choice"], _handle_pick, "✅"),
    ("avatar_reset", "用户想换回原来的默认形象（Adam，橘猫）时用。", {}, [], _handle_reset, "🐱"),
)


def register_tools(ctx: Any) -> None:
    for name, description, properties, required, handler, emoji in _TOOLS:
        ctx.register_tool(name=name, toolset=TOOLSET, handler=handler, description=description, emoji=emoji,
                          schema={"name": name, "description": description,
                                  "parameters": {"type": "object", "properties": properties, "required": required}})
