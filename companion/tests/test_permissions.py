import importlib.util
import sys
import unittest

from _loader import ROOT, load


def approvals():
    spec = importlib.util.spec_from_file_location("companion_approvals_test", ROOT / "approvals.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it loads
    spec.loader.exec_module(module)
    return module


class PermissionsTest(unittest.TestCase):
    def setUp(self):
        _, (self.permissions,) = load("permissions")

    def test_sorts_calls_into_kinds(self):
        kind = self.permissions.kind_of
        self.assertEqual(kind("mcp_browser_browser_navigate", {"url": "https://a.cn"}, None, True), "browse")
        pay = {"element": "立即付款"}
        self.assertEqual(kind("mcp_browser_browser_click", pay, approvals().approval_action("mcp_browser_browser_click", pay), True),
                         "payment")
        self.assertEqual(kind("terminal", {"command": "dir"}, None, False), "run_command")
        self.assertEqual(kind("cronjob", {}, None, False), "schedule")
        self.assertIsNone(kind("web_search", {"query": "天气"}, None, False))

    def test_levels_are_kept_and_paying_is_never_on_its_own(self):
        self.permissions.set_level("browse", "deny")
        self.assertEqual(self.permissions.level_of("browse"), "deny")
        with self.assertRaises(ValueError):
            self.permissions.set_level("payment", "allow")
        self.assertEqual(self.permissions.level_of(None), "allow")


class BrowserExecGateTest(unittest.TestCase):
    """Hermes's own browser tool runs a script: the gate reads what the script clicks."""

    def setUp(self):
        self.action = approvals().approval_action
        _, (self.permissions,) = load("permissions")

    def kind(self, code):
        args = {"code": code}
        return self.permissions.kind_of("browser_exec", args, self.action("browser_exec", args), True)

    def test_reading_a_page_runs_on_its_own(self):
        self.assertEqual(self.kind("new_tab('https://example.com'); print(page_info())"), "browse")
        self.assertEqual(self.kind("print(js('document.body.style.display'))"), "browse")

    def test_consequential_clicks_and_uploads_are_held(self):
        find = "n = [x for x in nodes if x['name'] == '{}'][0]; click_at_xy(10, 20)"
        self.assertEqual(self.kind(find.format("立即付款")), "payment")
        self.assertEqual(self.kind(find.format("提交订单")), "payment")
        self.assertEqual(self.kind(find.format("发送")), "browser_submit")
        self.assertEqual(self.kind(find.format("下一页")), "browse")
        self.assertEqual(self.kind("cdp('DOM.setFileInputFiles', files=['a.pdf'])"), "browser_upload")

    def test_a_store_page_address_is_not_an_order(self):
        # Reading the education store's prices: the address says "buy-mac", the click opens an FAQ.
        reading = (
            'goto_url("https://www.apple.com.cn/cn-edu/shop/buy-mac/macbook-air/13-inch-m5-32gb-memory-512gb-storage")\n'
            "j = js(\"\"\"(() => {\n"
            "  const b=[...document.querySelectorAll('button')].find(x=>/特别折扣或促销优惠/.test(x.innerText||''));\n"
            "  if(b){b.click(); return 'ok';} return 'no';\n"
            "})()\"\"\")"
        )
        self.assertIsNone(self.action("browser_exec", {"code": reading}))
        self.assertEqual(self.kind(reading), "browse")
        # On the same page, pressing 立即购买 is still held.
        buying = 'goto_url("https://www.apple.com.cn/shop/buy-mac/macbook-air")\n' \
                 "n = [x for x in nodes if x['name'] == '立即购买'][0]; click_at_xy(10, 20)"
        self.assertEqual(self.action("browser_exec", {"code": buying}), "browser_submit")


class BrowserCardWordsTest(unittest.TestCase):
    """A held step in the browser is shown in words: where, what it presses, and what that does."""

    def setUp(self):
        self.approvals = approvals()
        _, (self.permissions,) = load("permissions")

    def test_says_the_site_page_and_button(self):
        code = "n = [x for x in nodes if x['name'] == '立即购买'][0]; click_at_xy(10, 20)"
        page = {"url": "https://www.apple.com.cn/shop/buy-mac/macbook-air", "title": "购买 MacBook Air - Apple (中国大陆)"}
        words = self.approvals.browser_context({"code": code}, "browser_submit", page, paying=True)
        self.assertEqual(words["host"], "apple.com.cn")
        self.assertEqual(words["target"], "立即购买")
        self.assertTrue(words["summary"].startswith("要在 apple.com.cn「购买 MacBook Air」页面上点「立即购买」。"))
        self.assertIn("付款", words["summary"])

    def test_a_script_that_goes_somewhere_names_that_site(self):
        code = """goto_url("https://mail.example.com/compose")
fill_input("#to", "a@b.cn"); x = "发送"; js("document.querySelector('.send').click()")"""
        words = self.approvals.browser_context({"code": code}, "browser_submit", {"url": "https://other.cn/", "title": "别处"})
        self.assertEqual(words["host"], "mail.example.com")
        self.assertEqual(words["page"], "")
        self.assertEqual(words["summary"], "要在 mail.example.com 上点「发送」。点了就会提交出去，可能没法撤回。")

    def test_an_upload_names_its_files(self):
        words = self.approvals.browser_context({"paths": ["E:/docs/简历.pdf"]}, "browser_upload", None)
        self.assertEqual(words["summary"], "要把「简历.pdf」上传到网页上。传上去以后，这个网站就能看到文件内容。")
        self.assertIsNone(self.approvals.browser_context({}, "send_email"))

    def test_an_address_does_not_make_a_click_a_payment(self):
        code = """goto_url("https://www.apple.com.cn/shop/buy-mac/macbook-air")
n = [x for x in nodes if x['name'] == '提交问题'][0]; click_at_xy(10, 20)"""
        args = {"code": code}
        self.assertEqual(self.permissions.kind_of("browser_exec", args, self.approvals.approval_action("browser_exec", args), True),
                         "browser_submit")

    def test_the_card_gets_every_context(self):
        record = self.approvals.ApprovalRecord(id="apv_1", session_key="s", call_id="c", tool_name="browser_exec",
                                               arguments={}, action="browser_submit",
                                               extra={"browser": {"summary": "要在 a.cn 上点「发送」。点了就会提交出去。"},
                                                      "egress": {"host": "a.cn"}})
        self.assertEqual(set(record.public()["context"]), {"browser", "egress"})
        self.assertEqual(self.approvals.describe_approval(record), "要在 a.cn 上点「发送」")


if __name__ == "__main__":
    unittest.main()
