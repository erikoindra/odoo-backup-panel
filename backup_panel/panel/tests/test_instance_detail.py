from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import docker
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from panel import dockerops
from panel.dockerops import Backup, BackupListing, ContainerNotRunning, DockerOpsError, Endpoint, Server
from panel.tests.fakes import make_instance

ENDPOINT = Endpoint("vps-1", "portainer", "https://portainer.example.com:443/api/endpoints/4/docker", "k")
SLUG = "acme-staging-backup"
# 00:20 UTC is 07:20 in Jakarta, which is what the page must show.
LISTING = BackupListing(
    [
        Backup("acme_staging_20261028_072000.zip", 6740230, datetime(2026, 10, 28, 0, 20, tzinfo=UTC)),
        Backup("acme_staging_20261027_020000.zip", 6600000, datetime(2026, 10, 26, 19, 0, tzinfo=UTC)),
    ],
    datetime(2026, 10, 28, 1, 2, 3, tzinfo=UTC),
)
NOTHING = {"PORTAINER_URL": "", "PORTAINER_API_KEY": "", "DOCKER_ENDPOINTS": []}


def urls(slug=SLUG):
    return {
        "detail": reverse("instance_detail", args=["vps-1", slug]),
        "backups": reverse("backups_partial", args=["vps-1", slug]),
        "summary": reverse("summary_partial", args=["vps-1", slug]),
    }


@override_settings(**NOTHING)
class DetailBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("admin", password="x" * 14)

    def get(self, url, server=None, listing=LISTING, listing_error=None, logged_in=True):
        """GET with Docker replaced. Returns (response, the mock standing in for list_backups_cached)."""
        if logged_in:
            self.client.force_login(self.user)
        server = server or Server("vps-1", "portainer", [make_instance()])
        cached = MagicMock(return_value=listing, side_effect=listing_error)
        with (
            patch.object(dockerops, "find_endpoint", side_effect=lambda n: ENDPOINT if n == "vps-1" else None),
            patch.object(dockerops, "look_at", return_value=server),
            patch.object(dockerops, "list_backups_cached", cached),
        ):
            return self.client.get(url), cached


class AccessTests(DetailBase):
    def test_every_page_needs_login_and_nothing_touches_docker_before_that(self):
        for name, url in urls().items():
            with self.subTest(name):
                response, cached = self.get(url, logged_in=False)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response["Location"])
                cached.assert_not_called()

    def test_an_unknown_server_or_stack_is_a_404_everywhere(self):
        for name in ("detail", "backups", "summary"):
            with self.subTest(name):
                response, _ = self.get(urls()[name].replace("vps-1", "nope"))
                self.assertEqual(response.status_code, 404)
                response, _ = self.get(urls("no-such-stack")[name])
                self.assertEqual(response.status_code, 404)


class DetailPageTests(DetailBase):
    def test_shows_status_and_chips_but_reads_no_backups_itself(self):
        response, cached = self.get(urls()["detail"])
        self.assertContains(response, "<h1>acme-staging-backup</h1>", html=True)
        self.assertContains(response, "<dd>Running</dd>", html=True)
        self.assertContains(response, "acme-staging-backup-odoo-backup-1")
        self.assertContains(response, "<dd>0 2 * * *</dd>", html=True)
        self.assertContains(response, "<dd>Asia/Jakarta</dd>", html=True)
        self.assertContains(response, "<dd>3 days</dd>", html=True)
        self.assertContains(response, "<dd>24 hours</dd>", html=True)  # the longest gap of a daily schedule
        self.assertContains(response, "PostgreSQL <b>17</b>")
        self.assertContains(response, "acme_staging")
        self.assertContains(response, 'href="/instances/"')  # breadcrumb back to the list
        cached.assert_not_called()  # the table arrives later, as a fragment

    def test_the_table_is_loaded_on_demand(self):
        response, _ = self.get(urls()["detail"])
        self.assertContains(response, f'hx-get="{urls()["backups"]}"')
        self.assertContains(response, 'hx-trigger="load, backups-changed from:body"')
        self.assertContains(response, "Loading the backup list")

    def test_a_stopped_container_explains_itself_and_loads_nothing(self):
        server = Server("vps-1", "portainer", [make_instance(status="exited")])
        response, cached = self.get(urls()["detail"], server=server)
        self.assertContains(response, "Backup container stopped")
        self.assertContains(response, "Start the stack again in Portainer")
        self.assertNotContains(response, "hx-get=")
        cached.assert_not_called()

    def test_an_unreachable_server_shows_the_error_and_retry(self):
        server = Server("vps-1", "portainer", [], error="timeout after 20 seconds")
        response, _ = self.get(urls()["detail"], server=server)
        self.assertContains(response, "timeout after 20 seconds")
        self.assertContains(response, "Retry")
        self.assertNotContains(response, "hx-get=")

    def test_has_the_backup_button_and_the_side_panels(self):
        response, _ = self.get(urls()["detail"])
        self.assertContains(response, f'action="/e/vps-1/{SLUG}/backup"')
        self.assertContains(response, "Back up now")
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, 'id="side-status"')
        self.assertContains(response, "Before you restore")
        self.assertContains(response, "No backup or restore has been run from the panel")
        self.assertNotContains(response, "<dialog")  # restore is a wizard page now

    def test_a_stopped_container_has_a_disabled_button(self):
        server = Server("vps-1", "portainer", [make_instance(status="exited")])
        response, _ = self.get(urls()["detail"], server=server)
        self.assertContains(response, "disabled aria-describedby")
        self.assertContains(response, "Container stopped.")
        self.assertContains(response, "Container stopped</span>")  # the status in the side panel


class BackupTableTests(DetailBase):
    def test_lists_files_with_size_and_local_time(self):
        response, cached = self.get(urls()["backups"])
        self.assertContains(response, "acme_staging_20261028_072000.zip")
        self.assertContains(response, "acme_staging_20261027_020000.zip")
        self.assertContains(response, "28 Oct 2026, 07:20")
        self.assertContains(response, "27 Oct 2026, 02:00")
        self.assertRegex(response.content.decode(), r"6\.4\sMB")
        self.assertContains(response, "read at 08:02:03")
        self.assertContains(response, "2 files · 12.")  # count and total size in the footer
        self.assertContains(response, "retention 3 days")
        self.assertContains(response, "there is no resume")
        self.assertContains(
            response, 'href="/e/vps-1/acme-staging-backup/backups/acme_staging_20261028_072000.zip/download"'
        )
        self.assertContains(response, "data-download=", count=2)  # one per file
        self.assertContains(
            response, 'href="/e/vps-1/acme-staging-backup/restore/?file=acme_staging_20261028_072000.zip"'
        )
        self.assertNotContains(
            response, 'hx-get="/e/vps-1/acme-staging-backup/backups/acme_staging_20261028_072000.zip'
        )
        self.assertNotContains(response, "<html")  # a fragment, not a page
        cached.assert_called_once_with(ENDPOINT, make_instance(), use_cache=True)

    def test_the_response_carries_the_side_status_for_an_out_of_band_swap(self):
        response, _ = self.get(urls()["backups"])
        self.assertContains(response, 'id="side-status"')
        self.assertContains(response, 'hx-swap-oob="true"')

    def test_the_status_follows_the_age_of_the_newest_backup(self):
        now = datetime.now(UTC)
        for hours, label in ((6, "Fresh"), (60, "Stale")):
            with self.subTest(label):
                listing = BackupListing(
                    [Backup("acme_staging_20261028_072000.zip", 1, now - timedelta(hours=hours))], now
                )
                response, _ = self.get(urls()["backups"], listing=listing)
                self.assertContains(response, f"{label}</span>")
        response, _ = self.get(urls()["backups"], listing=BackupListing([], now))
        self.assertContains(response, "No backups yet</span>")

    def test_the_reload_button_reads_again_instead_of_using_the_cache(self):
        response, _ = self.get(urls()["backups"])
        self.assertContains(response, f'hx-get="{urls()["backups"]}?refresh=1"')
        _, cached = self.get(urls()["backups"] + "?refresh=1")
        cached.assert_called_once_with(ENDPOINT, make_instance(), use_cache=False)

    def test_no_files_is_an_empty_state(self):
        response, _ = self.get(urls()["backups"], listing=BackupListing([], LISTING.read_at))
        self.assertContains(response, "No backups yet")
        self.assertContains(response, "0 2 * * * · Asia/Jakarta")
        self.assertNotContains(response, "<table")
        self.assertNotContains(response, "there is no resume")

    def test_a_failed_read_is_a_message_with_a_retry_not_a_crash(self):
        for error in (DockerOpsError("stat failed"), docker.errors.APIError("daemon unhappy"), TimeoutError("slow")):
            with self.subTest(type(error).__name__):
                response, _ = self.get(urls()["backups"], listing_error=error)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "The backup list cannot be read")
                self.assertContains(response, "refresh=1")

    def test_error_text_cannot_inject_markup(self):
        # describe() already drops tags (a proxy's HTML error page), and the template escapes the rest.
        response, _ = self.get(urls()["backups"], listing_error=DockerOpsError("<img src=x onerror=alert(1)>"))
        self.assertNotContains(response, "<img")
        response, _ = self.get(urls()["backups"], listing_error=DockerOpsError('a "b" & c'))
        self.assertContains(response, "a &quot;b&quot; &amp; c")

    def test_a_stopped_container_is_not_read(self):
        server = Server("vps-1", "portainer", [make_instance(status="exited")])
        response, cached = self.get(urls()["backups"], server=server)
        self.assertContains(response, "Backup container stopped")
        cached.assert_not_called()

    def test_a_container_that_stops_between_two_reads_is_reported(self):
        response, _ = self.get(urls()["backups"], listing_error=ContainerNotRunning("Container x is exited."))
        self.assertContains(response, "is exited")

    def test_an_unreachable_server_is_reported_in_the_fragment(self):
        server = Server("vps-1", "portainer", [], error="Portainer rejected the API key (401).")
        response, cached = self.get(urls()["backups"], server=server)
        self.assertContains(response, "401")
        cached.assert_not_called()


class SummaryTests(DetailBase):
    """The summary is one finished row of the instance list; it replaces the loading row."""

    def fresh(self, hours=6):
        now = datetime.now(UTC)
        return BackupListing(
            [Backup("acme_staging_20261028_072000.zip", 6740230, now - timedelta(hours=hours)), LISTING.backups[1]], now
        )

    def test_latest_backup_age_size_and_count(self):
        response, _ = self.get(urls()["summary"], listing=self.fresh())
        self.assertContains(response, 'data-level="fresh"')
        self.assertContains(response, "6 hours ago")
        self.assertContains(response, "2 file")
        self.assertRegex(response.content.decode(), r"12\.\d\sMB")  # total of both files
        self.assertContains(response, "Fresh")
        self.assertContains(response, f'href="{urls()["detail"]}"')
        self.assertNotContains(response, "hx-get=")  # a finished row does not load again
        self.assertTrue(response.content.decode().lstrip().startswith("<tr"))

    def test_a_late_backup_is_stale_with_the_reason(self):
        response, _ = self.get(urls()["summary"], listing=self.fresh(hours=60))
        self.assertContains(response, 'data-level="stale"')
        self.assertContains(response, "Stale")
        self.assertContains(response, "expected interval")
        self.assertContains(response, "2 days ago")

    def test_empty_stopped_and_failed(self):
        response, _ = self.get(urls()["summary"], listing=BackupListing([], LISTING.read_at))
        self.assertContains(response, 'data-level="empty"')
        self.assertContains(response, "No backups yet")
        response, _ = self.get(urls()["summary"], listing_error=DockerOpsError("x" * 300))
        self.assertContains(response, 'data-level="unreadable"')
        self.assertContains(response, "Unreadable")
        self.assertNotContains(response, "x" * 200)  # long errors are shortened in a table cell
        stopped = Server("vps-1", "portainer", [make_instance(status="exited")])
        response, cached = self.get(urls()["summary"], server=stopped)
        self.assertContains(response, 'data-level="stopped"')
        self.assertContains(response, "Container stopped")
        cached.assert_not_called()

    def test_an_unreachable_server_is_still_a_row(self):
        server = Server("vps-1", "portainer", [], error="Portainer rejected the API key (401).")
        response, _ = self.get(urls()["summary"], server=server)
        self.assertContains(response, 'data-level="unreadable"')
        self.assertContains(response, "401")
