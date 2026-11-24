"""The phone's own notifications for reminders: what to say, and when it runs over the next two days."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from _loader import load

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 2, 20, 30, tzinfo=TZ)


def iso(dt):
    return dt.isoformat()


class ReminderTimesTest(unittest.TestCase):
    def setUp(self):
        import muse.cron.jobs as cron_jobs

        _, (self.automations,) = load("automations")
        # Every two hours from the last run: a stand-in for schedule rules (never the real cron).
        self.enterContext(
            mock.patch.object(
                cron_jobs,
                "compute_next_run",
                lambda schedule, last_run_at=None: iso(datetime.fromisoformat(last_run_at) + timedelta(hours=2)),
            )
        )

    def job(self, **extra):
        return {"id": "w", "name": "喝水提醒（带当日次数）", "enabled": True, "deliver": "companion:main",
                "schedule": {"kind": "cron", "expr": "0 9-21/2 * * *"}, "next_run_at": iso(NOW + timedelta(minutes=30)), **extra}

    def test_a_reminder_runs_every_time_over_the_next_two_days(self):
        times = self.automations.upcoming(self.job(), now=NOW.timestamp())
        self.assertEqual(times[0], int((NOW + timedelta(minutes=30)).timestamp() * 1000))
        self.assertEqual(len(times), 24)  # at most so many
        self.assertTrue(all(b - a == 2 * 3600_000 for a, b in zip(times, times[1:])))
        once = self.job(schedule={"kind": "once", "run_at": iso(NOW + timedelta(hours=1))}, next_run_at=iso(NOW + timedelta(hours=1)))
        self.assertEqual(len(self.automations.upcoming(once, now=NOW.timestamp())), 1)
        self.assertEqual(self.automations.upcoming(self.job(enabled=False), now=NOW.timestamp()), [])
        later = self.job(next_run_at=iso(NOW + timedelta(days=3)))
        self.assertEqual(self.automations.upcoming(later, now=NOW.timestamp()), [])

    def test_what_the_phone_says(self):
        self.assertEqual(self.automations.notice(self.job()), "喝水提醒")
        self.assertEqual(self.automations.notice(self.job(name="每日早报")), "每日早报")
        self.assertIsNone(self.automations.notice(self.job(name="动态更新")))  # its posts are not a reminder
        self.assertIsNone(self.automations.notice(self.job(deliver="local")))  # not said in a chat


if __name__ == "__main__":
    unittest.main()
