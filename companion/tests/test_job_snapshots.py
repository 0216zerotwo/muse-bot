"""Copies of the scheduled jobs: one when the list changed, the last few kept."""

import tempfile
import unittest
from pathlib import Path

from _loader import load


class JobSnapshotsTest(unittest.TestCase):
    def setUp(self):
        self.folder, (self.snapshots,) = load("job_snapshots")
        self.jobs = Path(tempfile.mkdtemp(prefix="companion-jobs-")) / "jobs.json"
        self.copies = self.folder / "job-snapshots"

    def test_a_copy_only_when_the_jobs_changed_and_the_newest_are_kept(self):
        self.assertIsNone(self.snapshots.snapshot(self.jobs, self.copies))  # no jobs file yet
        self.jobs.write_text('{"jobs": [{"name": "喝水提醒"}]}', encoding="utf-8")
        first = self.snapshots.snapshot(self.jobs, self.copies)
        self.assertEqual(first.read_text(encoding="utf-8"), '{"jobs": [{"name": "喝水提醒"}]}')
        self.assertIsNone(self.snapshots.snapshot(self.jobs, self.copies))  # unchanged
        for n in range(4):
            self.jobs.write_text(f'{{"jobs": [{n}]}}', encoding="utf-8")
            (self.copies / f"jobs-20260101-00000{n}.json").write_text("old", encoding="utf-8")
        newest = self.snapshots.snapshot(self.jobs, self.copies, keep=3)
        kept = sorted(p.name for p in self.copies.glob("jobs-*.json"))
        self.assertEqual(len(kept), 3)
        self.assertEqual(kept[-1], newest.name)
        self.assertEqual(newest.read_text(encoding="utf-8"), '{"jobs": [3]}')


if __name__ == "__main__":
    unittest.main()
