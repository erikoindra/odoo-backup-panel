from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from panel import checks, dockerops
from panel.dockerops import Endpoint
from panel.models import Job
from panel.tests.fakes import FakeApi, FakeClient, make_instance, odoo_inspect

ENDPOINT = Endpoint("vps-1", "docker", "tcp://x:1")
NOTHING = {"PORTAINER_URL": "", "PORTAINER_API_KEY": "", "DOCKER_ENDPOINTS": []}


class FakeDatabase:
    def __init__(self, names=("postgres", "acme_staging"), exit_code=None):
        self.names, self.exit_code = set(names), exit_code
        self.calls = []

    def runner(self, argv, env):
        self.calls.append((argv, env))
        if self.exit_code is not None:
            return self.exit_code, b"", b"psql: could not connect"
        return (3 if env["NEW_DB"] in self.names else 0), b"", b""


@override_settings(**NOTHING, JOB_POLL_INTERVAL=0)
class CheckTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("admin", password="x" * 14)

    def setUp(self):
        self.database = FakeDatabase()
        self.folders = {"acme_staging"}
        self.helper_exit = None
        self.api = FakeApi(runner=self.database.runner)
        self.api.inspects["id-acme-odoo-1"] = odoo_inspect()
        self.client_ = FakeClient(self.api, self.helper)
        self.instance = make_instance()
        patcher = patch.object(dockerops, "docker_client", return_value=self.client_)
        patcher.start()
        self.addCleanup(patcher.stop)

    def helper(self, call):
        env = call["environment"]
        if self.helper_exit is not None:
            return self.helper_exit, b"docker: no space left"
        return (3 if env["NEW_DB"] in self.folders else 0), b""

    def test_a_free_database_passes(self):
        result = checks.database_is_free(self.api, self.instance, "acme_copy")
        self.assertEqual((result.ok, result.label), (True, "Database acme_copy does not exist yet"))
        ((argv, env),) = self.database.calls
        self.assertEqual(env["NEW_DB"], "acme_copy")
        self.assertNotIn("acme_copy", argv[-1])  # the name travels as a variable only

    def test_an_existing_database_fails_with_its_name(self):
        result = checks.database_is_free(self.api, self.instance, "acme_staging")
        self.assertFalse(result.ok)
        self.assertEqual(result.problem, "Database acme_staging already exists.")

    def test_a_database_that_cannot_be_asked_is_not_reported_as_free(self):
        self.database.exit_code = 2
        result = checks.database_is_free(self.api, self.instance, "acme_copy")
        self.assertFalse(result.ok)
        self.assertIn("Could not check the database", result.problem)

    def test_the_folder_check_mounts_the_data_volume_read_only(self):
        result = checks.folder_is_free(self.client_, self.instance, "acme_copy")
        self.assertTrue(result.ok)
        (call,) = self.client_.containers.calls
        self.assertEqual(call["environment"]["MODE"], "check")
        self.assertEqual(call["volumes"]["acme_odoo_data"], {"bind": "/d", "mode": "ro"})
        self.assertEqual(call["volumes"]["acme_backups"], {"bind": "/b", "mode": "ro"})

    def test_an_existing_folder_fails_and_an_unknown_error_is_not_free(self):
        result = checks.folder_is_free(self.client_, self.instance, "acme_staging")
        self.assertEqual((result.ok, result.problem), (False, "The filestore folder acme_staging already exists."))
        self.helper_exit = 1
        result = checks.folder_is_free(self.client_, self.instance, "acme_copy")
        self.assertFalse(result.ok)
        self.assertIn("no space left", result.problem)

    def test_all_checks_in_display_order(self):
        results = checks.run_checks(ENDPOINT, self.instance, "acme_copy", neutralize=False)
        self.assertEqual(
            [c.label for c in results],
            [
                "Database acme_copy does not exist yet",
                "Filestore folder acme_copy does not exist yet",
                "No other job is running for this instance",
            ],
        )
        self.assertTrue(all(c.ok for c in results))

    def test_the_odoo_container_is_checked_only_when_neutralize_is_asked(self):
        results = checks.run_checks(ENDPOINT, self.instance, "acme_copy", neutralize=True)
        self.assertEqual(results[-1].label, "Odoo container found: acme-odoo-1 (odoo:19.0)")
        self.assertTrue(results[-1].ok)
        del self.api.inspects["id-acme-odoo-1"]
        results = checks.run_checks(ENDPOINT, self.instance, "acme_copy", neutralize=True)
        self.assertFalse(results[-1].ok)
        self.assertIn("No Odoo container", results[-1].problem)

    def test_an_active_job_fails_the_check(self):
        job = Job.objects.create(
            kind=Job.Kind.BACKUP,
            status=Job.Status.RUNNING,
            endpoint="vps-1",
            instance_slug=self.instance.slug,
            container_name="c",
            created_by=self.user,
        )
        results = checks.run_checks(ENDPOINT, self.instance, "acme_copy", neutralize=False)
        self.assertFalse(results[2].ok)
        self.assertIn(f"Job #{job.pk} is still running", results[2].problem)

    def test_a_docker_error_in_one_check_does_not_hide_the_others(self):
        with patch.object(checks, "folder_is_free", side_effect=TimeoutError("slow")):
            results = checks.run_checks(ENDPOINT, self.instance, "acme_copy", neutralize=False)
        self.assertTrue(results[0].ok)
        self.assertEqual((results[1].ok, results[1].label), (False, "Pemeriksaan"))
        self.assertIn("slow", results[1].problem)
