"""The companion's own wording for notices: each one replaces a notice that exists in ``muse.notices``
and asks for no placeholder value the notice isn't given."""

import string
import unittest

from _loader import load


def _fields(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class WordingTest(unittest.TestCase):
    def test_every_rewording_fits_the_notice_it_replaces(self):
        from muse import notices

        _, (persona,) = load("persona")
        for key, text in persona._COMPANION_ZH.items():
            with self.subTest(key=key):
                self.assertIn(key, notices.NOTICE_PLACEHOLDERS)
                self.assertLessEqual(_fields(text), notices.NOTICE_PLACEHOLDERS[key])

    def test_a_failed_reply_points_at_the_app_not_at_commands(self):
        _, (persona,) = load("persona")
        for key, text in persona._COMPANION_ZH.items():
            if key.startswith("gateway.errors."):
                with self.subTest(key=key):
                    self.assertNotRegex(text, r"/retry|/model|/new|/compress|hermes ")


if __name__ == "__main__":
    unittest.main()
