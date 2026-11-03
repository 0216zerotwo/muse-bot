"""The server browser's viewer input: 复制 hands the page's selection to the phone."""

import asyncio
import unittest

from _loader import load


class _Page:
    """A page whose selection is ``selected``: answers Runtime.evaluate like CDP does."""

    def __init__(self, selected):
        self.selected = selected
        self.asked = []

    async def call(self, method, params=None):
        self.asked.append(params["expression"])
        if isinstance(self.selected, Exception):
            raise self.selected
        return {"result": {"value": self.selected}}

    async def send(self, method, params=None):
        return 0


class _Viewer:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


class CopyTest(unittest.TestCase):
    def setUp(self):
        _, (self.computer,) = load("computer")

    def copy(self, selected):
        viewer = _Viewer()
        page = _Page(selected)
        asyncio.run(self.computer._input(page, {"w": 1280.0, "h": 800.0}, {"type": "copy"}, viewer))
        return page, viewer.sent

    def test_the_selection_goes_to_the_phone(self):
        page, sent = self.copy("苹果发布新款 MacBook Pro")
        self.assertEqual(sent, [{"type": "copied", "text": "苹果发布新款 MacBook Pro"}])
        self.assertIn("password", page.asked[0])  # a password field is never read

    def test_nothing_selected_or_a_failed_read_is_empty(self):
        self.assertEqual(self.copy("")[1], [{"type": "copied", "text": ""}])
        self.assertEqual(self.copy(asyncio.TimeoutError())[1], [{"type": "copied", "text": ""}])


class ReopenTest(unittest.TestCase):
    """A browser closed at a turn's end opens its pages again when it is needed soon after."""

    def setUp(self):
        _, (self.computer,) = load("computer")

    def test_a_restart_soon_after_opens_the_pages_it_had(self):
        c = self.computer
        c._remember_closed(["https://www.douyin.com/?recommend=1", c.START_PAGE, "https://www.apple.com.cn/shop/buy-mac"])
        self.assertEqual(c._reopen(), ["https://www.douyin.com/?recommend=1", "https://www.apple.com.cn/shop/buy-mac"])
        self.assertEqual(c._reopen(), [c.START_PAGE])  # once: a later start begins fresh

    def test_after_half_an_hour_it_starts_fresh(self):
        c = self.computer
        c._remember_closed(["https://www.douyin.com/"])
        c._closed_at -= 31 * 60
        self.assertEqual(c._reopen(), [c.START_PAGE])

    def test_a_hold_on_a_browser_that_is_gone_is_let_go(self):
        c = self.computer
        c.hand_over("扫码登录抖音", "chat-1")
        c.running = lambda: True
        self.assertTrue(c.holding())
        c.running = lambda: False
        self.assertFalse(c.holding())
        self.assertIsNone(c.held())
        self.assertIsNone(c.release())  # the request went with it


if __name__ == "__main__":
    unittest.main()
