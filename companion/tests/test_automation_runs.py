"""运行记录: a scheduled job's runs from Hermes's reports, and what one run answered."""

import unittest
from pathlib import Path
from unittest import mock

from _loader import load

REPORT = "# Cron Job: 动态更新{failed}\n\n**Job ID:** j1\n\n## Prompt\n\n写动态\n\n{tail}\n"


class AutomationRunsTest(unittest.TestCase):
    def setUp(self):
        import muse.cron.jobs as cron_jobs

        self.folder, (self.automations,) = load("automations")
        self.output = Path(self.folder) / "cron-output"
        runs = self.output / "j1"
        runs.mkdir(parents=True)
        (runs / "2026-11-01_20-18-06.md").write_text(REPORT.format(failed="", tail="## Response\n\n[SILENT]"), encoding="utf-8")
        (runs / "2026-11-02_08-01-30.md").write_text(REPORT.format(failed="", tail="## Response\n\n写了 3 条"), encoding="utf-8")
        (runs / "2026-10-31_19-55-16.md").write_text(
            REPORT.format(failed=" (FAILED)", tail="## Error\n\nRuntimeError: cut off"), encoding="utf-8")
        (runs / "notes.md").write_text("not a run", encoding="utf-8")
        # Only where Muse keeps the reports: a temporary folder, never the real cron.
        self.enterContext(mock.patch.object(cron_jobs, "get_cron_output_dir", lambda: self.output))

    def test_runs_are_listed_newest_first_with_failures_marked(self):
        runs = self.automations._runs("j1")
        self.assertEqual([r["id"] for r in runs], ["2026-11-02_08-01-30", "2026-11-01_20-18-06", "2026-10-31_19-55-16"])
        self.assertEqual([r["status"] for r in runs], ["ok", "ok", "error"])
        self.assertEqual(self.automations._runs("none"), [])

    def test_a_run_answers_with_its_response_or_error(self):
        self.assertEqual(self.automations.run_report("j1", "2026-11-02_08-01-30")["text"], "写了 3 条")
        silent = self.automations.run_report("j1", "2026-11-01_20-18-06")
        self.assertTrue(silent["silent"])
        self.assertEqual(silent["text"], "")
        failed = self.automations.run_report("j1", "2026-10-31_19-55-16")
        self.assertEqual(failed["status"], "error")
        self.assertIn("cut off", failed["text"])

    def test_only_a_run_of_that_job_can_be_read(self):
        for job, run in (("j1", "../j1/notes"), ("j1", "notes"), ("other", "2026-11-02_08-01-30")):
            with self.assertRaises(ValueError):
                self.automations.run_report(job, run)


if __name__ == "__main__":
    unittest.main()
