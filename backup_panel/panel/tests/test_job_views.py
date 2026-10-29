from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from panel import checks, dockerops
from panel.dockerops import Backup, BackupListing, Endpoint, Server
from panel.forms import RestoreForm
from panel.models import Job
from panel.tests.fakes import make_instance

ENDPOINT = Endpoint("vps-1", "portainer", "https://portainer.example.com:443/api/endpoints/4/docker", "k")
SLUG = "acme-staging-backup"
ZIP = "acme_staging_20261028_072000.zip"
LISTING = BackupListing([Backup(ZIP, 6740230, datetime(2026, 10, 28, 0, 20, tzinfo=UTC))], datetime.now(UTC))
NOTHING = {"PORTAINER_URL": "", "PORTAINER_API_KEY": "", "DOCKER_ENDPOINTS": []}


@override_settings(**NOTHING)
class JobViewCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("admin", password="x" * 14)

    def setUp(self):
        self.server = Server("vps-1", "portainer", [make_instance()])
        self.listing = LISTING
        for target, kwargs in (
            ("find_endpoint", {"side_effect": lambda n: ENDPOINT if n == "vps-1" else None}),
            ("look_at", {"side_effect": lambda *a, **k: self.server}),
            ("list_backups_cached", {"side_effect": lambda *a, **k: self.listing}),
        ):
            patcher = patch.object(dockerops, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_login(self.user)

    def make_job(self, kind=Job.Kind.BACKUP, status=Job.Status.QUEUED, **fields):
        return Job.objects.create(
            kind=kind,
            status=status,
            endpoint="vps-1",
            instance_slug=SLUG,
            container_name="acme-1",
            created_by=self.user,
            **fields,
        )

    def backup_url(self, slug=SLUG):
        return reverse("backup_now", args=["vps-1", slug])

    def restore_url(self):
        return reverse("restore", args=["vps-1", SLUG])


class BackupNowTests(JobViewCase):
    def test_a_click_queues_a_job_and_goes_back_to_the_instance_where_its_panel_follows_it(self):
        response = self.client.post(self.backup_url())
        job = Job.objects.get()
        self.assertRedirects(response, reverse("instance_detail", args=["vps-1", SLUG]))
        self.assertEqual((job.kind, job.status), (Job.Kind.BACKUP, Job.Status.QUEUED))
        self.assertEqual((job.endpoint, job.instance_slug, job.created_by), ("vps-1", SLUG, self.user))
        self.assertEqual(job.container_name, f"{SLUG}-odoo-backup-1")

    def test_only_post_and_login_and_a_csrf_token(self):
        self.assertEqual(self.client.get(self.backup_url()).status_code, 405)
        anonymous = Client()
        self.assertEqual(anonymous.post(self.backup_url()).status_code, 302)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(self.backup_url()).status_code, 403)
        self.assertEqual(Job.objects.count(), 0)

    def test_a_second_click_while_one_is_waiting_or_running_is_refused(self):
        for status in (Job.Status.QUEUED, Job.Status.RUNNING):
            with self.subTest(status):
                Job.objects.all().delete()
                active = self.make_job(status=status)
                response = self.client.post(self.backup_url())
                self.assertContains(response, "A job is still running", status_code=409)
                self.assertContains(response, reverse("job_status", args=[active.pk]), status_code=409)
                self.assertEqual(Job.objects.count(), 1)

    def test_a_restore_blocks_a_backup_too_but_a_finished_job_or_another_stack_does_not(self):
        self.make_job(Job.Kind.RESTORE, Job.Status.RUNNING, target_db="x", filename=ZIP)
        self.assertEqual(self.client.post(self.backup_url()).status_code, 409)
        Job.objects.all().delete()
        self.make_job(status=Job.Status.DONE)
        self.make_job(status=Job.Status.FAILED)
        Job.objects.create(
            kind=Job.Kind.BACKUP, endpoint="vps-1", instance_slug="other", container_name="o", created_by=self.user
        )
        self.assertEqual(self.client.post(self.backup_url()).status_code, 302)

    def test_a_stopped_container_or_unreachable_server_creates_nothing(self):
        self.server = Server("vps-1", "portainer", [make_instance(status="exited")])
        self.assertContains(self.client.post(self.backup_url()), "Container stopped", status_code=409)
        self.server = Server("vps-1", "portainer", [], error="timeout")
        self.assertContains(self.client.post(self.backup_url()), "timeout", status_code=502)
        self.assertEqual(Job.objects.count(), 0)

    def test_unknown_server_or_stack_is_404(self):
        self.assertEqual(self.client.post(self.backup_url("nope")).status_code, 404)
        self.assertEqual(self.client.post(reverse("backup_now", args=["nope", SLUG])).status_code, 404)


class RestoreFormTests(TestCase):
    def errors(self, new_db, confirm, filename=ZIP):
        form = RestoreForm({"filename": filename, "new_db": new_db, "confirm": confirm})
        return form.is_valid(), form.errors

    def test_valid(self):
        self.assertTrue(self.errors("acme_restore_20261028", "acme_restore_20261028")[0])
        self.assertTrue(self.errors("a", "a")[0])
        self.assertTrue(self.errors("x" * 63, "x" * 63)[0])

    def test_invalid_names(self):
        for name in ("", "Acme", "acme-copy", "a b", "a;b", "a/../b", "x" * 64, "übung", "a.b", "'; drop"):
            with self.subTest(name):
                valid, errors = self.errors(name, name)
                self.assertFalse(valid)
                self.assertIn("new_db", errors)

    def test_names_postgres_owns_are_refused(self):
        for name in ("postgres", "template0", "template1"):
            self.assertIn("new_db", self.errors(name, name)[1])

    def test_the_confirmation_must_match_exactly(self):
        valid, errors = self.errors("acme_copy", "acme_copy2")
        self.assertFalse(valid)
        self.assertIn("confirm", errors)
        self.assertFalse(self.errors("acme_copy", "")[0])
        self.assertTrue(self.errors(" acme_copy ", "acme_copy")[0])  # spaces are trimmed

    def test_the_file_must_look_like_a_backup_zip(self):
        for name in ("", "x.txt", "../a.zip", "a b.zip", "a/b.zip"):
            with self.subTest(name):
                self.assertIn("filename", self.errors("acme_copy", "acme_copy", filename=name)[1])

    def test_neutralize_is_optional_and_defaults_to_off(self):
        form = RestoreForm({"filename": ZIP, "new_db": "a", "confirm": "a"})
        self.assertTrue(form.is_valid())
        self.assertFalse(form.cleaned_data["neutralize"])
        form = RestoreForm({"filename": ZIP, "new_db": "a", "confirm": "a", "neutralize": "on"})
        self.assertTrue(form.is_valid())
        self.assertTrue(form.cleaned_data["neutralize"])


class RestoreViewTests(JobViewCase):
    def post(self, new_db="acme_copy", confirm=None, name=ZIP, **extra):
        data = {"filename": name, "new_db": new_db, "confirm": new_db if confirm is None else confirm, **extra}
        return self.client.post(self.restore_url(), data)

    def test_the_wizard_page_is_prefilled_with_the_latest_backup(self):
        response = self.client.get(self.restore_url())
        self.assertContains(response, "acme_staging_restore_20261028")
        self.assertContains(response, ZIP)
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertContains(response, f'value="{ZIP}" data-suggest="acme_staging_restore_20261028" checked')
        self.assertContains(response, 'data-start="1"')

    def test_a_file_in_the_url_is_preselected_and_an_unknown_one_falls_back_to_the_latest(self):
        older = Backup("acme_staging_20261027_072000.zip", 100, datetime(2026, 10, 27, 0, 20, tzinfo=UTC))
        self.listing = BackupListing([LISTING.backups[0], older], datetime.now(UTC))
        response = self.client.get(self.restore_url(), {"file": older.name})
        self.assertContains(response, f'value="{older.name}" data-suggest="acme_staging_restore_20261027" checked')
        response = self.client.get(self.restore_url(), {"file": "nope.zip"})
        self.assertContains(response, f'value="{ZIP}" data-suggest="acme_staging_restore_20261028" checked')
        response = self.client.get(self.restore_url(), {"file": "<script>"})
        self.assertNotContains(response, "<script>")

    def test_a_valid_post_queues_a_restore_with_the_name_and_the_file(self):
        response = self.post()
        job = Job.objects.get()
        self.assertRedirects(response, reverse("job_status", args=[job.pk]))
        self.assertEqual(
            (job.kind, job.status, job.filename, job.target_db, job.neutralize),
            (Job.Kind.RESTORE, "queued", ZIP, "acme_copy", False),
        )
        self.assertEqual(job.created_by, self.user)

    def test_the_neutralize_switch_is_stored_on_the_job(self):
        self.post(neutralize="on")
        self.assertTrue(Job.objects.get().neutralize)

    def test_bad_input_re_renders_the_wizard_at_the_step_with_the_error_and_creates_nothing(self):
        response = self.post("Acme-Copy")
        self.assertContains(response, "Only lowercase letters, digits and underscores", status_code=400)
        self.assertContains(response, 'aria-invalid="true"', status_code=400)
        self.assertContains(response, 'data-start="2"', status_code=400)
        response = self.post("acme_copy", confirm="acme_other")
        self.assertContains(response, "Type the database name exactly", status_code=400)
        self.assertContains(response, 'data-start="3"', status_code=400)
        self.assertEqual(Job.objects.count(), 0)

    def test_files_that_are_not_in_the_listing_or_not_zip_names_are_refused(self):
        response = self.post(name="other.zip")
        self.assertContains(response, "is not in the backup list", status_code=400)
        self.assertContains(response, 'data-start="1"', status_code=400)
        self.assertContains(self.post(name="x.txt"), "Choose one of the backup files", status_code=400)
        self.assertContains(self.post(name="../etc.zip"), "Choose one of the backup files", status_code=400)
        self.assertEqual(Job.objects.count(), 0)

    def test_login_and_csrf_are_required(self):
        data = {"filename": ZIP, "new_db": "a", "confirm": "a"}
        self.assertEqual(Client().post(self.restore_url(), data).status_code, 302)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(self.restore_url(), data).status_code, 403)
        self.assertEqual(Job.objects.count(), 0)

    def test_only_one_backup_or_restore_per_stack_at_a_time(self):
        self.make_job(status=Job.Status.RUNNING)
        self.assertContains(self.post(), "A job is still running", status_code=409)
        self.assertEqual(Job.objects.count(), 1)

    def test_the_wizard_warns_when_a_job_is_running_and_disables_the_restore_button(self):
        active = self.make_job(status=Job.Status.RUNNING)
        response = self.client.get(self.restore_url())
        self.assertContains(response, reverse("job_status", args=[active.pk]))
        self.assertContains(response, "A job is still running")
        self.assertContains(response, '<button class="btn btn--danger" type="submit" disabled>')

    def test_a_filestore_root_outside_odoo_data_is_refused_up_front(self):
        self.server = Server("vps-1", "portainer", [make_instance(FILESTORE_ROOT="/srv/filestore")])
        self.assertContains(self.client.get(self.restore_url()), "outside /odoo-data", status_code=409)
        self.assertContains(self.post(), "outside /odoo-data", status_code=409)

    def test_a_stopped_container_is_refused(self):
        self.server = Server("vps-1", "portainer", [make_instance(status="exited")])
        self.assertContains(self.client.get(self.restore_url()), "Container stopped", status_code=409)

    def test_a_docker_error_while_reading_the_listing_is_a_502_page(self):
        with patch.object(dockerops, "list_backups_cached", side_effect=TimeoutError("slow")):
            self.assertContains(self.client.get(self.restore_url()), "Restore could not start", status_code=502)

    def test_without_any_backup_there_is_nothing_to_restore(self):
        self.listing = BackupListing([], datetime.now(UTC))
        self.assertContains(self.client.get(self.restore_url()), "No backups yet", status_code=404)


class RestoreCheckTests(JobViewCase):
    def url(self):
        return reverse("restore_check", args=["vps-1", SLUG])

    def test_it_shows_every_check_and_says_whether_all_passed(self):
        passed = [
            checks.Check(True, "Database acme_copy does not exist yet"),
            checks.Check(True, "Filestore folder ok"),
        ]
        with patch.object(checks, "run_checks", return_value=passed) as run:
            response = self.client.post(self.url(), {"new_db": "acme_copy", "neutralize": "on"})
        self.assertContains(response, 'data-all-ok="true"')
        self.assertContains(response, "Database acme_copy does not exist yet")
        self.assertEqual(run.call_args.args[2:], ("acme_copy", True))
        failed = [
            checks.Check(True, "Folder ok"),
            checks.Check(False, "Database acme_copy does not exist yet", "Already exists."),
        ]
        with patch.object(checks, "run_checks", return_value=failed):
            response = self.client.post(self.url(), {"new_db": "acme_copy"})
        self.assertContains(response, 'data-all-ok="false"')
        self.assertContains(response, "Already exists.")
        self.assertContains(response, "(failed)")

    def test_an_invalid_name_is_reported_without_touching_docker(self):
        for name in ("", "Acme", "a b", "postgres", "x" * 64):
            with self.subTest(name), patch.object(checks, "run_checks") as run:
                response = self.client.post(self.url(), {"new_db": name})
                self.assertContains(response, 'data-all-ok="false"')
                self.assertContains(response, "Database name valid")
                run.assert_not_called()

    def test_a_stopped_container_or_unreachable_server_is_a_failed_check(self):
        self.server = Server("vps-1", "portainer", [make_instance(status="exited")])
        self.assertContains(self.client.post(self.url(), {"new_db": "a"}), "Backup container running")
        self.server = Server("vps-1", "portainer", [], error="timeout")
        self.assertContains(self.client.post(self.url(), {"new_db": "a"}), "timeout")

    def test_a_docker_error_is_a_failed_check_not_a_500(self):
        with patch.object(checks, "run_checks", side_effect=TimeoutError("slow")):
            response = self.client.post(self.url(), {"new_db": "acme_copy"})
        self.assertContains(response, 'data-all-ok="false"')

    def test_it_is_post_only_needs_login_and_a_csrf_token(self):
        self.assertEqual(self.client.get(self.url()).status_code, 405)
        self.assertEqual(Client().post(self.url(), {"new_db": "a"}).status_code, 302)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(self.url(), {"new_db": "a"}).status_code, 403)


class SuggestedNameTests(TestCase):
    def test_names(self):
        suggest = dockerops.suggest_db_name
        self.assertEqual(suggest("acme_staging_20261028_072000.zip"), "acme_staging_restore_20261028")
        self.assertEqual(suggest("demo-shop19_20261029_020000.zip"), "demo_shop19_restore_20261029")
        self.assertEqual(suggest("Prod.DB_20261029_020000.zip"), "prod_db_restore_20261029")
        long = suggest("a" * 80 + "_20261029_020000.zip")
        self.assertEqual(len(long), 63)
        self.assertTrue(long.endswith("_restore_20261029"))
        self.assertRegex(suggest("odd.zip"), r"^odd_restore_\d{8}$")  # no date in the name: today's
        self.assertTrue(RestoreForm({"filename": ZIP, "new_db": long, "confirm": long}).is_valid())


class JobPageTests(JobViewCase):
    def test_a_running_job_page_polls_itself_and_shows_steps_and_log(self):
        job = self.make_job(
            Job.Kind.RESTORE, Job.Status.RUNNING, filename=ZIP, target_db="acme_copy", log="10:00:00 mulai\n"
        )
        job.steps = [
            {"name": "Check filestore folder", "status": "done", "at": ""},
            {"name": "Create database", "status": "running", "at": ""},
            {"name": "Done", "status": "todo", "at": ""},
        ]
        job.save()
        response = self.client.get(reverse("job_status", args=[job.pk]))
        self.assertContains(response, "Restore to acme_copy")
        self.assertContains(response, f'hx-get="/jobs/{job.pk}/?page=1"')
        self.assertContains(response, 'hx-trigger="every 2s"')
        self.assertContains(response, "Running")
        self.assertContains(response, 'data-s="done"')
        self.assertContains(response, 'data-s="active"')
        self.assertContains(response, 'data-s="todo"')
        self.assertContains(response, 'aria-valuenow="50"')
        self.assertContains(response, "10:00:00 mulai")
        self.assertContains(response, ZIP)
        self.assertContains(response, "<html")

    def test_polling_stops_when_the_job_is_over(self):
        for status in (Job.Status.DONE, Job.Status.FAILED):
            with self.subTest(status):
                job = self.make_job(status=status, summary="done or failed")
                response = self.client.get(reverse("job_status", args=[job.pk]))
                self.assertNotContains(response, "hx-trigger")
                self.assertContains(response, "done or failed")

    def test_a_queued_job_says_it_is_waiting(self):
        job = self.make_job()
        response = self.client.get(reverse("job_status", args=[job.pk]))
        self.assertContains(response, "Queued")
        self.assertContains(response, "Waiting for the worker")

    def test_a_failed_job_shows_its_reason_as_an_error(self):
        job = self.make_job(status=Job.Status.FAILED, summary="Database x already exists.")
        response = self.client.get(reverse("job_status", args=[job.pk]))
        self.assertContains(response, "alert--bad")
        self.assertContains(response, "Job failed")
        self.assertContains(response, "Database x already exists.")

    def test_the_job_page_has_no_close_button_but_the_panel_on_the_detail_page_does(self):
        job = self.make_job(status=Job.Status.DONE, summary="ok", finished_at=datetime.now(UTC))
        page = self.client.get(reverse("job_status", args=[job.pk]))
        self.assertNotContains(page, "data-dismiss")
        self.assertNotContains(page, f"Job details #{job.pk}")
        detail = self.client.get(reverse("instance_detail", args=["vps-1", SLUG]))
        self.assertContains(detail, "data-dismiss")
        self.assertContains(detail, f"Job details #{job.pk}")

    def test_the_poll_from_the_job_page_keeps_it_standalone(self):
        job = self.make_job(status=Job.Status.RUNNING)
        page = self.client.get(reverse("job_status", args=[job.pk]))
        self.assertContains(page, f'hx-get="/jobs/{job.pk}/?page=1"')
        fragment = self.client.get(reverse("job_status", args=[job.pk]) + "?page=1", headers={"HX-Request": "true"})
        self.assertContains(fragment, f'hx-get="/jobs/{job.pk}/?page=1"')
        self.assertNotContains(fragment, f"Job details #{job.pk}")
        detail_fragment = self.client.get(reverse("job_status", args=[job.pk]), headers={"HX-Request": "true"})
        self.assertContains(detail_fragment, f'hx-get="/jobs/{job.pk}/"')
        self.assertContains(detail_fragment, f"Job details #{job.pk}")

    def test_the_fragment_has_no_page_around_it(self):
        job = self.make_job(status=Job.Status.RUNNING)
        response = self.client.get(reverse("job_status", args=[job.pk]), headers={"HX-Request": "true"})
        self.assertNotContains(response, "<html")
        self.assertContains(response, 'id="job-panel"')

    def test_when_a_job_ends_the_fragment_tells_the_page_to_reload_its_table_and_drops_the_cache(self):
        job = self.make_job(status=Job.Status.DONE)
        with patch.object(dockerops, "forget_listing") as forget:
            response = self.client.get(reverse("job_status", args=[job.pk]), headers={"HX-Request": "true"})
        self.assertEqual(response["HX-Trigger"], "backups-changed")
        forget.assert_called_once_with("vps-1", SLUG)

    def test_a_running_job_does_not_trigger_a_reload(self):
        job = self.make_job(status=Job.Status.RUNNING)
        with patch.object(dockerops, "forget_listing") as forget:
            response = self.client.get(reverse("job_status", args=[job.pk]), headers={"HX-Request": "true"})
        self.assertNotIn("HX-Trigger", response)
        forget.assert_not_called()
        self.assertContains(response, f'hx-get="/jobs/{job.pk}/"')

    def test_a_download_row_never_triggers_a_reload(self):
        job = self.make_job(Job.Kind.DOWNLOAD, Job.Status.DONE, filename=ZIP)
        response = self.client.get(reverse("job_status", args=[job.pk]), headers={"HX-Request": "true"})
        self.assertNotIn("HX-Trigger", response)

    def test_unknown_job_is_404_and_login_is_required(self):
        self.assertEqual(self.client.get(reverse("job_status", args=[999])).status_code, 404)
        job = self.make_job()
        for name in ("job_status",):
            self.assertEqual(Client().get(reverse(name, args=[job.pk])).status_code, 302)
        self.assertEqual(Client().get(reverse("job_list")).status_code, 302)

    def test_the_detail_page_shows_the_active_job_and_disables_the_button(self):
        job = self.make_job(status=Job.Status.RUNNING, started_at=datetime.now(UTC))
        response = self.client.get(reverse("instance_detail", args=["vps-1", SLUG]))
        self.assertContains(response, 'id="job-panel"')
        self.assertContains(response, f'hx-get="/jobs/{job.pk}/"')
        self.assertContains(response, "disabled aria-describedby")
        self.assertContains(response, "Waiting for the running job")

    def test_the_detail_page_without_a_job_has_no_banner_and_an_enabled_button(self):
        response = self.client.get(reverse("instance_detail", args=["vps-1", SLUG]))
        self.assertNotContains(response, 'id="job-panel"')
        self.assertNotContains(response, "disabled")


class JobListTests(JobViewCase):
    def test_lists_everything_with_who_and_result(self):
        self.make_job(status=Job.Status.DONE)
        self.make_job(Job.Kind.RESTORE, Job.Status.FAILED, target_db="acme_copy", filename=ZIP)
        self.make_job(Job.Kind.DOWNLOAD, Job.Status.DONE, filename=ZIP)
        response = self.client.get(reverse("job_list"))
        for text in ("Back up now", "Restore to acme_copy", f"Download {ZIP}", "admin", "Done", "Failed", SLUG):
            self.assertContains(response, text)

    def test_empty(self):
        self.assertContains(self.client.get(reverse("job_list")), "No activity yet")


class HtmxSessionTests(TestCase):
    def test_a_fragment_request_without_a_session_redirects_the_whole_window(self):
        response = Client().get("/instances/", headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["HX-Redirect"], "/login/?next=/instances/")

    def test_a_normal_request_still_gets_the_normal_redirect(self):
        response = Client().get("/instances/")
        self.assertEqual((response.status_code, response["Location"]), (302, "/login/?next=/instances/"))
        self.assertNotIn("HX-Redirect", response)

    def test_other_redirects_for_htmx_are_left_alone(self):
        user = get_user_model().objects.create_user("admin", password="x" * 14)
        client = Client()
        client.force_login(user)
        response = client.get("/", headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 302)
