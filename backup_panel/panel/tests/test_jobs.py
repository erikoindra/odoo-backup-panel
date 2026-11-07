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
        self.stats = {}  # /backups listing: name -> (size, mtime as epoch seconds)
        self.delete_exit = 0
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
        if argv[:2] == ["sh", "-c"] and argv[2] == dockerops.LIST_BACKUPS_SCRIPT:
            lines = [f"/backups/{name}|{size}|{mtime}" for name, (size, mtime) in self.stats.items()]
            return 0, ("\n".join(lines) + "\n").encode(), b""
        if argv[:2] == ["sh", "-c"] and argv[2] == jobs.DELETE_SCRIPT:
            if self.locked:
                return 4, b"", b""
            names = env["FILES"].split()
            gone = [n for n in names if n not in self.stats]
            if gone:
                return 5, b"", f"missing: {gone[0]}\n".encode()
            if self.delete_exit:
                return self.delete_exit, f"deleted {names[0]}\n".encode(), b"rm: boom"
            for name in names:
                del self.stats[name]
            return 0, "".join(f"deleted {n}\n" for n in names).encode(), b""
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
        env = self.lab.exec_calls[0][1]
        self.assertNotIn("BACKUP_FILESTORE", env)  # a plain backup runs with the container's own settings
        self.assertNotIn("BACKUP_PREFIX", env)

    def test_database_only_runs_the_same_command_with_two_variables_changed(self):
        self.server = Server("vps-1", "docker", [make_instance(ODOO_BACKUP_VERSION="v0.2.0")])
        job = self.run_job(self.job(database_only=True))
        self.assertEqual((job.status, job.summary), (Job.Status.DONE, "Database-only backup finished."))
        self.assertEqual(job.steps[0]["name"], "Run odoo-backup (database only)")
        argv, env = self.lab.exec_calls[0]
        self.assertEqual(argv, ["odoo-backup"])
        self.assertEqual((env["BACKUP_FILESTORE"], env["BACKUP_PREFIX"]), ("false", "dbonly-"))

    def test_database_only_keeps_the_stacks_own_prefix(self):
        self.server = Server("vps-1", "docker", [make_instance(ODOO_BACKUP_VERSION="v0.2.3", BACKUP_PREFIX="prod_")])
        self.run_job(self.job(database_only=True))
        self.assertEqual(self.lab.exec_calls[0][1]["BACKUP_PREFIX"], "prod_dbonly-")

    def test_database_only_stops_when_the_service_is_too_old_by_the_time_it_runs(self):
        self.server = Server("vps-1", "docker", [make_instance(ODOO_BACKUP_VERSION="v0.1.0")])
        job = self.run_job(self.job(database_only=True))
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("0.2 or newer", job.summary)
        self.assertEqual([s["status"] for s in job.steps], ["failed", "todo"])
        self.assertEqual(self.lab.exec_calls, [])  # nothing was started in the container

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


SHOP_NEW = "shop_20261029_020000.zip"
SHOP_MID = "shop_20261028_020000.zip"
SHOP_OLD = "shop_20261027_020000.zip"
WIKI_NEW = "wiki_20261029_030000.zip"
WIKI_OLD = "wiki_20261028_030000.zip"


class DeleteJobTests(JobTestCase):
    def setUp(self):
        super().setUp()
        self.lab.stats = {
            SHOP_NEW: (3_000_000, 1_793_000_000),
            SHOP_MID: (2_000_000, 1_792_900_000),
            SHOP_OLD: (1_000_000, 1_792_800_000),
            WIKI_NEW: (500_000, 1_793_000_100),
            WIKI_OLD: (400_000, 1_792_900_100),
        }

    def delete_job(self, *names):
        return self.job(Job.Kind.DELETE, files=list(names))

    def delete_execs(self):
        return [e for e in self.api.execs.values() if e["cmd"][-1] == jobs.DELETE_SCRIPT]

    def test_the_chosen_files_are_removed_and_nothing_else(self):
        job = self.run_job(self.delete_job(SHOP_OLD, SHOP_MID))
        self.assertEqual(job.status, Job.Status.DONE, job.summary)
        self.assertEqual(set(self.lab.stats), {SHOP_NEW, WIKI_NEW, WIKI_OLD})
        self.assertEqual(job.summary, "Deleted 2 backup files (2.9 MB).")
        self.assertEqual([s["status"] for s in job.steps], ["done"] * 3)
        self.assertIn(f"deleted {SHOP_OLD}", job.log)
        self.assertIn(f"to delete: {SHOP_OLD}", job.log)

    def test_one_file_is_singular(self):
        job = self.run_job(self.delete_job(SHOP_OLD))
        self.assertEqual(job.summary, "Deleted 1 backup file (976.6 KB).")

    def test_the_names_reach_the_script_only_as_a_variable(self):
        self.run_job(self.delete_job(SHOP_OLD, SHOP_MID))
        (execution,) = self.delete_execs()
        self.assertEqual(execution["env"]["FILES"], f"{SHOP_OLD} {SHOP_MID}")
        self.assertNotIn("shop_", jobs.DELETE_SCRIPT)
        self.assertEqual(execution["container"], self.instance.container_id)

    def test_a_name_asked_for_twice_is_removed_once(self):
        job = self.run_job(self.delete_job(SHOP_OLD, SHOP_OLD))
        self.assertEqual(job.status, Job.Status.DONE)
        self.assertEqual(self.delete_execs()[0]["env"]["FILES"], SHOP_OLD)
        self.assertEqual(job.summary, "Deleted 1 backup file (976.6 KB).")

    def test_the_newest_backup_of_a_database_is_kept(self):
        job = self.run_job(self.delete_job(SHOP_OLD, SHOP_NEW))
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn(f"{SHOP_NEW} is the newest backup of its database and is kept. Nothing was changed.", job.summary)
        self.assertEqual(self.delete_execs(), [])  # the script never ran
        self.assertIn(SHOP_OLD, self.lab.stats)
        self.assertEqual([s["status"] for s in job.steps], ["failed", "todo", "todo"])

    def test_each_database_keeps_its_own_newest(self):
        job = self.run_job(self.delete_job(SHOP_OLD, WIKI_OLD))  # the oldest of each: fine
        self.assertEqual(job.status, Job.Status.DONE, job.summary)
        self.lab.stats[SHOP_OLD] = (1, 1)
        refused = self.run_job(self.delete_job(SHOP_MID, WIKI_NEW))  # the newest wiki is kept
        self.assertEqual(refused.status, Job.Status.FAILED)
        self.assertIn(f"{WIKI_NEW} is the newest", refused.summary)
        self.assertIn(SHOP_MID, self.lab.stats)

    def test_a_zip_that_does_not_follow_the_naming_is_never_deleted(self):
        self.lab.stats["notes_backup.zip"] = (10, 1_700_000_000)
        job = self.run_job(self.delete_job("notes_backup.zip"))
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("is the newest backup of its database", job.summary)

    def test_a_file_that_is_gone_stops_everything_before_a_single_removal(self):
        job = self.run_job(self.delete_job(SHOP_OLD, "shop_20200101_000000.zip"))
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("shop_20200101_000000.zip is no longer in /backups", job.summary)
        self.assertIn("Nothing was changed", job.summary)
        self.assertEqual(self.delete_execs(), [])
        self.assertIn(SHOP_OLD, self.lab.stats)

    def test_a_file_that_vanishes_between_the_check_and_the_script_changes_nothing(self):
        real = self.lab.runner

        def racing(argv, env):
            if argv[:2] == ["sh", "-c"] and argv[2] == jobs.DELETE_SCRIPT:
                self.lab.stats.pop(SHOP_MID)  # retention (or a person) removed it a moment ago
            return real(argv, env)

        self.api.runner = racing
        job = self.run_job(self.delete_job(SHOP_OLD, SHOP_MID))
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("no longer in /backups, so nothing was deleted", job.summary)
        self.assertIn(SHOP_OLD, self.lab.stats)  # the script checks every file before removing any

    def test_a_running_backup_blocks_the_delete(self):
        self.lab.locked = True
        job = self.run_job(self.delete_job(SHOP_OLD))
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("A backup is running, so nothing was deleted", job.summary)
        self.assertEqual([s["status"] for s in job.steps], ["done", "failed", "todo"])
        self.assertIn(SHOP_OLD, self.lab.stats)

    def test_a_script_that_stops_midway_says_some_files_may_be_gone(self):
        self.lab.delete_exit = 1
        job = self.run_job(self.delete_job(SHOP_OLD, SHOP_MID))
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("Some files may already be gone", job.summary)
        self.assertIn("rm: boom", job.log)

    def test_no_files_is_a_failed_job_not_a_crash(self):
        job = self.run_job(self.delete_job())
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("No backup files were selected", job.summary)

    def test_a_stopped_container_fails_it_like_any_other_job(self):
        self.server = Server("vps-1", "docker", [make_instance(status="exited")])
        job = self.run_job(self.delete_job(SHOP_OLD))
        self.assertIn("is exited", job.summary)
        self.assertEqual(self.delete_execs(), [])

    def test_a_delete_cut_off_by_a_restart_is_failed_at_the_next_start(self):
        stuck = self.job(Job.Kind.DELETE, status=Job.Status.RUNNING, files=[SHOP_OLD])
        self.assertEqual(jobs.recover_orphans(), 1)
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, Job.Status.FAILED)
        self.assertIn("restarted", stuck.summary)


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
