"""资源库's grid shows small copies of pictures, made once, and the original when no copy can be made."""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from _loader import load


class ThumbnailTest(unittest.TestCase):
    def setUp(self):
        self.folder, (self.creations,) = load("creations")
        self.source = Path(tempfile.mkdtemp(prefix="companion-thumb-")) / "大图.png"
        Image.new("RGB", (1600, 1200), (200, 120, 40)).save(self.source)

    def test_a_picture_becomes_a_small_webp_made_once(self):
        thumb = self.creations.thumbnail(self.source)
        self.assertEqual(thumb.suffix, ".webp")
        self.assertEqual(max(Image.open(thumb).size), self.creations.THUMB_SIDE)
        self.assertTrue(thumb.is_relative_to(self.folder))
        self.assertEqual(self.creations.thumbnail(self.source), thumb)

    def test_a_file_that_is_no_picture_stays_itself(self):
        broken = self.source.with_name("坏的.png")
        broken.write_bytes(b"not a picture")
        self.assertEqual(self.creations.thumbnail(broken), broken)


if __name__ == "__main__":
    unittest.main()
