"""Memory fades: what has not come up for long is shortened, then forgotten; who the user is stays."""

import unittest

from _loader import load

DAY = 24 * 3600
NOW = 1_800_000_000.0


class MemoryFadeTest(unittest.TestCase):
    def setUp(self):
        self.folder, (self.tidy, self.fade, self.learn) = load("memory_tidy", "memory_fade", "memory_learn")
        self.meta = self.tidy._read_meta()

    def seen(self, entry, days_ago):
        self.meta["seen_at"][self.tidy.entry_key(entry)] = NOW - days_ago * DAY

    def test_plans_fade_then_go_but_who_the_user_is_stays(self):
        trip = "【近况与计划】在比较 Instabox 和 DMIT 的服务器，准备把伙伴部署上去"
        old_trip = "【近况与计划】十月在追美股科技股的跌幅"
        hobby = "【喜好与习惯】周末喜欢去西湖边骑车"
        school = "【基本信息】软件工程硕士在读"
        mine = "【近况与计划】每周日称一次体重"
        note = "美股行情：Yahoo Finance 的接口可以直接取"
        for entry, days in ((trip, 25), (old_trip, 50), (hobby, 30), (school, 400), (mine, 300), (note, 61)):
            self.seen(entry, days)
        self.meta["user_written"] = [mine]
        shorten, forget = self.fade.plan({"user": [trip, old_trip, hobby, school, mine], "memory": [note]}, self.meta, NOW)
        self.assertEqual([e for _, e, _ in shorten], [trip])
        self.assertEqual({e for _, e, _ in forget}, {old_trip, note})

    def test_a_shortened_line_is_not_shortened_again_and_new_lines_start_their_clock(self):
        trip = "【近况与计划】在比较 Instabox 和 DMIT 的服务器"
        new = "【近况与计划】下周去杭州"
        self.seen(trip, 25)
        self.meta["faded"] = [self.tidy.entry_key(trip)]
        shorten, forget = self.fade.plan({"user": [trip, new], "memory": []}, self.meta, NOW)
        self.assertEqual((shorten, forget), ([], []))
        self.assertEqual(self.meta["seen_at"][self.tidy.entry_key(new)], NOW)

    def test_coming_up_again_starts_the_clock_over(self):
        trip = "【近况与计划】在比较服务器"
        self.seen(trip, 40)
        self.meta["faded"] = [self.tidy.entry_key(trip)]
        self.tidy.mark_seen(self.meta, [trip], NOW)
        self.assertEqual(self.tidy.seen_at(self.meta, trip), NOW)
        self.assertEqual(self.meta["faded"], [])

    def test_the_change_log_dates_lines_learned_before(self):
        entry = "【喜好与习惯】喜欢爬山"
        self.meta["changes"] = [{"kind": "learn-add", "before": [], "after": entry, "at": NOW - 5 * DAY}]
        self.assertEqual(self.tidy.seen_at(self.meta, entry), NOW - 5 * DAY)

    def test_a_shorter_line_keeps_its_section_and_only_its_own_words(self):
        entry = "【近况与计划】在比较 Instabox 和 DMIT 两家的服务器价格和线路，准备把伙伴部署上去"
        self.assertEqual(self.fade.shortened(entry, "在比较 Instabox 和 DMIT 的服务器"), "【近况与计划】在比较 Instabox 和 DMIT 的服务器")
        self.assertIsNone(self.fade.shortened(entry, "在比较阿里云的服务器"))  # a word it never said
        self.assertIsNone(self.fade.shortened(entry, entry))  # not shorter
        self.assertIsNone(self.fade.shortened(entry, None))

    def test_an_entry_comes_up_when_the_user_talks_about_it(self):
        stocks = "【近况与计划】在关注美股科技股的跌幅"
        trip = "【近况与计划】下周去杭州看西湖"
        said = "用户：美股科技股今天又跌了吗\n用户：最近好累"
        self.assertEqual(self.learn.mentioned([stocks, trip], said), [stocks])
        self.assertEqual(self.learn.mentioned([trip], "用户：最近怎么样"), [])


if __name__ == "__main__":
    unittest.main()
