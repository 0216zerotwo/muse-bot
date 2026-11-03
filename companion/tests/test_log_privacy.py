"""The user's words stay out of the server's logs; the lines themselves (and everything else) stay."""

import logging
import unittest

from _loader import load


class LogPrivacyTest(unittest.TestCase):
    def setUp(self):
        _, (self.privacy,) = load("log_privacy")
        self.privacy.install()
        self.privacy.install()  # twice: still one filter

    def line(self, logger, msg, *args):
        record = logging.LogRecord(logger, logging.INFO, __file__, 1, msg, args, None)
        for f in logging.getLogger(logger).filters:
            f.filter(record)
        return record.getMessage()

    def test_a_message_is_logged_by_its_size(self):
        text = self.line("gateway.run", "inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r",
                         "companion", "我", "c1", "1w预算，macbook air高配还是pro低配", None, "")
        self.assertNotIn("macbook", text)
        self.assertIn("msg='<25 字>'", text)
        self.assertIn("chat=c1", text)

    def test_tool_failures_keep_the_tool_not_its_output(self):
        text = self.line("agent.tool_executor", "Tool %s returned error (%.2fs): %s", "execute_code", 25.06, '{"NVDA": 204.64}')
        self.assertEqual(text, "Tool execute_code returned error (25.06s): <16 字>")
        text = self.line("tools.terminal_tool", "Execution failed after %d retries - Command: %s - Error: %s: %s - Task: %s, Backend: %s",
                         3, "curl 'https://stooq.com/q/d/l/?s=orcl.us'", "RuntimeError", "No shell", "t", "local")
        self.assertNotIn("stooq", text)
        self.assertIn("No shell", text)  # the error itself is kept

    def test_other_lines_are_untouched(self):
        self.assertEqual(self.line("gateway.run", "response ready: platform=%s time=%.1fs", "companion", 18.7),
                         "response ready: platform=companion time=18.7s")
        self.assertEqual(len([f for f in logging.getLogger("gateway.run").filters if type(f).__name__ == "_WordsOut"]), 1)


if __name__ == "__main__":
    unittest.main()
