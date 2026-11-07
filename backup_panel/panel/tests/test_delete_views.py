from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from panel import dockerops, jobs
from panel.dockerops import Backup, BackupListing, Server
from panel.models import Job
from panel.tests.fakes import make_instance
from panel.tests.test_job_views import ENDPOINT, SLUG, JobViewCase

NEW = "acme_staging_20261028_072000.zip"
MID = "acme_staging_20261027_072000.zip"
OLD = "acme_staging_20261026_072000.zip"
OTHER_NEW = "acme_wiki_20261028_080000.zip"
OTHER_OLD = "acme_wiki_20261027_080000.zip"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def backup(name, days_ago, size=6_740_230):
    return Backup(name, size, NOW - timedelta(days=days_ago))


def listing(*backups):
    return BackupListing(sorted(backups, key=lambda b: b.mtime, reverse=True), NOW)


class DeleteViewCase(JobViewCase):
    """JobViewCase with a superuser as the signed-in person, and three zips of one database."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.user = get_user_model().objects.create_superuser("root", password="x" * 14)
        cls.staff = get_user_model().objects.create_user("staff", password="x" * 14, is_staff=True)

    def setUp(self):
        super().setUp()
        self.listing = listing(backup(NEW, 0), backup(MID, 1), backup(OLD, 2))

    @property
    def url(self):
        return reverse("delete_backups", args=["vps-1", SLUG])

    def detail_url(self):
        return reverse("instance_detail", args=["vps-1", SLUG])


class ConfirmPageTests(DeleteViewCase):
    def test_it_lists_the_files_and_asks_for_the_word_DELETE(self):
        response = self.client.get(self.url, {"filename": [OLD, MID]})
        self.assertEqual(response.status_code, 200)
        for text in (
            MID,
            OLD,
            "This cannot be undone",
            "2 backup files",
            "12.9",
            "Type DELETE to confirm",
            "Delete 2 files",
        ):
            self.assertContains(response, text)
        self.assertContains(response, 'name="confirm"')
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, '<input type="hidden" name="filename"', count=2)
        self.assertContains(response, f'href="{self.detail_url()}"')  # Cancel
        self.assertNotContains(response, NEW)

    def test_the_files_are_shown_newest_first_whatever_the_order_asked(self):
        html = self.client.get(self.url, {"filename": [OLD, MID]}).content.decode()
        self.assertLess(html.index(MID), html.index(OLD))

    def test_a_name_asked_for_twice_counts_once(self):
        response = self.client.get(self.url, {"filename": [OLD, OLD]})
        self.assertContains(response, "1 backup file ")
        self.assertContains(response, '<input type="hidden" name="filename"', count=1)

    def test_nothing_selected_is_refused(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Select at least one backup file.", status_code=400)
        self.assertNotContains(response, 'name="confirm"', status_code=400)

    def test_the_newest_backup_of_a_database_is_refused(self):
        response = self.client.get(self.url, {"filename": [NEW, OLD]})
        self.assertContains(
            response, f"{NEW} is the newest backup of its database and cannot be deleted.", status_code=400
        )
        self.assertNotContains(response, 'name="confirm"', status_code=400)

    def test_each_database_keeps_its_own_newest(self):
        self.listing = listing(backup(NEW, 0), backup(OLD, 2), backup(OTHER_NEW, 0.5), backup(OTHER_OLD, 1.5))
        self.assertEqual(self.client.get(self.url, {"filename": [OLD, OTHER_OLD]}).status_code, 200)
        self.assertEqual(self.client.get(self.url, {"filename": [OLD, OTHER_NEW]}).status_code, 400)

    def test_names_that_are_not_in_the_list_or_not_zip_names_are_refused(self):
        for name in ("nope_20200101_000000.zip", "../etc/passwd", "a b.zip", "x.txt", "/backups/" + MID, ".hidden.zip"):
            with self.subTest(name):
                response = self.client.get(self.url, {"filename": [name]})
                self.assertContains(response, "is not in the backup list.", status_code=400)

    def test_a_name_is_escaped_in_the_message(self):
        response = self.client.get(self.url, {"filename": ["<script>alert(1)</script>.zip"]})
        self.assertNotContains(response, "<script>alert", status_code=400)
        self.assertContains(response, "&lt;script&gt;", status_code=400)

    def test_too_many_files_at_once_are_refused(self):
        many = [backup(f"acme_staging_{20250101 + i}_072000.zip", 100 + i) for i in range(jobs.MAX_DELETE_FILES + 1)]
        self.listing = listing(backup(NEW, 0), *many)
        names = [b.name for b in many]
        response = self.client.get(self.url, {"filename": names})
        self.assertContains(response, f"Select at most {jobs.MAX_DELETE_FILES} files at a time.", status_code=400)
        self.assertEqual(self.client.get(self.url, {"filename": names[: jobs.MAX_DELETE_FILES]}).status_code, 200)

    def test_it_warns_and_disables_the_button_while_a_job_is_running(self):
        active = self.make_job(status=Job.Status.RUNNING)
        response = self.client.get(self.url, {"filename": [OLD]})
        self.assertContains(response, "A job is still running")
        self.assertContains(response, reverse("job_status", args=[active.pk]))
        self.assertContains(response, '<button class="btn btn--danger" type="submit" disabled>')

    def test_a_stopped_container_an_unreachable_server_and_a_docker_error_are_error_pages(self):
        self.server = Server("vps-1", "portainer", [make_instance(status="exited")])
        self.assertContains(self.client.get(self.url, {"filename": [OLD]}), "Container stopped", status_code=409)
        self.server = Server("vps-1", "portainer", [], error="timeout")
        self.assertContains(self.client.get(self.url, {"filename": [OLD]}), "timeout", status_code=502)
        self.server = Server("vps-1", "portainer", [make_instance()])
        with patch.object(dockerops, "list_backups_cached", side_effect=TimeoutError("slow")):
            response = self.client.get(self.url, {"filename": [OLD]})
        self.assertContains(response, "Delete could not start", status_code=502)


class ConfirmedDeleteTests(DeleteViewCase):
    def post(self, names=(OLD, MID), confirm="DELETE"):
        return self.client.post(self.url, {"filename": list(names), "confirm": confirm})

    def test_a_confirmed_delete_queues_a_job_and_returns_to_the_instance(self):
        response = self.post()
        self.assertRedirects(response, self.detail_url())
        job = Job.objects.get()
        self.assertEqual((job.kind, job.status, job.created_by), (Job.Kind.DELETE, Job.Status.QUEUED, self.user))
        self.assertEqual(job.files, [MID, OLD])  # the order of the listing, newest first
        self.assertEqual((job.endpoint, job.instance_slug), ("vps-1", SLUG))
        self.assertEqual(job.title, "Delete 2 backup files")

    def test_the_word_must_be_DELETE_in_capitals(self):
        for word in ("delete", "Delete", "", "DELET", "DELETE ME"):
            with self.subTest(word):
                response = self.post(confirm=word)
                self.assertContains(response, "Type DELETE in capital letters to continue.", status_code=400)
                self.assertContains(response, 'aria-invalid="true"', status_code=400)
        self.assertEqual(Job.objects.count(), 0)
        self.assertEqual(self.post(confirm="  DELETE  ").status_code, 302)  # spaces around it do not matter

    def test_the_newest_backup_is_refused_even_with_the_right_word(self):
        response = self.post(names=(NEW, OLD))
        self.assertContains(response, "is the newest backup of its database", status_code=400)
        self.assertEqual(Job.objects.count(), 0)

    def test_a_file_that_is_not_listed_is_refused_even_with_the_right_word(self):
        self.assertEqual(self.post(names=("../x.zip",)).status_code, 400)
        self.assertEqual(self.post(names=(OLD, "gone_20200101_000000.zip")).status_code, 400)
        self.assertEqual(self.post(names=()).status_code, 400)
        self.assertEqual(Job.objects.count(), 0)

    def test_only_one_backup_restore_or_delete_per_stack_at_a_time(self):
        for kind in (Job.Kind.BACKUP, Job.Kind.RESTORE, Job.Kind.DELETE):
            with self.subTest(kind):
                Job.objects.all().delete()
                self.make_job(kind, Job.Status.RUNNING, files=[OLD] if kind == Job.Kind.DELETE else [])
                self.assertContains(self.post(), "A job is still running", status_code=409)
                self.assertEqual(Job.objects.count(), 1)

    def test_a_delete_in_progress_blocks_a_backup_now(self):
        self.make_job(Job.Kind.DELETE, Job.Status.QUEUED, files=[OLD])
        self.assertEqual(self.client.post(self.backup_url()).status_code, 409)

    def test_the_list_is_read_fresh_not_from_the_cache(self):
        with patch.object(dockerops, "list_backups_cached", return_value=self.listing) as read:
            self.post()
        read.assert_called_once_with(ENDPOINT, make_instance(ODOO_BACKUP_VERSION="v0.2.0"), use_cache=False)


class PermissionTests(DeleteViewCase):
    def staff_client(self):
        client = Client()
        client.force_login(self.staff)
        return client

    def test_anonymous_people_go_to_the_sign_in_page_and_nothing_is_read(self):
        with patch.object(dockerops, "list_backups_cached") as read:
            for response in (Client().get(self.url), Client().post(self.url, {"filename": [OLD], "confirm": "DELETE"})):
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response["Location"])
        read.assert_not_called()
        self.assertEqual(Job.objects.count(), 0)

    def test_someone_who_is_not_a_superuser_gets_a_403_and_nothing_is_read_or_queued(self):
        with patch.object(dockerops, "list_backups_cached") as read:
            client = self.staff_client()
            got = client.get(self.url, {"filename": [OLD]})
            posted = client.post(self.url, {"filename": [OLD], "confirm": "DELETE"})
        for response in (got, posted):
            self.assertContains(response, "Only superusers can delete backup files.", status_code=403)
        read.assert_not_called()
        self.assertEqual(Job.objects.count(), 0)

    def test_a_csrf_token_is_required_to_queue_a_delete(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(self.url, {"filename": [OLD], "confirm": "DELETE"}).status_code, 403)
        self.assertEqual(Job.objects.count(), 0)

    def test_other_methods_are_not_allowed(self):
        for method in ("put", "patch", "delete"):
            with self.subTest(method):
                self.assertEqual(getattr(self.client, method)(self.url).status_code, 405)

    def test_an_unknown_server_or_stack_is_a_404(self):
        self.assertEqual(self.client.get(reverse("delete_backups", args=["nope", SLUG])).status_code, 404)
        self.assertEqual(self.client.get(reverse("delete_backups", args=["vps-1", "nope"])).status_code, 404)


class TableControlsTests(DeleteViewCase):
    def table(self, client=None):
        return (client or self.client).get(reverse("backups_partial", args=["vps-1", SLUG]))

    def test_a_superuser_can_select_every_file_except_the_newest(self):
        response = self.table()
        self.assertContains(response, "data-select-all")
        self.assertContains(response, 'id="delete-form"')
        self.assertContains(response, f'action="/e/vps-1/{SLUG}/delete/"')
        self.assertContains(response, "data-delete-submit")
        self.assertContains(response, "Delete selected")
        self.assertContains(response, 'form="delete-form" data-delete-box', count=2)  # the older two
        self.assertContains(response, f'value="{OLD}" form="delete-form"')
        self.assertNotContains(response, f'value="{NEW}"')
        self.assertContains(response, 'disabled title="The newest backup of its database is kept"', count=1)

    def test_every_database_has_its_own_disabled_newest(self):
        self.listing = listing(backup(NEW, 0), backup(OLD, 2), backup(OTHER_NEW, 0.5), backup(OTHER_OLD, 1.5))
        response = self.table()
        self.assertContains(response, "The newest backup of its database is kept", count=2)
        self.assertContains(response, "data-delete-box", count=2)

    def test_someone_who_is_not_a_superuser_sees_no_delete_controls_at_all(self):
        client = Client()
        client.force_login(self.staff)
        response = self.table(client)
        self.assertContains(response, NEW)  # the list itself is the same
        for text in ("data-select-all", "delete-form", "Delete selected", "data-delete-box", "/delete/"):
            self.assertNotContains(response, text)

    def test_with_no_backups_there_is_nothing_to_select(self):
        self.listing = BackupListing([], NOW)
        response = self.table()
        self.assertNotContains(response, "delete-form")
        self.assertNotContains(response, "data-select-all")


class DeleteJobPageTests(DeleteViewCase):
    def test_the_job_page_and_the_activity_list_name_the_files(self):
        job = self.make_job(
            Job.Kind.DELETE, Job.Status.DONE, files=[OLD, MID], summary="Deleted 2 backup files (12.9 MB)."
        )
        page = self.client.get(reverse("job_status", args=[job.pk]))
        for text in ("Delete 2 backup files", "Files <b>2</b>", "Deleted 2 backup files (12.9 MB)."):
            self.assertContains(page, text)
        activity = self.client.get(reverse("job_list"))
        self.assertContains(activity, "Delete 2 backup files")

    def test_when_a_delete_ends_the_page_is_told_to_reload_the_table(self):
        job = self.make_job(Job.Kind.DELETE, Job.Status.DONE, files=[OLD])
        with patch.object(dockerops, "forget_listing") as forget:
            response = self.client.get(reverse("job_status", args=[job.pk]), headers={"HX-Request": "true"})
        self.assertEqual(response["HX-Trigger"], "backups-changed")
        forget.assert_called_once_with("vps-1", SLUG)

    def test_the_detail_page_follows_a_running_delete(self):
        job = self.make_job(Job.Kind.DELETE, Job.Status.RUNNING, files=[OLD], started_at=datetime.now(UTC))
        response = self.client.get(reverse("instance_detail", args=["vps-1", SLUG]))
        self.assertContains(response, 'id="job-panel"')
        self.assertContains(response, f'hx-get="/jobs/{job.pk}/"')
        self.assertContains(response, "disabled aria-describedby")  # "Back up now" waits
