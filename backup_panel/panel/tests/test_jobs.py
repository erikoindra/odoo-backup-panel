from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import docker
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from panel import dockerops, jobs
from panel.dockerops import Endpoint, Server
from panel.models import Job
from panel.tests.fakes import SECRET, FakeApi, FakeClient, make_instance, odoo_inspect

ENDPOINT = Endpoint("vps-1", "docker", "tcp://x:1")
SLUG = "acme-staging-backup"
ZIP = "acme_staging_20261028_020000.zip"
NOTHING = {"PORTAINER_URL": "", "PORTAINER_API_KEY": "", "DOCKER_ENDPOINTS": []}


class Lab:
    """A stand-in for one backup container and its volumes, answering the way the real scripts do.
    (Whether the scripts really behave like this is the job of test_contract.py, against real Docker.)"""

    def __init__(self):
        self.databases = {"postgres", "acme_staging"}
        self.filestores = {"acme_staging"}
        self.zips = {ZIP}
        self.locked = False
        self.dump_fails = False
        self.copy_fails = False
        self.backup_exit = 0
        self.neutralize_exit = 0
        self.neutralized = []
        self.backup_text = b"[10:00:00] finished: 1 database(s) backed up to /backups\n"
        self.exec_calls = []
        self.helper_calls = []

    def runner(self, argv, env):
        self.exec_calls.append((argv, env))
        if argv == ["odoo-backup"]:
            return self.backup_exit, self.backup_text, b""
        if argv[:2] == ["sh", "-c"] and argv[2] == jobs.DATABASE_SCRIPT:
            if self.locked:
                return 4, b"", b""
            if env["ZIP"] not in self.zips:
                return 5, b"", b""
            if env["NEW_DB"] in self.databases:
                return 3, b"", b""
            self.databases.add(env["NEW_DB"])
            if self.dump_fails:
                return 1, b"db-created\n", b"psql: ERROR: syntax error"
            return 0, b"db-created\ndump-loaded\n", b""
        if argv[:2] == ["sh", "-c"] and argv[2] == jobs.NEUTRALIZE_SCRIPT:
            if self.neutralize_exit:
                return self.neutralize_exit, b"", b"odoo: error: boom"
            self.neutralized.append(env["NEW_DB"])
            return 0, b"neutralized\n", b""
        return 0, b"", b""

    def helper(self, call):
        self.helper_calls.append(call)
        env = call["environment"]
        if env["NEW_DB"] in self.filestores:
            return 3, b""
        if env["MODE"] == "copy":
            if env["ZIP"] not in self.zips:
                return 5, b""
            if self.copy_fails:
                return 1, b"unzip: disk full"
            self.filestores.add(env["NEW_DB"])
        return 0, b""


@override_settings(**NOTHING, JOB_POLL_INTERVAL=0)
class JobTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("admin", password="x" * 14)

    def setUp(self):
        self.lab = Lab()
        self.api = FakeApi(runner=self.lab.runner)
        self.client_ = FakeClient(self.api, self.lab.helper)
        self.instance = make_instance(FILESTORE_ROOT="/odoo-data/filestore")
        self.server = Server("vps-1", "docker", [self.instance])
        for target, kwargs in (
            ("find_endpoint", {"return_value": ENDPOINT}),
            ("look_at", {"side_effect": lambda *a, **k: self.server}),
            ("docker_client", {"return_value": self.client_}),
        ):
            patcher = patch.object(dockerops, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def job(self, kind=Job.Kind.BACKUP, **fields):
        return Job.objects.create(
            kind=kind,
            endpoint="vps-1",
            instance_slug=SLUG,
            container_name="acme-1",
            created_by=self.user,
            **fields,
        )

    def restore_job(self, db="acme_copy"):
        return self.job(Job.Kind.RESTORE, filename=ZIP, target_db=db)

    def run_job(self, job):
        claimed = jobs.claim_next()
        self.assertEqual(claimed.pk, job.pk)
        jobs.execute(claimed)
        return Job.objects.get(pk=job.pk)


class QueueTests(JobTestCase):
    def test_the_oldest_waiting_job_is_taken_first_and_only_once(self):
        first, second = self.job(), self.job()
        claimed = jobs.claim_next()
        self.assertEqual((claimed.pk, claimed.status), (first.pk, Job.Status.RUNNING))
        self.assertIsNotNone(claimed.started_at)
        self.assertEqual(jobs.claim_next().pk, second.pk)
        self.assertIsNone(jobs.claim_next())

    def test_downloads_and_finished_jobs_are_never_taken(self):
        self.job(Job.Kind.DOWNLOAD)
        self.job(status=Job.Status.DONE)
        self.job(status=Job.Status.RUNNING)
        self.assertIsNone(jobs.claim_next())

    def test_a_job_taken_by_someone_else_in_between_is_skipped(self):
        job = self.job()
        with patch.object(Job.objects, "filter", wraps=Job.objects.filter):
            Job.objects.filter(pk=job.pk).update(status=Job.Status.RUNNING)  # "another worker" got there first
            self.assertIsNone(jobs.claim_next())


class BackupJobTests(JobTestCase):
    def test_success(self):
        job = self.run_job(self.job())
        self.assertEqual(job.status, Job.Status.DONE)
        self.assertEqual(job.summary, "Backup finished.")
        self.assertIn("finished: 1 database(s)", job.log)
        self.assertEqual([s["status"] for s in job.steps], ["done", "done"])
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(self.lab.exec_calls[0][0], ["odoo-backup"])

    def test_failure_keeps_the_output_and_says_which_code(self):
        self.lab.backup_exit, self.lab.backup_text = 1, b"ERROR: PostgreSQL is not accepting connections"
        job = self.run_job(self.job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("code 1", job.summary)
        self.assertIn("not accepting connections", job.log)
        self.assertEqual([s["status"] for s in job.steps], ["failed", "todo"])

    def test_the_scheduled_backup_holding_the_lock_has_its_own_message(self):
        self.lab.backup_exit, self.lab.backup_text = 1, b"ERROR: another backup is already running"
        job = self.run_job(self.job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("A scheduled backup is running", job.summary)

    def test_the_log_keeps_only_the_last_200_kb(self):
        self.lab.backup_text = b"x" * 300_000 + b"THE END"
        job = self.run_job(self.job())
        self.assertLessEqual(len(job.log), jobs.LOG_LIMIT)
        self.assertTrue(job.log.rstrip().endswith("THE END"))


class WhenTheInstanceCannotBeUsedTests(JobTestCase):
    def test_each_reason_is_a_failed_job_with_a_sentence(self):
        cases = {
            "unknown server": (patch.object(dockerops, "find_endpoint", return_value=None), "no longer known"),
            "unreachable": (
                patch.object(dockerops, "look_at", return_value=Server("vps-1", "docker", [], error="timeout")),
                "cannot be reached: timeout",
            ),
            "stack gone": (
                patch.object(dockerops, "look_at", return_value=Server("vps-1", "docker", [])),
                "was not found",
            ),
            "stopped": (
                patch.object(
                    dockerops, "look_at", return_value=Server("vps-1", "docker", [make_instance(status="exited")])
                ),
                "is exited",
            ),
        }
        for label, (patcher, expected) in cases.items():
            with self.subTest(label), patcher:
                job = self.run_job(self.job())
                self.assertEqual(job.status, Job.Status.FAILED)
                self.assertIn(expected, job.summary)
        self.assertEqual(self.lab.exec_calls, [])  # nothing was run in any of them

    def test_a_docker_error_is_a_failed_job_not_a_crash(self):
        with patch.object(dockerops, "run_detached", side_effect=docker.errors.APIError("daemon marah")):
            job = self.run_job(self.job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("daemon marah", job.summary)

    def test_a_bug_ends_the_job_and_leaves_the_worker_alive(self):
        with (
            patch.object(dockerops, "run_detached", side_effect=RuntimeError("bug")),
            self.assertLogs("panel.jobs", "ERROR"),
        ):
            job = self.run_job(self.job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("Internal error", job.summary)
        self.assertEqual(self.run_job(self.job()).status, Job.Status.DONE)  # the next one still runs


class RestoreJobTests(JobTestCase):
    def test_success_creates_the_database_and_the_filestore(self):
        job = self.run_job(self.restore_job())
        self.assertEqual(job.status, Job.Status.DONE)
        self.assertEqual([s["status"] for s in job.steps], ["done"] * 4)
        self.assertIn("acme_copy", self.lab.databases)
        self.assertIn("acme_copy", self.lab.filestores)
        self.assertIn("?db=acme_copy", job.summary)
        self.assertIn("db-created", job.log)
        self.assertIn("dump-loaded", job.log)

    def test_the_two_helper_runs_check_read_only_then_copy_read_write(self):
        self.run_job(self.restore_job())
        check, copy = self.lab.helper_calls
        self.assertEqual((check["environment"]["MODE"], copy["environment"]["MODE"]), ("check", "copy"))
        for call, mode in ((check, "ro"), (copy, "rw")):
            self.assertEqual(call["volumes"]["acme_backups"], {"bind": "/b", "mode": "ro"})
            self.assertEqual(call["volumes"]["acme_odoo_data"], {"bind": "/d", "mode": mode})
            self.assertEqual((call["network_mode"], call["user"], call["remove"]), ("none", "0", True))
            self.assertIn("/backups", call["tmpfs"])  # keeps the image's VOLUME from leaving an anonymous volume
            self.assertEqual(call["entrypoint"], ["sh", "-c"])
            self.assertEqual(call["command"], [jobs.FILESTORE_SCRIPT])  # one element: a fixed script

    def test_nothing_from_the_form_is_ever_part_of_a_script(self):
        job = self.run_job(self.restore_job("acme_copy"))
        self.assertEqual(job.status, Job.Status.DONE)
        for argv, _ in self.lab.exec_calls:
            self.assertNotIn("acme_copy", " ".join(argv))
        for call in self.lab.helper_calls:
            self.assertNotIn("acme_copy", call["command"][0])
            self.assertEqual(call["environment"]["NEW_DB"], "acme_copy")
            self.assertEqual(call["environment"]["ZIP"], ZIP)

    def test_a_filestore_folder_that_exists_stops_everything_before_the_database_is_touched(self):
        self.lab.filestores.add("acme_copy")
        job = self.run_job(self.restore_job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("The filestore folder for acme_copy already exists", job.summary)
        self.assertNotIn("acme_copy", self.lab.databases)
        self.assertEqual(self.lab.exec_calls, [])

    def test_a_database_that_exists_is_left_alone(self):
        self.lab.databases.add("acme_copy")
        job = self.run_job(self.restore_job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("Database acme_copy already exists. Nothing was changed.", job.summary)
        self.assertEqual(len(self.lab.helper_calls), 1)  # only the read-only check ran
        self.assertNotIn("acme_copy", self.lab.filestores)

    def test_a_running_backup_blocks_the_restore_without_changes(self):
        self.lab.locked = True
        job = self.run_job(self.restore_job())
        self.assertIn("A backup is running", job.summary)
        self.assertNotIn("acme_copy", self.lab.databases)

    def test_a_zip_removed_by_retention_is_reported(self):
        self.lab.zips.clear()
        job = self.run_job(self.restore_job())
        self.assertIn("is no longer in /backups", job.summary)

    def test_a_dump_that_fails_reports_the_half_made_database_and_deletes_nothing(self):
        self.lab.dump_fails = True
        job = self.run_job(self.restore_job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("Database acme_copy was created, but dump.sql failed to load", job.summary)
        self.assertIn("syntax error", job.log)
        self.assertEqual([s["status"] for s in job.steps], ["done", "failed", "todo", "todo"])
        self.assertIn("acme_copy", self.lab.databases)  # left for the admin to look at
        self.assertEqual(len(self.lab.helper_calls), 1)  # the filestore copy never started

    def test_a_filestore_copy_that_fails_reports_what_exists(self):
        self.lab.copy_fails = True
        job = self.run_job(self.restore_job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn(
            "Database acme_copy was created and the dump loaded, but the filestore was not copied", job.summary
        )
        self.assertIn("disk full", job.summary)
        self.assertIn(".restore-acme_copy", job.summary)
        self.assertEqual([s["status"] for s in job.steps], ["done", "done", "failed", "todo"])

    def test_a_filestore_root_outside_odoo_data_is_refused_before_anything_runs(self):
        self.server = Server("vps-1", "docker", [make_instance(FILESTORE_ROOT="/elsewhere/filestore")])
        job = self.run_job(self.restore_job())
        self.assertIn("outside /odoo-data", job.summary)
        self.assertEqual((self.lab.exec_calls, self.lab.helper_calls), ([], []))

    def test_without_the_two_volumes_the_helper_is_refused(self):
        instance = make_instance()
        object.__setattr__(instance, "odoo_data_volume", "")
        self.server = Server("vps-1", "docker", [instance])
        job = self.run_job(self.restore_job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("volume", job.summary)


class NeutralizeJobTests(JobTestCase):
    def setUp(self):
        super().setUp()
        self.api.inspects["id-acme-odoo-1"] = odoo_inspect()

    def neutralizing_job(self, db="acme_copy"):
        return self.job(Job.Kind.RESTORE, filename=ZIP, target_db=db, neutralize=True)

    def test_the_restored_database_is_neutralized_inside_the_odoo_container(self):
        job = self.run_job(self.neutralizing_job())
        self.assertEqual(job.status, Job.Status.DONE)
        self.assertEqual(
            [s["name"] for s in job.steps],
            ["Check filestore folder", "Create database and load dump.sql", "Copy filestore", "Neutralize", "Done"],
        )
        self.assertEqual([s["status"] for s in job.steps], ["done"] * 5)
        self.assertEqual(self.lab.neutralized, ["acme_copy"])
        self.assertIn("and neutralized", job.summary)
        self.assertNotIn("run odoo neutralize first", job.summary)
        neutralize = [e for e in self.api.execs.values() if e["cmd"][-1] == jobs.NEUTRALIZE_SCRIPT]
        self.assertEqual([e["container"] for e in neutralize], ["id-acme-odoo-1"])
        self.assertEqual(neutralize[0]["env"]["NEW_DB"], "acme_copy")

    def test_the_script_gets_the_name_only_as_a_variable_and_never_the_odoo_password(self):
        job = self.run_job(self.neutralizing_job("acme_copy"))
        self.assertNotIn("acme_copy", jobs.NEUTRALIZE_SCRIPT)
        self.assertNotIn(SECRET, job.log + job.summary + str(job.steps))
        for execution in self.api.execs.values():
            self.assertNotIn(SECRET, str(execution))  # the panel never reads or passes the password itself

    def test_without_the_switch_nothing_is_neutralized_and_the_summary_says_to_do_it(self):
        job = self.run_job(self.restore_job())
        self.assertEqual(self.lab.neutralized, [])
        self.assertEqual(len(job.steps), 4)
        self.assertIn("run odoo neutralize first", job.summary)

    def test_without_an_odoo_container_the_job_stops_before_changing_anything(self):
        del self.api.inspects["id-acme-odoo-1"]
        job = self.run_job(self.neutralizing_job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("Neutralize was requested", job.summary)
        self.assertIn("Nothing was changed", job.summary)
        self.assertEqual((self.lab.exec_calls, self.lab.helper_calls), ([], []))
        self.assertNotIn("acme_copy", self.lab.databases)

    def test_a_stopped_odoo_container_also_stops_it_first(self):
        self.api.inspects["id-acme-odoo-1"] = odoo_inspect(status="exited")
        job = self.run_job(self.neutralizing_job())
        self.assertIn("is exited", job.summary)
        self.assertEqual(self.lab.exec_calls, [])

    def test_a_failing_neutralize_says_loudly_that_the_copy_is_not_neutralized(self):
        self.lab.neutralize_exit = 1
        job = self.run_job(self.neutralizing_job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("NOT neutralized", job.summary)
        self.assertIn("odoo neutralize -d acme_copy", job.summary)
        self.assertIn("boom", job.log)
        self.assertEqual([s["status"] for s in job.steps], ["done", "done", "done", "failed", "todo"])
        self.assertIn("acme_copy", self.lab.databases)  # nothing deleted
        self.assertIn("acme_copy", self.lab.filestores)


class WorkerStartTests(JobTestCase):
    def test_a_backup_or_restore_cut_off_by_a_restart_is_failed(self):
        cut_off = [self.job(status=Job.Status.RUNNING), self.restore_job()]
        cut_off[1].status = Job.Status.RUNNING
        cut_off[1].save()
        waiting = self.job()
        self.assertEqual(jobs.recover_orphans(), 2)
        for job in cut_off:
            job.refresh_from_db()
            self.assertEqual(job.status, Job.Status.FAILED)
            self.assertIn("restarted", job.summary)
            self.assertIsNotNone(job.finished_at)
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, Job.Status.QUEUED)

    def test_a_live_download_is_left_alone_and_a_long_dead_one_is_closed(self):
        live = self.job(Job.Kind.DOWNLOAD, status=Job.Status.RUNNING, started_at=timezone.now())
        dead = self.job(Job.Kind.DOWNLOAD, status=Job.Status.RUNNING, started_at=timezone.now() - timedelta(hours=7))
        self.assertEqual(jobs.recover_orphans(), 1)
        live.refresh_from_db()
        dead.refresh_from_db()
        self.assertEqual((live.status, dead.status), (Job.Status.RUNNING, Job.Status.FAILED))

    def test_the_temp_sweep_visits_running_containers_and_never_raises(self):
        self.server = Server("vps-1", "docker", [make_instance(), make_instance("stopped", status="exited")])
        with patch.object(dockerops, "discover_all", return_value=[self.server]):
            self.assertEqual(jobs.sweep_temp_dirs(), 1)
            with patch.object(dockerops, "sweep_temp", side_effect=docker.errors.APIError("x")):
                self.assertEqual(jobs.sweep_temp_dirs(), 0)
        with patch.object(dockerops, "discover_all", side_effect=docker.errors.DockerException("down")):
            self.assertEqual(jobs.sweep_temp_dirs(), 0)

    def test_the_command_runs_what_is_waiting_and_exits_with_once(self):
        job = self.job()
        out = StringIO()
        call_command("run_jobs", "--once", stdout=out)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.DONE)
        self.assertIn(f"job #{job.pk}", out.getvalue())
        self.assertIn("Worker stopped", out.getvalue())

    def test_the_command_reports_the_jobs_it_failed_at_start(self):
        orphan = self.job(status=Job.Status.RUNNING)
        out = StringIO()
        call_command("run_jobs", "--once", stdout=out)
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, Job.Status.FAILED)
        self.assertIn("1 interrupted job", out.getvalue())
