from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase, TestCase

from panel import health
from panel.dockerops import Backup, BackupListing
from panel.models import Job
from panel.templatetags import panel_tags
from panel.tests.fakes import make_instance

H = timedelta(hours=1)
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def listing(age):
    return BackupListing([Backup("acme_staging_20261029_020000.zip", 1, NOW - age)], NOW)


class CronTests(SimpleTestCase):
    def test_fields(self):
        self.assertEqual(health.parse_field("*/15", 0, 59), {0, 15, 30, 45})
        self.assertEqual(health.parse_field("1-5", 0, 7), {1, 2, 3, 4, 5})
        self.assertEqual(health.parse_field("0,12", 0, 23), {0, 12})
        self.assertEqual(health.parse_field("2-10/4", 0, 23), {2, 6, 10})
        self.assertEqual(health.parse_field("5/20", 0, 59), {5, 25, 45})

    def test_what_cannot_be_understood_is_none(self):
        for text in ("60", "*/0", "a", "5-2", "", "1,,2", "-1", "1-"):
            with self.subTest(text):
                self.assertIsNone(health.parse_field(text, 0, 59))
        for expression in ("", "0 2 * *", "0 2 * * * *", "@daily", "0 25 * * *", "x 2 * * *"):
            with self.subTest(expression):
                self.assertIsNone(health.parse_cron(expression))

    def test_the_interval_is_the_longest_gap(self):
        cases = {
            "0 2 * * *": 24 * H,
            "0 */6 * * *": 6 * H,
            "*/15 * * * *": timedelta(minutes=15),
            "0 2 * * 1-5": 72 * H,  # Friday night to Monday: quiet, not missed
            "0 2 * * 0": 7 * 24 * H,
            "0 2 * * 7": 7 * 24 * H,  # 7 is Sunday too
            "0 2,14 * * *": 12 * H,
            "0 8,10 * * *": 22 * H,  # two runs close together, then a long night
            "30 3 1 * *": 31 * 24 * H,
        }
        for expression, expected in cases.items():
            with self.subTest(expression):
                self.assertEqual(health.expected_interval(expression), expected)

    def test_an_unreadable_or_empty_schedule_assumes_a_day(self):
        for expression in ("", "garbage", "0 2 31 2 *"):
            with self.subTest(expression):
                self.assertEqual(health.expected_interval(expression), health.DEFAULT_INTERVAL)

    def test_day_of_month_or_weekday_when_both_are_restricted(self):
        # cron runs when EITHER matches: the 1st and every Monday, so the gap is at most a week
        self.assertLessEqual(health.expected_interval("0 2 1 * 1"), 7 * 24 * H)


class FormatTests(SimpleTestCase):
    def test_age(self):
        cases = {
            0: "just now",
            59: "just now",
            60: "1 minutes ago",
            12 * 60: "12 minutes ago",
            3600: "1 hours ago",
            6 * 3600: "6 hours ago",
            47 * 3600: "47 hours ago",
            48 * 3600: "2 days ago",
            -5: "just now",
        }
        for seconds, text in cases.items():
            with self.subTest(seconds):
                self.assertEqual(health.format_age(timedelta(seconds=seconds)), text)

    def test_interval(self):
        cases = {
            timedelta(minutes=15): "15 minutes",
            24 * H: "24 hours",
            6 * H: "6 hours",
            14 * H: "14 hours",
            72 * H: "3 days",
            timedelta(hours=1.5): "1.5 hours",
        }
        for delta, text in cases.items():
            with self.subTest(text):
                self.assertEqual(health.format_interval(delta), text)

    def test_duration(self):
        cases = {5: "5 s", 60: "1 min", 192: "3 min 12 s", 3600 + 120: "1 h 2 min", -3: "0 s"}
        for seconds, text in cases.items():
            with self.subTest(seconds):
                self.assertEqual(health.format_duration(timedelta(seconds=seconds)), text)


class AssessTests(SimpleTestCase):
    def assess(self, age=None, listing_=None, error="", instance=None):
        instance = instance or make_instance()
        if listing_ is None and age is not None:
            listing_ = listing(age)
        return health.assess(instance, listing_, error, now=NOW)

    def test_a_recent_backup_is_fresh(self):
        result = self.assess(6 * H)
        self.assertEqual((result.level, result.label, result.css), ("fresh", "Fresh", "good"))

    def test_stale_starts_after_one_and_a_half_intervals(self):
        self.assertEqual(self.assess(36 * H).level, "fresh")  # exactly 1.5 x 24 h
        stale = self.assess(36 * H + timedelta(minutes=1))
        self.assertEqual((stale.level, stale.label, stale.css), ("stale", "Stale", "warn"))
        self.assertIn("1.5× the expected interval", stale.reason)
        self.assertIn("36 hours", stale.reason)

    def test_the_schedule_decides_how_old_is_too_old(self):
        weekly = make_instance(CRON_SCHEDULE="0 2 * * 0")
        self.assertEqual(self.assess(5 * 24 * H, instance=weekly).level, "fresh")
        six_hourly = make_instance(CRON_SCHEDULE="0 */6 * * *")
        self.assertEqual(self.assess(10 * H, instance=six_hourly).level, "stale")

    def test_no_backup_file(self):
        result = self.assess(listing_=BackupListing([], NOW))
        self.assertEqual((result.level, result.css), ("empty", "warn"))

    def test_a_stopped_container_wins_over_everything(self):
        stopped = make_instance(status="exited")
        result = self.assess(1 * H, error="x", instance=stopped)
        self.assertEqual((result.level, result.label, result.css), ("stopped", "Container stopped", "bad"))

    def test_an_error_is_unreadable_with_its_text(self):
        result = self.assess(error="timeout")
        self.assertEqual((result.level, result.reason, result.css), ("unreadable", "timeout", "bad"))

    def test_no_listing_yet_is_loading(self):
        result = self.assess()
        self.assertEqual((result.level, result.css), ("loading", "idle"))


class FilterTests(SimpleTestCase):
    def test_filters_tolerate_empty_values(self):
        self.assertEqual(panel_tags.ago(None), "")
        self.assertEqual(panel_tags.duration(None), "")
        self.assertEqual(panel_tags.interval(None), "")
        self.assertEqual(panel_tags.expected(""), "–")
        self.assertEqual(panel_tags.expected("0 2 * * *"), "24 hours")
        self.assertEqual(panel_tags.duration(timedelta(seconds=192)), "3 min 12 s")
        self.assertEqual(panel_tags.ago(datetime.now(UTC) - 6 * H), "6 hours ago")


class JobProgressTests(TestCase):
    def job(self, status, steps):
        return Job(kind=Job.Kind.RESTORE, status=status, steps=[{"name": n, "status": s} for n, s in steps])

    def test_progress_counts_done_steps_fully_and_the_running_one_half(self):
        self.assertEqual(self.job("queued", []).progress, 0)
        self.assertEqual(self.job("running", [("a", "todo"), ("b", "todo")]).progress, 0)
        self.assertEqual(
            self.job("running", [("a", "done"), ("b", "running"), ("c", "todo"), ("d", "todo")]).progress, 38
        )
        self.assertEqual(self.job("running", [("a", "done"), ("b", "done")]).progress, 100)
        self.assertEqual(self.job("done", []).progress, 100)
        self.assertEqual(self.job("failed", [("a", "done"), ("b", "failed")]).progress, 50)

    def test_duration(self):
        queued = Job(status="queued")
        self.assertIsNone(queued.duration)
        start = datetime.now(UTC) - 90 * timedelta(seconds=1)
        self.assertGreaterEqual(Job(started_at=start).duration, timedelta(seconds=90))
        self.assertEqual(
            Job(started_at=start, finished_at=start + timedelta(seconds=30)).duration, timedelta(seconds=30)
        )
