"""检测: a platform's models and prices put in one shape, the list it gives, and 用量 of pictures and clips."""

import json
import time
import unittest

from _loader import load


class PriceTest(unittest.TestCase):
    def setUp(self):
        _, (self.check,) = load("model_check")

    def test_every_style_in_one_shape(self):
        clean = self.check.clean_price
        self.assertEqual(clean({"unit": "second", "currency": "CNY", "rows": [{"spec": "768P", "price": 0.26}, {"spec": "480P", "price": "x"}]}),
                         {"unit": "second", "rows": [{"spec": "768P", "price": 0.26}]})
        # Dollars become 元; a tier without both prices is dropped.
        self.assertEqual(clean({"unit": "mtok", "currency": "USD", "rows": [{"spec": "", "input": 1, "output": 4, "cached": 0.1},
                                                                            {"spec": "长", "input": 2}]}),
                         {"unit": "mtok", "rows": [{"spec": "", "input": 7.2, "output": 28.8, "cached": 0.72}]})
        self.assertIsNone(clean({"unit": "minute", "rows": [{"price": 1}]}))
        self.assertIsNone(clean({"unit": "image", "rows": []}))

    def test_a_price_in_the_wrong_shape_is_a_misread(self):
        models = self.check._clean([
            {"id": "MiniMax-H3-Lite", "name": "H3 Lite", "price": {"unit": "second", "rows": [{"spec": "768P", "price": 0.26}]}},
            {"id": "MiniMax-H3-Lite", "name": "again"},  # each model once
            {"id": "has space", "name": "x"},
            {"id": "image-01", "price": {"unit": "mtok", "rows": [{"input": 1, "output": 2}]}},
        ], "video")
        self.assertEqual([m["id"] for m in models], ["MiniMax-H3-Lite", "image-01"])
        self.assertIsNone(models[1]["price"])

    def test_what_a_clip_or_a_picture_cost(self):
        cost = self.check.cost_of
        per_second = {"unit": "second", "rows": [{"spec": "768P", "price": 0.5}, {"spec": "2K", "price": 0.8}]}
        self.assertEqual(cost(per_second, seconds=4, resolution="768p"), 2.0)
        self.assertEqual(cost(per_second, seconds=4, resolution="720p"), 2.0)  # sold as 768P
        self.assertEqual(cost(per_second, seconds=4, resolution="2k"), 3.2)
        per_clip = {"unit": "video", "rows": [{"spec": "768P·6秒", "price": 2.0}, {"spec": "768P·10秒", "price": 4.0},
                                              {"spec": "1080P·6秒", "price": 3.5}]}
        self.assertEqual(cost(per_clip, seconds=10, resolution="768P"), 4.0)
        self.assertEqual(cost(per_clip, seconds=6, resolution="1080p"), 3.5)
        self.assertEqual(cost({"unit": "image", "rows": [{"spec": "", "price": 0.025}]}, count=4), 0.1)
        self.assertIsNone(cost(None, seconds=4))


class CheckedListTest(unittest.TestCase):
    def test_the_platform_list_first_and_what_it_dropped_marked(self):
        folder, (catalog, check) = load("model_catalog", "model_check")
        catalog._registry = lambda: {}
        (folder / "model-check.json").write_text(json.dumps({"minimax": {"video": {
            "checked_at": time.time(), "sources": ["https://platform.minimax.cn/docs/guides/pricing-paygo"],
            "models": [{"id": "MiniMax-H3-Lite", "name": "MiniMax-H3-Lite", "note": "便宜",
                        "price": {"unit": "second", "rows": [{"spec": "768P", "price": 0.26}]}},
                       {"id": "MiniMax-Hailuo-2.3-Fast", "name": "MiniMax-Hailuo-2.3-Fast", "note": "",
                        "price": {"unit": "video", "rows": [{"spec": "768P·6秒", "price": 1.35}]}}]}}}), encoding="utf-8")
        listed = catalog.vendor_models("minimax", "video")
        ids = [m["id"] for m in listed["models"]]
        self.assertEqual(ids[:2], ["MiniMax-H3-Lite", "MiniMax-Hailuo-2.3-Fast"])
        lite = listed["models"][0]
        self.assertEqual((lite["name"], lite["price"]["rows"][0]["price"], lite["checked"]), ("海螺 H3 Lite", 0.26, True))
        self.assertTrue(next(m for m in listed["models"] if m["id"] == "MiniMax-H3-Max")["missing"])
        self.assertTrue(listed["checked"]["sources"])


class MediaUsageTest(unittest.TestCase):
    def test_pictures_and_clips_are_counted_and_priced(self):
        folder, (models,) = load("models")
        data = models.read_models()
        data["media"]["video"]["models"]["海螺"] = {"vendor": "minimax", "backend": "companion-minimax", "provider": "p",
                                                  "model": "MiniMax-H3-Lite",
                                                  "price": {"unit": "second", "rows": [{"spec": "768P", "price": 0.26}]}}
        data["media"]["video"]["active"] = "海螺"
        data["media"]["image"]["models"]["画"] = {"vendor": None, "backend": "openai", "provider": "p", "model": "img"}
        data["media"]["image"]["active"] = "画"
        models._write_json(models._models_path(), data)
        for _ in range(4):
            models.record_media("video", seconds=4, resolution="768p")
        models.record_media("image", count=2)
        rows = {r["name"]: r for r in models._media_period(time.time() - 60, models.read_models())}
        self.assertEqual((rows["海螺"]["count"], rows["海螺"]["seconds"], rows["海螺"]["cost"]), (4, 16.0, 4.16))
        self.assertEqual((rows["画"]["count"], rows["画"]["cost"]), (2, None))  # no price yet
        self.assertEqual(models._media_period(time.time() + 60, models.read_models()), [])

    def test_a_subscription_is_paid_by_the_plan(self):
        _, (models,) = load("models")
        data = models.read_models()
        data["presets"]["Sol"] = {"provider": "openai-codex", "auth": True, "model": "gpt-6.1-sol"}
        data["router"]["models"]["Sol"] = {"price_in": 14.4, "price_out": 72.0}  # the API's price, not the plan's
        data["media"]["image"]["models"]["GPT Image 2"] = {"vendor": "chatgpt", "backend": "openai-codex",
                                                            "provider": "openai-codex", "model": "gpt-image-2-medium"}
        data["media"]["image"]["active"] = "GPT Image 2"
        models._write_json(models._models_path(), data)
        period = models._period([("gpt-6.1-sol", "", None, "chat", 3, 1_000_000, 50_000, 0)], models.read_models())
        self.assertEqual((period["by_model"][0]["cost"], period["by_model"][0]["subscription"]), (0.0, True))
        models.record_media("image")
        row = models._media_period(time.time() - 60, models.read_models())[0]
        self.assertEqual((row["cost"], row["subscription"]), (0.0, True))

    def test_a_check_prices_the_models_already_added(self):
        _, (models,) = load("models")
        models._services = lambda: {}
        data = models.read_models()
        data["media"]["video"]["models"]["海螺"] = {"vendor": "minimax", "backend": "b", "provider": "p", "model": "MiniMax-H3-Lite"}
        data["media"]["video"]["models"]["别家"] = {"vendor": "fal", "backend": "b", "provider": "p", "model": "MiniMax-H3-Lite"}
        models._write_json(models._models_path(), data)
        price = {"unit": "second", "rows": [{"spec": "768P", "price": 0.26}]}
        models.update_checked_prices("minimax", "video", {"MiniMax-H3-Lite": price})
        after = models.read_models()["media"]["video"]["models"]
        self.assertEqual(after["海螺"]["price"], price)
        self.assertNotIn("price", after["别家"])  # another company's model of the same name


if __name__ == "__main__":
    unittest.main()
