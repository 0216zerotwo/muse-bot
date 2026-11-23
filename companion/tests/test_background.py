"""Background work: learning habits, reading a model's JSON, the feed's posts, drawings, usage."""

import io
import json
import shutil
import subprocess
import unittest

from _loader import load


class HabitLearningTest(unittest.TestCase):
    def setUp(self):
        self.folder, (self.helpers, self.learn) = load("helpers", "helper_learn")
        self.helpers.notice = lambda helper, text: None  # no running app to show it in

    def test_checks_what_the_model_proposes(self):
        self.helpers._save({"helpers": [{"id": "a", "name": "行程", "chat_id": "c", "habits": [
            {"id": "u1", "text": "靠窗座位", "by": "you"}, {"id": "l1", "text": "每天最多四个点", "by": "learned"}]}]})
        made = self.learn.apply(self.helpers._read(), [
            {"helper": "行程", "op": "add", "text": "景点之间留出休息"},
            {"helper": "行程", "op": "remove", "id": 1},  # the user's own: kept
            {"helper": "行程", "op": "update", "id": 2, "text": "每天最多三个点"},
            {"helper": "没有这个", "op": "add", "text": "x"},
            {"helper": "行程", "op": "add", "text": "密码是 123"},
        ])
        self.assertEqual(made, 2)
        self.assertEqual([h["text"] for h in self.helpers.habits_of(self.helpers._read()[0])],
                         ["靠窗座位", "景点之间留出休息", "每天最多三个点"])


class AskJsonTest(unittest.TestCase):
    def test_reads_the_object_in_an_answer(self):
        _, (llm,) = load("llm")
        llm.ask = lambda *a, **k: '好的：\n```json\n{"add": [1]}\n```'
        self.assertEqual(llm.ask_json("s", "u"), {"add": [1]})
        llm.ask = lambda *a, **k: "没有可改的"
        self.assertEqual(llm.ask_json("s", "u"), {})


class FeedPostTest(unittest.TestCase):
    def setUp(self):
        _, (self.feed,) = load("feed")
        self.feed._cover_image = lambda link: None  # no network

    def post(self, **args):
        return json.loads(self.feed._post({"title": "新闻", "area": "tech", **args}))

    def test_a_post_always_leads_to_its_source(self):
        result = self.post(body="发生了一件事。", link="https://example.com/a")
        self.assertTrue(result["success"])
        body = self.feed._read()["posts"][0]["body"]
        self.assertIn("[阅读原文](https://example.com/a)", body)

    def test_linked_key_words_are_kept_and_http_counts(self):
        self.assertTrue(self.post(body="[一件事](http://example.com/b)发生了。")["success"])
        self.assertNotIn("阅读原文", self.feed._read()["posts"][0]["body"])

    def test_no_link_at_all_is_turned_back(self):
        self.assertFalse(self.post(body="发生了一件事。")["success"])

    def test_a_new_post_tells_the_apps_at_once(self):
        import asyncio

        sent = []

        class Adapter:
            async def broadcast(self, event):
                sent.append(event)

        loop = asyncio.new_event_loop()
        try:
            self.feed.bind(Adapter(), loop)
            self.post(body="发生了一件事。", link="https://example.com/c")
            loop.run_until_complete(asyncio.sleep(0.05))
        finally:
            self.feed.bind(None, None)
            loop.close()
        self.assertEqual(sent, [{"event": "feed_updated"}])


class DrawingTest(unittest.TestCase):
    def test_cuts_the_plain_background_away(self):
        from PIL import Image

        _, (drawing,) = load("drawing")
        buffer = io.BytesIO()
        picture = Image.new("RGB", (200, 200), "white")
        picture.paste((200, 80, 20), (60, 60, 140, 140))
        picture.save(buffer, "PNG")
        out = drawing.cut_out(buffer.getvalue(), 64)
        self.assertEqual(out.size, (64, 64))
        self.assertEqual(out.getpixel((0, 0))[3], 0)
        self.assertEqual(out.getpixel((32, 32)), (200, 80, 20, 255))


class MotionLoopTest(unittest.TestCase):
    """A clip of the look (on white) becomes a transparent animation that loops without a jump."""

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_clip_to_loop(self):
        from PIL import Image

        folder, (motion,) = load("avatar_motion")
        still = folder / "still.png"
        picture = Image.new("RGB", (240, 240), "white")
        picture.paste((40, 90, 200), (90, 70, 150, 190))  # a "character": a blue block
        picture.save(still)
        clip = folder / "clip.mp4"
        # Two seconds of it sinking steadily (a clip that doesn't come back by itself).
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "color=white:s=240x280:d=2:r=24",
                        "-loop", "1", "-i", str(still), "-filter_complex",
                        "[0][1]overlay=0:'20*t':shortest=1,format=yuv420p", str(clip)],
                       check=True, capture_output=True)
        out = Image.open(io.BytesIO(motion.loop_from(clip, side=96)))
        frames = []
        for i in range(out.n_frames):
            out.seek(i)
            frames.append(out.convert("RGBA"))
        self.assertGreater(len(frames), 30)  # 12 fps, forward and back
        self.assertEqual(frames[0].size, (96, 96))
        self.assertEqual(frames[0].getpixel((0, 0))[3], 0)  # the white is gone

        def top(frame):  # where the character's top edge is
            alpha = frame.getchannel("A")
            return next(y for y in range(alpha.height) if any(alpha.getpixel((x, y)) > 128 for x in range(alpha.width)))

        tops = [top(f) for f in frames]
        # Out and back: it ends next to where it starts (no jump when it repeats), furthest away halfway.
        self.assertLessEqual(abs(tops[-1] - tops[0]), 2)
        self.assertGreater(tops[len(tops) // 2] - tops[0], 8)


class MotionClipFilingTest(unittest.TestCase):
    def test_each_clip_goes_to_the_videos(self):
        folder, (motion, creations) = load("avatar_motion", "creations")
        look = motion.avatars.avatars_root() / "abcdef123456"
        look.mkdir(parents=True)
        (look / "meta.json").write_text(json.dumps({"id": "abcdef123456", "name": "小鲸", "files": {}}), encoding="utf-8")
        clip = folder / "clip.mp4"
        clip.write_bytes(b"\0" * 64)
        motion.file_clip("abcdef123456", "waiting", clip)
        motion.file_clip("abcdef123456", "waiting", clip)  # the same clip: filed once
        videos = sorted(p.name for p in (creations.creations_root() / "视频").iterdir())
        self.assertEqual(len(videos), 1)
        self.assertIn("形象动作·等回复：小鲸", videos[0])


class LookLinksTest(unittest.TestCase):
    def test_a_remade_file_gets_a_new_link(self):
        import os

        _, (avatars,) = load("avatars")
        look = avatars.avatars_root() / "abcdef123457"
        look.mkdir(parents=True)
        (look / "meta.json").write_text(json.dumps({"id": "abcdef123457", "kind": "image", "character": True,
                                                    "files": {"image": "image.webp", "motion_idle": "motion_idle.webp"}}))
        for name in ("image.webp", "motion_idle.webp"):
            (look / name).write_bytes(b"x")
        link = lambda: avatars.list_avatars(lambda p: "/f/" + p.name)["items"][0]["motions"]["idle"]  # noqa: E731
        before = link()
        self.assertTrue(before.startswith("/f/motion_idle.webp?v="))
        os.utime(look / "motion_idle.webp", ns=(0, (look / "motion_idle.webp").stat().st_mtime_ns + 5_000_000_000))
        self.assertNotEqual(link(), before)


class LookCandidatesTest(unittest.TestCase):
    def test_four_to_pick_from_then_the_picked_one(self):
        from PIL import Image

        _, (make,) = load("avatar_make")

        def drawn(color):
            picture = Image.new("RGB", (200, 200), "white")
            picture.paste(color, (60, 40, 140, 170))
            out = io.BytesIO()
            picture.save(out, "PNG")
            return out.getvalue()

        colors = iter([(200, 40, 40), (40, 160, 40), None, (40, 40, 200)])
        make.generate = lambda prompt, reference=None: (lambda c: (drawn(c), "") if c else (None, "busy"))(next(colors))
        finished = []
        make._finish = lambda main, why, description, name: finished.append((main, description)) or {"avatar": "image:x"}

        result = make.make("戴围巾的小企鹅")
        self.assertEqual(result["count"], 3)  # one failed: the three that came out
        card = json.loads(result["card"].split("\n")[1])
        self.assertEqual([i["send"] for i in card["items"]], ["就选第 1 张", "就选第 2 张", "就选第 3 张"])

        shown = make.sign_picture_cards("挑一张吧\n" + result["card"], lambda rel: "/signed/" + rel)
        items = json.loads(shown.split("```card\n")[1].split("\n```")[0])["items"]
        self.assertEqual(items[0]["url"], f"/signed/_candidates/{result['batch']}/1.webp")
        self.assertNotIn("file", items[0])
        # A card naming a file outside the candidates shows nothing.
        self.assertEqual(make.sign_picture_cards('```card\n{"type":"pictures","items":[{"file":"../meta.json","label":"1"}]}\n```',
                                                 str), "")

        # The reply shows the card even when the model leaves it out, and only once.
        make._unshown_card = result["card"]
        self.assertEqual(make.with_pick_card("画好了四张，挑一张吧"), "画好了四张，挑一张吧\n\n" + result["card"])
        self.assertEqual(make.with_pick_card("还有别的吗"), "还有别的吗")
        make._unshown_card = result["card"]
        self.assertEqual(make.with_pick_card("挑吧\n" + result["card"]), "挑吧\n" + result["card"])

        with self.assertRaises(ValueError):
            make.pick(4)
        second = (make._candidates_root() / result["batch"] / "2.png").read_bytes()
        make.pick(2)
        self.assertEqual(finished[0], (second, "戴围巾的小企鹅"))  # made from the full picture picked
        with self.assertRaises(ValueError):  # picked: the candidates are gone
            make.pick(1)


class CompanionStateTest(unittest.TestCase):
    def test_appearance_and_feed_read_are_kept(self):
        _, (state,) = load("companion_state")
        saved = state.update_companion_state({"theme_mode": "dark", "pet_size": "large", "feed_seen_at": 1793000000000})
        self.assertEqual((saved["theme_mode"], saved["pet_size"], saved["feed_seen_at"]), ("dark", "large", 1793000000000))
        self.assertEqual(state.read_companion_state()["theme_mode"], "dark")
        for bad in ({"theme_mode": "blue"}, {"pet_size": "huge"}, {"feed_seen_at": -1}):
            with self.assertRaises(ValueError):
                state.update_companion_state(bad)


class DrawingRulesTest(unittest.TestCase):
    def test_it_knows_its_look_and_shows_every_picture(self):
        from PIL import Image

        _, (state,) = load("companion_state")
        words, picture = state.own_look_reference()
        self.assertIn("橘猫", words)
        with Image.open(picture) as opened:
            self.assertEqual(opened.mode, "RGB")  # on white, ready as a reference
        prompt = state.companion_identity_prompt() or ""
        if prompt:  # only once a state file exists
            self.assertIn(picture, prompt)
        lines = "\n".join(state._drawing_lines({}))
        self.assertIn("画了几张就发几张", lines)
        self.assertIn("不要先用 vision 检查", lines)


class RouterTest(unittest.TestCase):
    def test_asked_to_keep_it_short_is_text(self):
        _, (router,) = load("router")
        self.assertEqual(router.quick_choice("用一句话解释什么是复利")["form"], "text")
        self.assertEqual(router.quick_choice("简单说说房贷怎么算")["form"], "text")
        self.assertEqual(router.quick_choice("帮我算算房贷，30 年等额本息")["form"], "ui")  # not asked to be brief


class UsageTest(unittest.TestCase):
    def test_cache_hits_cost_a_tenth(self):
        _, (models,) = load("models")
        models._info = lambda data, preset: {"price_in": 2.0, "price_out": 8.0}
        rows = [("m", "u", None, "companion", 10, 1_000_000, 100_000, 900_000)]
        period = models._period(rows, {"presets": {"flash": {"model": "m"}}})
        # 100k fresh + 900k cached at a tenth = 190k billed input, 100k output.
        self.assertAlmostEqual(period["cost"], 0.19 * 2.0 + 0.1 * 8.0)
        self.assertEqual((period["input_tokens"], period["cached_tokens"]), (1_000_000, 900_000))


if __name__ == "__main__":
    unittest.main()
