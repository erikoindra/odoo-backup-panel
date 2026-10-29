"""Contract test: the panel against a real Docker daemon and the real odoo-backup-service image.

It builds a small lab (PostgreSQL, an Odoo data volume holding a filestore, and a backup-service
container), then runs the panel's own code (dockerops and jobs) through backup, download and restore, and
through every way restore can be refused or fail. This is what proves that the shell scripts in
panel/jobs.py really behave the way the unit tests' stand-in assumes. The tests are numbered because
they build on each other, and unittest runs them in name order.

Off by default. Switch on with PANEL_CONTRACT_TESTS=1. Settings:
  CONTRACT_ADMIN_URL   Docker used to build the lab           (default unix:///var/run/docker.sock)
  CONTRACT_PANEL_URL   Docker the panel talks to              (default: same as the admin URL)
                       point it at a docker-socket-proxy to check the permissions the panel needs
  CONTRACT_BACKUP_IMAGE  image under test                     (default ghcr.io/erikoindra/odoo-backup-service:pg17-0.1)
  CONTRACT_POSTGRES_IMAGE                                      (default postgres:17-alpine)
  CONTRACT_ODOO_IMAGE  an Odoo image, e.g. odoo:19.0           (default: unset = skip the neutralize tests)
Everything created is named obp-contract-<random> and removed afterwards.
"""

import hashlib
import io
import os
import secrets
import time
import unittest
import zipfile

import docker
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from panel import dockerops, jobs
from panel.models import Job

ENABLED = os.environ.get("PANEL_CONTRACT_TESTS") == "1"
ADMIN_URL = os.environ.get("CONTRACT_ADMIN_URL", "unix:///var/run/docker.sock")
PANEL_URL = os.environ.get("CONTRACT_PANEL_URL", ADMIN_URL)
BACKUP_IMAGE = os.environ.get("CONTRACT_BACKUP_IMAGE", "ghcr.io/erikoindra/odoo-backup-service:pg17-0.1")
POSTGRES_IMAGE = os.environ.get("CONTRACT_POSTGRES_IMAGE", "postgres:17-alpine")
ODOO_IMAGE = os.environ.get("CONTRACT_ODOO_IMAGE", "")
PASSWORD = "contract-pass-1"
OWNER = "100:101"  # the user Odoo runs as in the official image


def wait_until(check, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.5)
    raise AssertionError(message)


@unittest.skipUnless(ENABLED, "set PANEL_CONTRACT_TESTS=1 to run against a real Docker daemon")
@override_settings(PORTAINER_URL="", PORTAINER_API_KEY="", JOB_POLL_INTERVAL=0.2, JOB_TIMEOUT=300)
class LabCase(TestCase):
    """The lab and the helpers; the scenarios are in the subclasses. A subclass changes DB_NAME and seed()."""

    DB_NAME = "shop"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.names = f"obp-contract-{secrets.token_hex(3)}"
        cls.slug = f"{cls.names}-stack"
        cls.admin = docker.DockerClient(base_url=ADMIN_URL, version="auto", timeout=60)
        cls.made = []  # (kind, name), removed in reverse order
        cls.started_at = cls.admin.info()["SystemTime"][:19]  # anonymous volumes older than this are not ours
        try:
            cls.build_lab()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for kind, name in reversed(getattr(cls, "made", [])):
            try:
                if kind == "container":
                    cls.admin.containers.get(name).remove(force=True, v=True)  # v: also its anonymous volumes
                elif kind == "volume":
                    cls.admin.volumes.get(name).remove(force=True)
                elif kind == "network":
                    cls.admin.networks.get(name).remove()
            except docker.errors.DockerException:
                pass
        super().tearDownClass()

    # --- the lab ---

    @classmethod
    def build_lab(cls):
        for image in (BACKUP_IMAGE, POSTGRES_IMAGE):
            try:
                cls.admin.images.get(image)
            except docker.errors.ImageNotFound:
                cls.admin.images.pull(image)
        net, cls.data, cls.backups = f"{cls.names}-net", f"{cls.names}-data", f"{cls.names}-backups"
        cls.admin.networks.create(net)
        cls.made.append(("network", net))
        for volume in (cls.data, cls.backups):
            cls.admin.volumes.create(volume)
            cls.made.append(("volume", volume))
        cls.db = cls.admin.containers.run(
            POSTGRES_IMAGE,
            detach=True,
            name=f"{cls.names}-db",
            network=net,
            environment={"POSTGRES_USER": "odoo", "POSTGRES_PASSWORD": PASSWORD},
            tmpfs={"/var/lib/postgresql/data": ""},
        )
        cls.made.append(("container", cls.db.name))
        wait_until(lambda: cls.psql("postgres", "select 1")[0] == 0, 90, "PostgreSQL did not start")
        cls.seed()
        cls.backup = cls.admin.containers.run(
            BACKUP_IMAGE,
            detach=True,
            name=f"{cls.names}-backup",
            network=net,
            labels={
                "com.docker.compose.service": "odoo-backup",
                "com.docker.compose.project": cls.slug,
            },
            environment={
                "PGHOST": f"{cls.names}-db",  # Docker's DNS knows containers by name on a user-defined network
                "PGUSER": "odoo",
                "PGPASSWORD": PASSWORD,
                "DB_NAME": cls.DB_NAME,
                "CRON_SCHEDULE": "0 4 * * *",
                "TZ": "UTC",
            },
            volumes={cls.data: {"bind": "/odoo-data", "mode": "ro"}, cls.backups: {"bind": "/backups", "mode": "rw"}},
        )
        cls.made.append(("container", cls.backup.name))
        wait_until(lambda: cls.backup.reload() or cls.backup.status == "running", 30, "backup container did not start")

    @classmethod
    def seed(cls):
        """The database and filestore the backup container will back up. Here: a table that looks like Odoo's."""
        cls.psql("postgres", "CREATE DATABASE shop")
        cls.psql("shop", "CREATE TABLE ir_attachment (id serial PRIMARY KEY, store_fname text)")
        cls.psql("shop", "INSERT INTO ir_attachment (store_fname) VALUES ('ab/abc123'), ('cd/cdef45')")
        cls.volume_shell(
            "mkdir -p /d/filestore/shop/ab /d/filestore/shop/cd "
            "&& echo one > /d/filestore/shop/ab/abc123 && echo two > /d/filestore/shop/cd/cdef45 "
            f"&& chown -R {OWNER} /d/filestore"
        )

    @classmethod
    def psql(cls, database, sql):
        code, out = cls.db.exec_run(
            ["psql", "-h", "127.0.0.1", "-U", "odoo", "-d", database, "-Atc", sql], environment={"PGPASSWORD": PASSWORD}
        )
        return code, out.decode().strip()

    @classmethod
    def volume_shell(cls, script, mode="rw"):
        """Run a shell snippet in a throw-away container with the data volume at /d (and backups at /b)."""
        out = cls.admin.containers.run(
            BACKUP_IMAGE,
            command=[script],
            entrypoint=["sh", "-c"],
            user="0",
            network_mode="none",
            volumes={cls.data: {"bind": "/d", "mode": mode}, cls.backups: {"bind": "/b", "mode": "rw"}},
            tmpfs={"/backups": "size=1m"},  # the image's VOLUME /backups would leave an anonymous volume
            remove=True,
        )
        return out.decode().strip()

    # --- the panel's view of the lab ---

    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password="x" * 14)
        patcher = override_settings(DOCKER_ENDPOINTS=[f"lab={PANEL_URL}"])
        patcher.enable()
        self.addCleanup(patcher.disable)
        dockerops.clear_cache()
        self.endpoint = dockerops.find_endpoint("lab")

    def instance(self):
        server = dockerops.look_at(self.endpoint, use_cache=False)
        self.assertEqual(server.error, "")
        return next(i for i in server.instances if i.slug == self.slug)

    def api(self):
        return dockerops.docker_client(self.endpoint).api

    def zips(self):
        return dockerops.list_backups(self.api(), self.instance())

    def run_job(self, kind, **fields):
        job = Job.objects.create(
            kind=kind,
            endpoint="lab",
            instance_slug=self.slug,
            container_name=self.backup.name,
            created_by=self.user,
            **fields,
        )
        jobs.execute(jobs.claim_next())
        return Job.objects.get(pk=job.pk)

    def rows(self, database):
        code, out = self.psql(database, "select count(*) from ir_attachment")
        return int(out) if code == 0 else None

    def database_exists(self, name):
        return self.psql("postgres", f"select 1 from pg_database where datname = '{name}'")[1] == "1"

    def newest_zip(self):
        return self.zips()[0].name


class ContractTests(LabCase):
    # --- the scenarios, in order ---

    def test_01_the_panel_finds_the_backup_container_and_keeps_no_secret(self):
        instance = self.instance()
        self.assertEqual(instance.container_name, self.backup.name)
        self.assertEqual((instance.backup_volume, instance.odoo_data_volume), (self.backups, self.data))
        self.assertEqual((instance.database, instance.pg_major, instance.status), ("shop", "17", "running"))
        self.assertEqual(instance.schedule, "0 4 * * * · UTC")
        self.assertNotIn(PASSWORD, repr(instance))

    def test_02_backup_now_makes_a_valid_zip_with_dump_and_filestore(self):
        self.assertEqual(self.zips(), [])
        job = self.run_job(Job.Kind.BACKUP)
        self.assertEqual((job.status, job.summary), (Job.Status.DONE, "Backup finished."), job.log)
        self.assertIn("[shop] OK", job.log)
        (backup,) = self.zips()
        self.assertRegex(backup.name, r"^shop_\d{8}_\d{6}\.zip$")
        stream = dockerops.open_backup_stream(self.api(), self.instance(), backup.name)
        data = b"".join(stream.chunks())
        self.assertEqual((len(data), stream.size), (backup.size, backup.size))
        archive = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(archive.testzip())
        self.assertEqual(sorted({n.split("/")[0] for n in archive.namelist()}), ["dump.sql", "filestore"])
        self.assertEqual(archive.read("filestore/ab/abc123"), b"one\n")
        self.assertIn(b"PostgreSQL database dump", archive.read("dump.sql"))

    def test_03_the_download_is_byte_for_byte_the_file_in_the_container(self):
        name = self.newest_zip()
        stream = dockerops.open_backup_stream(self.api(), self.instance(), name)
        digest = hashlib.sha256()
        for chunk in stream.chunks():
            digest.update(chunk)
        code, out, _ = dockerops.run_detached(
            self.api(), self.instance().container_id, ["sha256sum", f"/backups/{name}"]
        )
        self.assertEqual((code, out.decode().split()[0]), (0, digest.hexdigest()))

    def test_04_names_outside_the_listing_are_not_served(self):
        self.volume_shell("echo secret > /b/.hidden.zip; echo x > /b/notes.txt; echo y > /b/.x.zip.partial")
        self.assertEqual([b.name for b in self.zips()], [self.newest_zip()])  # only the real zip is listed
        for bad in ("../etc/passwd.zip", "a b.zip", "notes.txt", ".hidden.zip/../x.zip"):
            with self.subTest(bad), self.assertRaises(dockerops.BackupNotFound):
                dockerops.open_backup_stream(self.api(), self.instance(), bad)
        with self.assertRaises(dockerops.BackupNotFound):
            dockerops.open_backup_stream(self.api(), self.instance(), "missing_20260101_000000.zip")

    def test_05_a_running_scheduled_backup_makes_both_jobs_wait_politely(self):
        holder = self.api().exec_create(
            self.backup.id, ["sh", "-c", "exec 9>/backups/.odoo-backup.lock; flock -n 9; sleep 12"]
        )["Id"]
        self.api().exec_start(holder, detach=True)
        time.sleep(1.5)
        backup = self.run_job(Job.Kind.BACKUP)
        self.assertEqual(backup.status, Job.Status.FAILED)
        self.assertIn("A scheduled backup is running", backup.summary)
        restore = self.run_job(Job.Kind.RESTORE, filename=self.newest_zip(), target_db="shop_locked")
        self.assertEqual(restore.status, Job.Status.FAILED)
        self.assertIn("A backup is running, so the restore did not start", restore.summary)
        self.assertFalse(self.database_exists("shop_locked"))
        wait_until(lambda: not self.api().exec_inspect(holder)["Running"], 30, "lock holder did not finish")

    def test_06_restore_creates_the_database_and_the_filestore(self):
        job = self.run_job(Job.Kind.RESTORE, filename=self.newest_zip(), target_db="shop_copy")
        self.assertEqual(job.status, Job.Status.DONE, job.summary + "\n" + job.log)
        self.assertEqual([s["status"] for s in job.steps], ["done"] * 4)
        self.assertEqual(self.rows("shop_copy"), 2)
        self.assertEqual(
            self.volume_shell("cat /d/filestore/shop_copy/ab/abc123 /d/filestore/shop_copy/cd/cdef45"), "one\ntwo"
        )
        self.assertEqual(
            self.volume_shell("stat -c %u:%g /d/filestore/shop_copy /d/filestore/shop_copy/ab/abc123 | sort -u"), OWNER
        )
        self.assertEqual(self.volume_shell("ls -A /d/filestore"), "shop\nshop_copy")  # no leftover .restore-* folder
        self.assertEqual(self.rows("shop"), 2)  # the original is untouched

    def test_07_restoring_over_an_existing_database_changes_nothing(self):
        before = self.volume_shell("ls -A /d/filestore")
        job = self.run_job(Job.Kind.RESTORE, filename=self.newest_zip(), target_db="shop")
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("The filestore folder for shop already exists", job.summary)  # the folder check comes first
        self.assertEqual(self.volume_shell("ls -A /d/filestore"), before)
        self.assertEqual(self.rows("shop"), 2)

    def test_08_an_existing_database_without_a_folder_is_refused_too(self):
        self.psql("postgres", "CREATE DATABASE shop_orphan")
        job = self.run_job(Job.Kind.RESTORE, filename=self.newest_zip(), target_db="shop_orphan")
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("Database shop_orphan already exists. Nothing was changed.", job.summary)
        self.assertEqual(self.rows("shop_orphan"), None)  # still empty: it has no tables
        self.assertEqual(self.volume_shell("ls -A /d/filestore"), "shop\nshop_copy")

    def test_09_an_existing_filestore_folder_stops_before_the_database_is_made(self):
        self.volume_shell(f"mkdir /d/filestore/shop_folder && chown {OWNER} /d/filestore/shop_folder")
        job = self.run_job(Job.Kind.RESTORE, filename=self.newest_zip(), target_db="shop_folder")
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("already exists", job.summary)
        self.assertFalse(self.database_exists("shop_folder"))

    def test_10_a_broken_zip_leaves_a_reported_half_made_database_and_deletes_nothing(self):
        self.volume_shell("echo this is not a zip > /b/broken_20260101_000000.zip")
        job = self.run_job(Job.Kind.RESTORE, filename="broken_20260101_000000.zip", target_db="shop_broken")
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("Database shop_broken was created, but dump.sql failed to load", job.summary)
        self.assertTrue(self.database_exists("shop_broken"))  # kept for the admin to look at
        self.assertNotIn("shop_broken", self.volume_shell("ls -A /d/filestore"))
        self.assertEqual([s["status"] for s in job.steps], ["done", "failed", "todo", "todo"])

    def test_11_a_zip_without_a_filestore_restores_the_database_and_an_empty_folder(self):
        self.volume_shell(
            "cd /tmp && echo 'create table t(x int);' > dump.sql && zip -q /b/nofs_20260101_000000.zip dump.sql"
        )
        job = self.run_job(Job.Kind.RESTORE, filename="nofs_20260101_000000.zip", target_db="shop_nofs")
        self.assertEqual(job.status, Job.Status.DONE, job.summary + "\n" + job.log)
        self.assertEqual(self.volume_shell("ls -A /d/filestore/shop_nofs | wc -l"), "0")
        self.assertEqual(self.psql("shop_nofs", "select count(*) from t")[1], "0")

    def test_12_a_name_with_shell_characters_is_data_not_code(self):
        # The form never lets such a name through, but the job layer must not depend on that: if the
        # name were part of a script, $(...) would run and leave a file behind.
        self.run_job(Job.Kind.RESTORE, filename=self.newest_zip(), target_db="a$(id>pwned_marker)b")
        _code, out, _ = dockerops.run_detached(
            self.api(), self.instance().container_id, ["sh", "-c", "find / -xdev -name 'pwned_marker*' 2>/dev/null"]
        )
        self.assertEqual(out.strip(), b"")

    def test_13_the_commands_leave_no_temporary_files_in_the_container(self):
        time.sleep(1)  # the cleanup request is sent without waiting for it
        _code, out, _ = dockerops.run_detached(
            self.api(), self.instance().container_id, ["sh", "-c", "ls -d /tmp/obp-* 2>/dev/null"]
        )
        self.assertEqual(len(out.split()), 1)  # only this very command's own directory
        dockerops.sweep_temp(self.api(), self.instance())  # runs without error

    def test_14_no_anonymous_volumes_are_left_behind(self):
        # Every helper container above used the backup image, which declares VOLUME /backups.
        leftovers = [
            v.name
            for v in self.admin.volumes.list(filters={"dangling": True})
            if v.attrs.get("Labels", {}).get("com.docker.volume.anonymous") is not None
            and v.attrs["CreatedAt"] >= self.started_at
        ]
        self.assertEqual(leftovers, [])

    def test_15_a_stopped_container_gives_a_failed_job_not_a_hang(self):
        self.backup.stop(timeout=3)
        try:
            job = self.run_job(Job.Kind.BACKUP)
            self.assertEqual(job.status, Job.Status.FAILED)
            self.assertIn("is exited", job.summary)
        finally:
            self.backup.start()
            wait_until(lambda: self.backup.reload() or self.backup.status == "running", 30, "did not restart")


@unittest.skipUnless(ODOO_IMAGE, "set CONTRACT_ODOO_IMAGE (for example odoo:19.0) to test neutralize on real Odoo")
class NeutralizeContractTests(LabCase):
    """Restore with the neutralize switch, against a database that a real Odoo initialised."""

    DB_NAME = "odoo_shop"
    NEUTRALIZED = "select value from ir_config_parameter where key = 'database.is_neutralized'"

    @classmethod
    def seed(cls):
        try:
            cls.admin.images.get(ODOO_IMAGE)
        except docker.errors.ImageNotFound:
            cls.admin.images.pull(ODOO_IMAGE)
        cls.volume_shell(f"mkdir -p /d/filestore && chown -R {OWNER} /d")
        settings = {"HOST": f"{cls.names}-db", "USER": "odoo", "PASSWORD": PASSWORD}
        cls.admin.containers.run(  # the official image turns HOST/USER/PASSWORD into --db_* options
            ODOO_IMAGE,
            command=["odoo", "-d", cls.DB_NAME, "-i", "base", "--without-demo=all", "--stop-after-init"],
            network=f"{cls.names}-net",
            environment=settings,
            volumes={cls.data: {"bind": "/var/lib/odoo", "mode": "rw"}},
            tmpfs={"/tmp": "", "/mnt/extra-addons": ""},  # the image declares it as a VOLUME
            remove=True,
        )
        cls.odoo = cls.admin.containers.run(  # what a stack's Odoo container looks like to the panel
            ODOO_IMAGE,
            command=["sleep", "infinity"],
            detach=True,
            name=f"{cls.names}-odoo",
            network=f"{cls.names}-net",
            environment=settings,
            volumes={cls.data: {"bind": "/var/lib/odoo", "mode": "rw"}},
            tmpfs={"/mnt/extra-addons": ""},
        )
        cls.made.append(("container", cls.odoo.name))

    def odoo_count(self, database):
        # exec bypasses the image's entrypoint, so the connection options are given like NEUTRALIZE_SCRIPT does
        script = (
            "echo \"print('users:', env['res.users'].search_count([]))\" | "
            f'odoo shell -d {database} --no-http --db_host "$HOST" --db_user "$USER" --db_password "$PASSWORD"'
        )
        code, out = self.odoo.exec_run(["sh", "-c", script])
        return code, out.decode()

    def test_01_the_panel_finds_the_odoo_container_by_its_volume(self):
        found = dockerops.find_odoo_container(self.api(), self.instance())
        self.assertEqual((found.name, found.image), (self.odoo.name, ODOO_IMAGE))
        self.assertNotIn(PASSWORD, repr(found))

    def test_02_restore_with_neutralize_makes_a_usable_neutralized_copy(self):
        self.assertEqual(self.run_job(Job.Kind.BACKUP).status, Job.Status.DONE)
        job = self.run_job(Job.Kind.RESTORE, filename=self.newest_zip(), target_db="odoo_copy", neutralize=True)
        self.assertEqual(job.status, Job.Status.DONE, job.summary + job.log)
        self.assertEqual([s["name"] for s in job.steps][-2], "Neutralize")
        self.assertIn("and neutralized", job.summary)
        self.assertEqual(self.psql("odoo_copy", self.NEUTRALIZED), (0, "true"))
        self.assertNotEqual(self.psql(self.DB_NAME, self.NEUTRALIZED)[1], "true")  # the source is untouched
        code, out = self.odoo_count("odoo_copy")  # Odoo itself can open the restored database
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"users: [1-9]")

    def test_03_restore_without_the_switch_is_not_neutralized(self):
        job = self.run_job(Job.Kind.RESTORE, filename=self.newest_zip(), target_db="odoo_plain")
        self.assertEqual(job.status, Job.Status.DONE, job.summary + job.log)
        self.assertNotEqual(self.psql("odoo_plain", self.NEUTRALIZED)[1], "true")
        self.assertIn("run odoo neutralize first", job.summary)

    def test_04_without_a_running_odoo_container_nothing_is_created(self):
        self.odoo.stop(timeout=2)
        job = self.run_job(Job.Kind.RESTORE, filename=self.newest_zip(), target_db="odoo_never", neutralize=True)
        self.assertEqual(job.status, Job.Status.FAILED)
        self.assertIn("Neutralize was requested", job.summary)
        self.assertIn("Nothing was changed", job.summary)
        self.assertFalse(self.database_exists("odoo_never"))
        self.assertNotIn("odoo_never", self.volume_shell("ls /d/filestore"))
