from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import docker
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from panel import dockerops
from panel.dockerops import Backup, BackupListing, Endpoint, Server
from panel.models import Job
from panel.tests.fakes import FakeApi, FakeClient, make_instance

ENDPOINT = Endpoint("vps-1", "portainer", "https://portainer.example.com:443/api/endpoints/4/docker", "k")
SLUG = "acme-staging-backup"
NAME = "acme_staging_20261028_072000.zip"
DATA = bytes(range(256)) * 800  # 204 800 bytes
LISTING = BackupListing([Backup(NAME, len(DATA), datetime(2026, 10, 28, 0, 20, tzinfo=UTC))], datetime.now(UTC))
NOTHING = {"PORTAINER_URL": "", "PORTAINER_API_KEY": "", "DOCKER_ENDPOINTS": []}


def url(name=NAME, slug=SLUG, endpoint="vps-1"):
    return reverse("download", args=[endpoint, slug, name])


@override_settings(**NOTHING)
class DownloadTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("admin", password="x" * 14)

    def setUp(self):
        self.api = FakeApi()
        self.api.files[f"/backups/{NAME}"] = DATA
        self.listings = [LISTING]  # what each successive read of /backups returns

    def get(self, name=NAME, server=None, logged_in=True, method="get", listing_error=None, raw_url=None):
        if logged_in:
            self.client.force_login(self.user)
        server = server or Server("vps-1", "portainer", [make_instance()])
        listing = MagicMock(
            side_effect=listing_error
            or (lambda *a, **k: self.listings.pop(0) if len(self.listings) > 1 else self.listings[0])
        )
        with (
            patch.object(dockerops, "find_endpoint", side_effect=lambda n: ENDPOINT if n == "vps-1" else None),
            patch.object(dockerops, "look_at", return_value=server),
            patch.object(dockerops, "list_backups_cached", listing),
            patch.object(dockerops, "docker_client", return_value=FakeClient(self.api)),
        ):
            response = getattr(self.client, method)(raw_url or url(name))
            if getattr(response, "streaming", False):
                response.body = b"".join(response.streaming_content)  # read it while the patches are active
        self.read_listing = listing
        return response


class SuccessTests(DownloadTestCase):
    def test_streams_the_file_with_the_right_headers(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, DATA)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response["Content-Length"], str(len(DATA)))
        self.assertEqual(response["Content-Disposition"], f'attachment; filename="{NAME}"')
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Accel-Buffering"], "no")

    def test_it_is_audited_with_who_what_and_how_it_ended(self):
        self.get()
        (job,) = Job.objects.all()
        self.assertEqual((job.kind, job.status), (Job.Kind.DOWNLOAD, Job.Status.DONE))
        self.assertEqual((job.endpoint, job.instance_slug, job.filename), ("vps-1", SLUG, NAME))
        self.assertEqual(job.container_name, f"{SLUG}-odoo-backup-1")
        self.assertEqual(job.created_by, self.user)
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)
        self.assertIn(str(len(DATA)), job.log)

    def test_the_name_must_be_in_the_listing_the_table_was_built_from(self):
        self.api.files["/backups/not_listed.zip"] = b"secret"
        response = self.get("not_listed.zip")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "is not in the backup list", status_code=404)
        self.assertEqual(self.api.streams, [])  # never asked Docker for it
        self.assertEqual(Job.objects.count(), 0)
        self.assertEqual(
            [c.kwargs["use_cache"] for c in self.read_listing.call_args_list], [True, False]
        )  # re-read once

    def test_a_backup_made_a_moment_ago_is_found_by_the_second_read(self):
        self.listings = [BackupListing([], datetime.now(UTC)), LISTING]
        self.assertEqual(self.get().status_code, 200)


class RefusalTests(DownloadTestCase):
    def test_login_is_required_and_docker_is_not_touched(self):
        response = self.get(logged_in=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
        self.assertEqual(self.api.streams, [])
        self.assertEqual(Job.objects.count(), 0)

    def test_only_get_is_allowed(self):
        for method in ("post", "put", "delete"):
            with self.subTest(method):
                self.assertEqual(self.get(method=method).status_code, 405)
        self.assertEqual(self.api.streams, [])

    def test_names_that_are_not_plain_zip_names_are_404_without_touching_docker(self):
        base = f"/e/vps-1/{SLUG}/backups/"
        bad = [
            base + quote("../x.zip", safe="") + "/download",  # %2F-encoded traversal
            base + quote("..%2f..%2fetc%2fpasswd.zip", safe="") + "/download",
            base + "x.txt/download",
            base + quote("a b.zip", safe="") + "/download",
            base + "../x.zip/download",  # a real slash: no route at all
        ]
        for path in bad:
            with self.subTest(path):
                self.assertEqual(self.get(raw_url=path).status_code, 404)
        self.assertEqual(self.api.streams, [])
        self.assertEqual(Job.objects.count(), 0)

    def test_unknown_server_or_stack_is_404(self):
        self.assertEqual(self.get(raw_url=url(endpoint="nope")).status_code, 404)
        self.assertEqual(self.get(raw_url=url(slug="nope")).status_code, 404)

    def test_the_file_vanishing_between_listing_and_download_is_a_404_page(self):
        del self.api.files[f"/backups/{NAME}"]
        response = self.get()
        self.assertContains(response, "no longer in /backups", status_code=404)
        self.assertContains(response, reverse("instance_detail", args=["vps-1", SLUG]), status_code=404)  # way back
        self.assertEqual(Job.objects.count(), 0)

    def test_a_stopped_container_is_a_409_page(self):
        server = Server("vps-1", "portainer", [make_instance(status="exited")])
        response = self.get(server=server, listing_error=dockerops.ContainerNotRunning("Container x is exited."))
        self.assertContains(response, "Container stopped", status_code=409)
        self.assertEqual(Job.objects.count(), 0)

    def test_an_unreachable_server_is_a_502_page(self):
        server = Server("vps-1", "portainer", [], error="timeout after 20 seconds")
        response = self.get(server=server)
        self.assertContains(response, "timeout after 20 seconds", status_code=502)
        self.assertEqual(self.api.streams, [])

    def test_a_docker_failure_before_the_first_byte_is_a_502_page_not_a_crash(self):
        for error in (
            docker.errors.APIError("daemon marah"),
            TimeoutError("slow"),
            dockerops.DockerOpsError("<b>x</b>"),
        ):
            with self.subTest(type(error).__name__):
                response = self.get(listing_error=error)
                self.assertContains(response, "Download could not start", status_code=502)
                self.assertNotContains(response, "<b>x</b>", status_code=502)
        self.assertEqual(Job.objects.count(), 0)

    def test_a_missing_size_from_docker_is_refused_up_front(self):
        self.api.stat_size = False
        response = self.get()
        self.assertContains(response, "file size", status_code=502)
        self.assertEqual(Job.objects.count(), 0)
        self.assertTrue(self.api.streams[0].closed)


class InterruptedDownloadTests(DownloadTestCase):
    def open(self):
        self.client.force_login(self.user)
        server = Server("vps-1", "portainer", [make_instance()])
        patches = [
            patch.object(dockerops, "find_endpoint", return_value=ENDPOINT),
            patch.object(dockerops, "look_at", return_value=server),
            patch.object(dockerops, "list_backups_cached", return_value=LISTING),
            patch.object(dockerops, "docker_client", return_value=FakeClient(self.api)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.client.get(url())

    def test_a_browser_that_goes_away_is_recorded_and_the_stream_is_released(self):
        with patch.object(dockerops, "DOWNLOAD_CHUNK", 1000):
            response = self.open()
            pieces = iter(response.streaming_content)
            next(pieces)
            response.close()
        (job,) = Job.objects.all()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("Stopped by the client", job.log)
        self.assertTrue(self.api.streams[0].closed)

    def test_a_connection_that_drops_midway_fails_the_job_and_is_not_swallowed(self):
        self.api.fail_after = 90_000
        response = self.open()
        with self.assertRaises(ConnectionError):
            b"".join(response.streaming_content)
        (job,) = Job.objects.all()
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("koneksi terputus", job.log)
        self.assertTrue(self.api.streams[0].closed)
