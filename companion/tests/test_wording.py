"""The companion's own wording for Hermes's notices: each one replaces a notice that exists, and asks
for no value the notice isn't given (a missing one would stop it from showing at all)."""

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

        # Also cross-check Hermes's bundled i18n catalog whenever available in the environment.
        try:
            from agent import i18n
        except ImportError:
            return
        bundled = i18n._load_bundled("zh")
        for key, text in persona._COMPANION_ZH.items():
            with self.subTest(hermes_key=key):
                self.assertIn(key, bundled)
                self.assertLessEqual(_fields(text), _fields(bundled[key]))

    def test_a_failed_reply_points_at_the_app_not_at_commands(self):
        _, (persona,) = load("persona")
        for key, text in persona._COMPANION_ZH.items():
            if key.startswith("gateway.errors."):
                with self.subTest(key=key):
                    self.assertNotRegex(text, r"/retry|/model|/new|/compress|hermes ")


if __name__ == "__main__":
    unittest.main()
