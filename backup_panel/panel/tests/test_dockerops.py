from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import docker
import requests
from django.test import SimpleTestCase, override_settings

from panel import dockerops
from panel.dockerops import ContainerNotRunning, DockerOpsError, Endpoint, Instance, Server
from panel.tests.fakes import SECRET, FakeApi, FakeClient, inspect_result, make_instance, odoo_inspect

NO_SOURCES = {"PORTAINER_URL": "", "PORTAINER_API_KEY": "", "PORTAINER_ENVIRONMENTS": [], "DOCKER_ENDPOINTS": []}
PORTAINER = {**NO_SOURCES, "PORTAINER_URL": "https://portainer.example.com", "PORTAINER_API_KEY": "ptr_secret_key"}


def environment(name, id_=1, type_=2, status=1):
    return {"Id": id_, "Name": name, "Type": type_, "Status": status}


class EndpointTests(SimpleTestCase):
    def setUp(self):
        dockerops.clear_cache()

    def test_docker_base_url_always_has_a_port(self):
        self.assertEqual(
            dockerops.docker_base_url("https://p.example.com", 4), "https://p.example.com:443/api/endpoints/4/docker"
        )
        self.assertEqual(
            dockerops.docker_base_url("http://10.0.0.5:9000/", 4), "http://10.0.0.5:9000/api/endpoints/4/docker"
        )
        self.assertEqual(
            dockerops.docker_base_url("http://p.example.com", 4), "http://p.example.com:80/api/endpoints/4/docker"
        )
        self.assertEqual(
            dockerops.docker_base_url("https://p.example.com/portainer/", 4),
            "https://p.example.com:443/portainer/api/endpoints/4/docker",
        )

    @override_settings(**{**NO_SOURCES, "DOCKER_ENDPOINTS": ["local=tcp://socket-proxy:2375"]})
    def test_docker_endpoints_are_name_equals_url(self):
        self.assertEqual(dockerops.load_endpoints(), [Endpoint("local", "docker", "tcp://socket-proxy:2375")])

    @override_settings(**{**NO_SOURCES, "DOCKER_ENDPOINTS": ["no-equals-sign", "bad name=tcp://x:1"]})
    def test_malformed_docker_endpoints_are_reported_not_guessed(self):
        endpoints = dockerops.load_endpoints()
        self.assertEqual([e.kind for e in endpoints], ["config", "config"])
        self.assertTrue(all("name=url" in e.error for e in endpoints))

    @override_settings(**PORTAINER)
    def test_portainer_environments_become_endpoints(self):
        environments = [
            environment("vps-1", 4),
            environment("vps-down", 5, status=2),
            environment("k8s", 6, type_=6),
            environment("VPS 1", 7),
        ]
        with patch.object(dockerops, "_http_get_json", return_value=environments) as get:
            endpoints = dockerops.load_endpoints()
        get.assert_called_once_with("https://portainer.example.com/api/endpoints", "ptr_secret_key")
        by_name = {e.name: e for e in endpoints}
        self.assertEqual(set(by_name), {"vps-1", "vps-down", "VPS 1"})  # the Kubernetes environment is skipped
        self.assertEqual(by_name["vps-1"].base_url, "https://portainer.example.com:443/api/endpoints/4/docker")
        self.assertEqual(by_name["vps-1"].error, "")
        self.assertIn("down", by_name["vps-down"].error)
        self.assertIn("cannot be used in a panel URL", by_name["VPS 1"].error)

    @override_settings(**PORTAINER)
    def test_the_api_key_is_not_in_the_repr(self):
        with patch.object(dockerops, "_http_get_json", return_value=[environment("vps-1")]):
            endpoint = dockerops.load_endpoints()[0]
        self.assertEqual(endpoint.api_key, "ptr_secret_key")
        self.assertNotIn("ptr_secret_key", repr(endpoint))

    @override_settings(**{**PORTAINER, "PORTAINER_ENVIRONMENTS": ["vps-2"]})
    def test_environment_allowlist(self):
        with patch.object(dockerops, "_http_get_json", return_value=[environment("vps-1", 4), environment("vps-2", 5)]):
            self.assertEqual([e.name for e in dockerops.load_endpoints()], ["vps-2"])

    @override_settings(**PORTAINER)
    def test_unreachable_portainer_is_one_error_endpoint(self):
        with patch.object(dockerops, "_http_get_json", side_effect=requests.ConnectionError("boom")):
            (endpoint,) = dockerops.load_endpoints()
        self.assertIn("Portainer could not be read", endpoint.error)
        self.assertIn("boom", endpoint.error)

    @override_settings(**PORTAINER)
    def test_rejected_api_key_says_so(self):
        response = requests.Response()
        response.status_code = 401
        with patch.object(dockerops, "_http_get_json", side_effect=requests.HTTPError(response=response)):
            (endpoint,) = dockerops.load_endpoints()
        self.assertIn("401", endpoint.error)
        self.assertIn("PORTAINER_API_KEY", endpoint.error)

    @override_settings(**{**PORTAINER, "PORTAINER_API_KEY": ""})
    def test_portainer_url_without_key_is_reported(self):
        (endpoint,) = dockerops.load_endpoints()
        self.assertIn("PORTAINER_API_KEY is not", endpoint.error)

    @override_settings(**PORTAINER)
    def test_environment_list_is_cached_for_a_minute_but_failures_are_not(self):
        with patch.object(dockerops, "_http_get_json", return_value=[environment("vps-1")]) as get:
            dockerops.load_endpoints()
            dockerops.load_endpoints()
        self.assertEqual(get.call_count, 1)
        dockerops.clear_cache()
        with patch.object(dockerops, "_http_get_json", side_effect=[requests.ConnectionError("x"), [environment("a")]]):
            self.assertIn("could not be read", dockerops.load_endpoints()[0].error)
            self.assertEqual(dockerops.load_endpoints()[0].name, "a")

    @override_settings(**{**PORTAINER, "DOCKER_ENDPOINTS": ["vps-1=tcp://a:2375"]})
    def test_a_name_used_twice_is_dropped_and_reported(self):
        with patch.object(dockerops, "_http_get_json", return_value=[environment("vps-1"), environment("vps-2", 2)]):
            endpoints = dockerops.load_endpoints()
        self.assertEqual([e.name for e in endpoints], ["vps-2", "config"])
        self.assertIn("vps-1", endpoints[-1].error)

    @override_settings(**NO_SOURCES)
    def test_nothing_configured(self):
        self.assertFalse(dockerops.is_configured())
        self.assertEqual(dockerops.load_endpoints(), [])


class ClientTests(SimpleTestCase):
    def setUp(self):
        dockerops.clear_cache()

    def test_portainer_client_reads_the_api_version_itself_then_sets_the_key(self):
        endpoint = Endpoint("vps-1", "portainer", "https://p.example.com:443/api/endpoints/4/docker", "ptr_secret_key")
        client = MagicMock()
        client.api.headers = {}
        with (
            patch.object(dockerops, "_http_get_json", return_value={"ApiVersion": "1.44"}) as get,
            patch.object(docker, "DockerClient", return_value=client) as constructor,
        ):
            got = dockerops.docker_client(endpoint)
        get.assert_called_once_with("https://p.example.com:443/api/endpoints/4/docker/version", "ptr_secret_key")
        constructor.assert_called_once_with(base_url=endpoint.base_url, version="1.44", timeout=dockerops.TIMEOUT)
        self.assertEqual(got.api.headers, {"X-API-Key": "ptr_secret_key"})

    def test_plain_docker_endpoint_sends_no_key_and_lets_the_sdk_pick_the_version(self):
        endpoint = Endpoint("local", "docker", "tcp://socket-proxy:2375")
        with patch.object(docker, "DockerClient") as constructor, patch.object(dockerops, "_http_get_json") as get:
            dockerops.docker_client(endpoint)
        get.assert_not_called()
        constructor.assert_called_once_with(
            base_url="tcp://socket-proxy:2375", version="auto", timeout=dockerops.TIMEOUT
        )

    def test_the_client_is_reused_until_it_is_forgotten(self):
        endpoint = Endpoint("local", "docker", "tcp://socket-proxy:2375")
        with patch.object(docker, "DockerClient", side_effect=lambda **_: MagicMock()) as constructor:
            first = dockerops.docker_client(endpoint)
            self.assertIs(dockerops.docker_client(endpoint), first)
            dockerops.forget_client(endpoint)
            self.assertIsNot(dockerops.docker_client(endpoint), first)
        self.assertEqual(constructor.call_count, 2)


class DiscoveryTests(SimpleTestCase):
    def setUp(self):
        dockerops.clear_cache()

    def test_a_good_server_is_remembered_for_a_while_but_can_be_refreshed(self):
        endpoint = Endpoint("vps-1", "docker", "tcp://x:1")
        api = FakeApi([inspect_result()])
        with (
            patch.object(dockerops, "load_endpoints", return_value=[endpoint]),
            patch.object(dockerops, "docker_client", return_value=FakeClient(api)),
        ):
            dockerops.discover_all()
            dockerops.discover_all()
            self.assertEqual(len(api.filters), 1)
            dockerops.discover_all(use_cache=False)
            self.assertEqual(len(api.filters), 2)

    def test_an_error_is_never_remembered(self):
        endpoint = Endpoint("vps-1", "docker", "tcp://x:1")
        clients = [docker.errors.DockerException("down"), FakeClient(FakeApi([inspect_result()]))]
        with (
            patch.object(dockerops, "load_endpoints", return_value=[endpoint]),
            patch.object(dockerops, "docker_client", side_effect=clients),
        ):
            (first,) = dockerops.discover_all()
            (second,) = dockerops.discover_all()
        self.assertIn("down", first.error)
        self.assertEqual((second.error, len(second.instances)), ("", 1))

    def test_a_failure_forgets_the_cached_client(self):
        endpoint = Endpoint("vps-1", "docker", "tcp://x:1")
        cached_key = ("client", "docker", "tcp://x:1", "")
        dockerops._cache[cached_key] = (float("inf"), "old client")
        with patch.object(dockerops, "docker_client", side_effect=docker.errors.DockerException("down")):
            dockerops.discover_server(endpoint)
        self.assertNotIn(cached_key, dockerops._cache)

    def test_instance_from_inspect(self):
        instance = dockerops.instance_from_inspect("vps-1", inspect_result())
        self.assertEqual(instance.endpoint, "vps-1")
        self.assertEqual(instance.slug, "acme-staging-backup")
        self.assertEqual(instance.container_name, "acme-staging-backup-odoo-backup-1")
        self.assertEqual((instance.backup_volume, instance.odoo_data_volume), ("acme_backups", "acme_odoo_data"))
        self.assertEqual(instance.status, "running")
        self.assertEqual(instance.database, "acme_staging")
        self.assertEqual(instance.pg_major, "17")
        self.assertEqual(instance.schedule, "0 2 * * * · Asia/Jakarta")

    def test_only_allowlisted_variables_are_kept_and_never_the_password(self):
        instance = dockerops.instance_from_inspect("vps-1", inspect_result())
        self.assertEqual(
            set(instance.config),
            {"PGHOST", "PGUSER", "DB_NAME", "CRON_SCHEDULE", "TZ", "RETENTION_DAYS", "ODOO_BACKUP_PG_MAJOR"},
        )
        self.assertNotIn(SECRET, repr(instance))
        self.assertNotIn(SECRET, str(asdict(instance)))
        self.assertFalse(any(key.startswith("PGPASSWORD") for key in instance.config))

    def test_other_images_are_ignored(self):
        self.assertIsNone(dockerops.instance_from_inspect("vps-1", inspect_result(Config__Image="nginx:alpine")))

    def test_slug_falls_back_to_the_container_name(self):
        instance = dockerops.instance_from_inspect("vps-1", inspect_result(project=""))
        self.assertEqual(instance.slug, "acme-staging-backup-odoo-backup-1")

    def test_stopped_container_and_bind_mounts(self):
        info = inspect_result(State__Status="exited")
        info["Mounts"] = [
            {"Type": "bind", "Source": "/srv/backups", "Destination": "/backups"},
            {"Type": "volume", "Name": "acme_odoo_data", "Destination": "/odoo-data"},
        ]
        instance = dockerops.instance_from_inspect("vps-1", info)
        self.assertFalse(instance.is_running)
        self.assertEqual(instance.status_label, "Container stopped")
        self.assertEqual(instance.status_class, "status-warn")
        self.assertEqual(instance.backup_volume, "/srv/backups")

    def test_discover_instances_asks_docker_for_the_service_label(self):
        api = FakeApi([inspect_result(), inspect_result("other", "other", Config__Image="nginx:alpine")])
        with patch.object(dockerops, "docker_client", return_value=FakeClient(api)):
            instances = dockerops.discover_instances(Endpoint("vps-1", "docker", "tcp://x:1"))
        self.assertEqual(api.filters, [{"label": ["com.docker.compose.service=odoo-backup"]}])
        self.assertEqual([i.slug for i in instances], ["acme-staging-backup"])

    def test_duplicate_slugs_are_hidden_and_reported(self):
        api = FakeApi(
            [inspect_result("one-1", "same"), inspect_result("two-1", "same"), inspect_result("solo-1", "alone")]
        )
        with patch.object(dockerops, "docker_client", return_value=FakeClient(api)):
            server = dockerops.discover_server(Endpoint("vps-1", "docker", "tcp://x:1"))
        self.assertEqual([i.slug for i in server.instances], ["alone"])
        self.assertIn("same", server.warning)
        self.assertIn("one-1", server.warning)
        self.assertEqual(server.error, "")

    def test_a_failing_endpoint_becomes_an_error_not_an_exception(self):
        with patch.object(dockerops, "docker_client", side_effect=docker.errors.DockerException("daemon down")):
            server = dockerops.discover_server(Endpoint("vps-1", "docker", "tcp://x:1"))
        self.assertEqual(server.instances, [])
        self.assertIn("daemon down", server.error)

    def test_an_endpoint_with_a_known_error_is_not_contacted(self):
        with patch.object(dockerops, "docker_client") as client:
            server = dockerops.discover_server(Endpoint("vps-1", "portainer", error="down"))
        client.assert_not_called()
        self.assertEqual(server, Server("vps-1", "portainer", [], error="down"))

    def test_one_endpoint_failing_does_not_hide_the_others(self):
        good = FakeApi([inspect_result()])

        def client_for(endpoint):
            if endpoint.name == "bad":
                raise docker.errors.DockerException("nope")
            return FakeClient(good)

        endpoints = [Endpoint("good", "docker", "tcp://a:1"), Endpoint("bad", "docker", "tcp://b:1")]
        with (
            patch.object(dockerops, "load_endpoints", return_value=endpoints),
            patch.object(dockerops, "docker_client", side_effect=client_for),
        ):
            servers = dockerops.discover_all()
        self.assertEqual(
            [(s.name, bool(s.error), len(s.instances)) for s in servers], [("good", False, 1), ("bad", True, 0)]
        )


def runner(code=0, out=b"", err=b""):
    calls = []

    def run(argv, env):
        calls.append((argv, env))
        return code, out, err

    run.calls = calls
    return run


class RunDetachedTests(SimpleTestCase):
    def test_returns_exit_code_stdout_and_stderr(self):
        api = FakeApi(runner=runner(7, b"out\n", b"err\n"))
        self.assertEqual(dockerops.run_detached(api, "c1", ["odoo-backup"], poll_interval=0), (7, b"out\n", b"err\n"))

    def test_arguments_and_environment_reach_the_command_untouched(self):
        run = runner()
        api = FakeApi(runner=run)
        argv = ["sh", "-c", "echo \"$X\"; rm -rf '$(whoami)'"]
        dockerops.run_detached(api, "c1", argv, {"X": "a b'c$d"}, poll_interval=0)
        ((got_argv, got_env),) = run.calls
        self.assertEqual(got_argv, argv)
        self.assertEqual(got_env["X"], "a b'c$d")
        self.assertRegex(got_env["JOB"], r"^obp-[0-9a-f]{12}$")

    def test_the_temp_directory_is_removed(self):
        api = FakeApi(runner=runner(0, b"x"))
        dockerops.run_detached(api, "c1", ["true"], poll_interval=0)
        self.assertEqual(api.job_dirs, {})

    def test_the_temp_directory_is_removed_even_when_the_command_cannot_start(self):
        def broken(argv, env):
            raise docker.errors.APIError("boom")

        api = FakeApi(runner=broken)
        with self.assertRaises(docker.errors.APIError):
            dockerops.run_detached(api, "c1", ["true"], poll_interval=0)
        self.assertEqual(api.job_dirs, {})

    def test_timeout_raises_and_still_cleans_up(self):
        api = FakeApi(runner=runner())
        api.running_forever = True
        with (
            self.assertRaises(TimeoutError),
            patch.object(dockerops, "wait_for_exec", side_effect=[TimeoutError("slow"), 0]),
        ):
            dockerops.run_detached(api, "c1", ["sleep", "999"], timeout=0, poll_interval=0)
        self.assertEqual(api.job_dirs, {})
        self.assertEqual(api.execs["exec2"]["cmd"][:2], ["rm", "-rf"])

    def test_wait_for_exec_times_out(self):
        api = FakeApi()
        api.execs["e"] = {"cmd": [], "env": {}, "rc": None}
        api.running_forever = True
        with self.assertRaises(TimeoutError):
            dockerops.wait_for_exec(api, "e", timeout=0.05, poll_interval=0.01)


STAT_OUTPUT = (
    b"/backups/acme_20261027_020000.zip|6600000|1793000000\n"
    b"/backups/acme_20261028_020000.zip|6700000|1793086400\n"
    b"/backups/notes.zip.bak|10|1793086400\n"
    b"/backups/we ird.zip|10|1793086400\n"
    b"/backups/acme_20261026_020000.zip|6500000|not-a-number\n"
    b"garbage line\n"
    b"\n"
)


class ListBackupsTests(SimpleTestCase):
    def instance(self, **overrides):
        return Instance(
            **{
                "endpoint": "vps-1",
                "slug": "acme",
                "container_id": "c1",
                "container_name": "acme-1",
                "image": "i",
                "status": "running",
                "backup_volume": "v",
                "odoo_data_volume": "d",
                "config": {},
                **overrides,
            }
        )

    def test_parses_and_sorts_newest_first_and_skips_anything_else(self):
        api = FakeApi(runner=runner(0, STAT_OUTPUT))
        with patch.object(dockerops, "run_detached", wraps=dockerops.run_detached) as spy:
            backups = dockerops.list_backups(api, self.instance())
        self.assertEqual([b.name for b in backups], ["acme_20261028_020000.zip", "acme_20261027_020000.zip"])
        self.assertEqual(backups[0].size, 6700000)
        self.assertEqual(backups[0].mtime, datetime.fromtimestamp(1793086400, UTC))
        self.assertEqual(spy.call_args.args[2][:2], ["sh", "-c"])

    def test_no_zip_files_is_an_empty_list(self):
        self.assertEqual(dockerops.list_backups(FakeApi(runner=runner(0, b"")), self.instance()), [])

    def test_stopped_container_is_refused_without_touching_docker(self):
        api = FakeApi()
        with self.assertRaises(ContainerNotRunning):
            dockerops.list_backups(api, self.instance(status="exited"))
        self.assertEqual(api.execs, {})

    def test_a_failing_command_is_an_error_with_its_stderr(self):
        api = FakeApi(runner=runner(2, b"", b"stat: boom\n"))
        with self.assertRaisesRegex(DockerOpsError, "stat: boom"):
            dockerops.list_backups(api, self.instance())


class LookAtTests(SimpleTestCase):
    def setUp(self):
        dockerops.clear_cache()

    def test_find_endpoint_by_name(self):
        endpoints = [Endpoint("vps-1", "docker", "tcp://a:1"), Endpoint("vps-2", "docker", "tcp://b:1")]
        with patch.object(dockerops, "load_endpoints", return_value=endpoints):
            self.assertEqual(dockerops.find_endpoint("vps-2"), endpoints[1])
            self.assertIsNone(dockerops.find_endpoint("nope"))

    def test_look_at_remembers_a_good_server_and_use_cache_false_reads_again(self):
        endpoint = Endpoint("vps-1", "docker", "tcp://x:1")
        api = FakeApi([inspect_result()])
        with patch.object(dockerops, "docker_client", return_value=FakeClient(api)):
            first = dockerops.look_at(endpoint)
            self.assertIs(dockerops.look_at(endpoint), first)
            self.assertEqual(len(api.filters), 1)
            dockerops.look_at(endpoint, use_cache=False)
            self.assertEqual(len(api.filters), 2)


class ListingCacheTests(SimpleTestCase):
    endpoint = Endpoint("vps-1", "docker", "tcp://x:1")

    def setUp(self):
        dockerops.clear_cache()
        self.instance = dockerops.instance_from_inspect("vps-1", inspect_result())

    def test_a_listing_is_kept_for_a_while_and_can_be_read_again(self):
        api = FakeApi(runner=runner(0, STAT_OUTPUT))
        with patch.object(dockerops, "docker_client", return_value=FakeClient(api)):
            first = dockerops.list_backups_cached(self.endpoint, self.instance)
            self.assertIs(dockerops.list_backups_cached(self.endpoint, self.instance), first)
            self.assertEqual(len(api.execs), 2)  # one command and its cleanup: the second call read nothing
            fresh = dockerops.list_backups_cached(self.endpoint, self.instance, use_cache=False)
            self.assertEqual(len(api.execs), 4)
            self.assertIs(dockerops.list_backups_cached(self.endpoint, self.instance), fresh)  # and keeps that one
        self.assertEqual([b.name for b in first.backups], ["acme_20261028_020000.zip", "acme_20261027_020000.zip"])
        self.assertEqual(first.latest.name, "acme_20261028_020000.zip")
        self.assertEqual(first.total_size, 6700000 + 6600000)
        self.assertEqual(first.read_at.tzinfo, UTC)

    def test_a_failed_read_is_never_remembered(self):
        outcomes = [(2, b"", b"boom"), (0, STAT_OUTPUT, b"")]
        api = FakeApi(runner=lambda argv, env: outcomes.pop(0))
        with patch.object(dockerops, "docker_client", return_value=FakeClient(api)):
            with self.assertRaises(DockerOpsError):
                dockerops.list_backups_cached(self.endpoint, self.instance)
            self.assertEqual(len(dockerops.list_backups_cached(self.endpoint, self.instance).backups), 2)

    def test_each_container_has_its_own_listing(self):
        other = dockerops.instance_from_inspect("vps-1", inspect_result("other-1", "other"))
        api = FakeApi(runner=runner(0, STAT_OUTPUT))
        with patch.object(dockerops, "docker_client", return_value=FakeClient(api)):
            a = dockerops.list_backups_cached(self.endpoint, self.instance)
            b = dockerops.list_backups_cached(self.endpoint, other)
        self.assertIsNot(a, b)

    def test_no_files_gives_an_empty_listing_without_a_latest(self):
        api = FakeApi(runner=runner(0, b""))
        with patch.object(dockerops, "docker_client", return_value=FakeClient(api)):
            listing = dockerops.list_backups_cached(self.endpoint, self.instance)
        self.assertEqual((listing.backups, listing.latest, listing.total_size), ([], None, 0))


class RetentionLabelTests(SimpleTestCase):
    def label(self, **config):
        info = inspect_result()
        info["Config"]["Env"] = [f"{k}={v}" for k, v in config.items()]
        instance = dockerops.instance_from_inspect("vps-1", info)
        return instance.retention_days, instance.retention_label

    def test_labels(self):
        self.assertEqual(self.label(RETENTION_DAYS="3"), (3, "Retention 3 days"))
        self.assertEqual(self.label(RETENTION_DAYS="0"), (0, "No automatic deletion"))
        self.assertEqual(self.label(), (None, ""))
        self.assertEqual(self.label(RETENTION_DAYS="soon"), (None, ""))


class CleanupTests(SimpleTestCase):
    def test_the_cleanup_is_sent_but_not_waited_for(self):
        api = FakeApi(runner=runner(0, b"x"))
        dockerops.run_detached(api, "c1", ["true"], poll_interval=0)
        self.assertEqual(api.inspect_calls, 1)  # only the command itself is polled
        self.assertEqual([e["cmd"][0] for e in api.execs.values()], ["sh", "rm"])
        self.assertEqual(api.job_dirs, {})

    def test_a_failing_cleanup_is_logged_and_does_not_hide_the_result(self):
        api = FakeApi(runner=runner(0, b"ok"))
        original = api.exec_create

        def flaky(container_id, cmd, environment=None, **kw):
            if cmd[0] == "rm":
                raise docker.errors.APIError("cleanup failed")
            return original(container_id, cmd, environment, **kw)

        api.exec_create = flaky
        with self.assertLogs("panel.dockerops", level="WARNING") as logs:
            result = dockerops.run_detached(api, "c1", ["true"], poll_interval=0)
        self.assertEqual(result, (0, b"ok", b""))
        self.assertIn("could not remove", logs.output[0])


# 204 800 bytes: well above tarfile's 10 KB read-ahead, so "not read ahead" means something.
PAYLOAD = bytes(range(256)) * 800


class OpenBackupStreamTests(SimpleTestCase):
    path = "/backups/acme_20261028_020000.zip"
    name = "acme_20261028_020000.zip"

    def setUp(self):
        self.instance = dockerops.instance_from_inspect("vps-1", inspect_result())
        self.api = FakeApi()
        self.api.files[self.path] = PAYLOAD

    def test_streams_exactly_the_file_in_pieces_no_bigger_than_the_chunk(self):
        with patch.object(dockerops, "DOWNLOAD_CHUNK", 1000):
            download = dockerops.open_backup_stream(self.api, self.instance, self.name)
            pieces = list(download.chunks())
        self.assertEqual((download.name, download.size), (self.name, len(PAYLOAD)))
        self.assertEqual(b"".join(pieces), PAYLOAD)
        self.assertGreater(len(pieces), 100)
        self.assertTrue(all(len(piece) <= 1000 for piece in pieces))
        self.assertTrue(self.api.streams[0].closed)  # the HTTP stream is released when done

    def test_the_whole_file_is_never_read_ahead(self):
        with patch.object(dockerops, "DOWNLOAD_CHUNK", 1000):
            download = dockerops.open_backup_stream(self.api, self.instance, self.name)
            self.assertLess(self.api.streams[0].sent, 30_000)  # opening reads the tar header, not the file
            chunks = download.chunks()
            next(chunks)
            self.assertLess(self.api.streams[0].sent, 30_000)
            chunks.close()

    def test_stopping_early_closes_the_http_stream(self):
        download = dockerops.open_backup_stream(self.api, self.instance, self.name)
        chunks = download.chunks()
        next(chunks)
        self.assertFalse(self.api.streams[0].closed)
        chunks.close()
        self.assertTrue(self.api.streams[0].closed)

    def test_a_missing_file_is_backup_not_found(self):
        with self.assertRaisesRegex(dockerops.BackupNotFound, "no longer in /backups"):
            dockerops.open_backup_stream(self.api, self.instance, "other.zip")

    def test_a_bad_name_never_reaches_docker(self):
        for name in ("../x.zip", "x.txt", "a b.zip", "x.zip/..", "", "/etc/passwd.zip"):
            with self.subTest(name), self.assertRaises(dockerops.BackupNotFound):
                dockerops.open_backup_stream(self.api, self.instance, name)
        self.assertEqual(self.api.streams, [])

    def test_a_stopped_container_is_refused_without_asking_docker(self):
        instance = dockerops.instance_from_inspect("vps-1", inspect_result(State__Status="exited"))
        with self.assertRaises(ContainerNotRunning):
            dockerops.open_backup_stream(self.api, instance, self.name)
        self.assertEqual(self.api.streams, [])

    def test_without_a_size_from_docker_it_refuses_and_releases_the_stream(self):
        self.api.stat_size = False
        with self.assertRaisesRegex(DockerOpsError, "file size"):
            dockerops.open_backup_stream(self.api, self.instance, self.name)
        self.assertTrue(self.api.streams[0].closed)

    def test_a_size_that_disagrees_with_the_archive_is_refused(self):
        self.api.stat_size = len(PAYLOAD) + 5
        with self.assertRaisesRegex(DockerOpsError, "not what was expected"):
            dockerops.open_backup_stream(self.api, self.instance, self.name)
        self.assertTrue(self.api.streams[0].closed)

    def test_something_that_is_not_a_tar_archive_is_an_error_not_a_crash(self):
        self.api.files[self.path] = PAYLOAD
        original = self.api._file_archive

        def garbage(path):
            stream, stat = original(path)
            stream.__dict__["_pieces"] = iter([b"this is not a tar file" * 50])
            return stream, stat

        self.api._file_archive = garbage
        with self.assertRaisesRegex(DockerOpsError, "cannot be read"):
            dockerops.open_backup_stream(self.api, self.instance, self.name)
        self.assertTrue(self.api.streams[0].closed)

    def test_a_connection_that_drops_midway_raises_instead_of_looking_complete(self):
        self.api.fail_after = 60_000
        download = dockerops.open_backup_stream(self.api, self.instance, self.name)
        received = []
        with self.assertRaises(ConnectionError):  # the transport error surfaces as it is
            received.extend(download.chunks())
        self.assertLess(sum(map(len, received)), len(PAYLOAD))
        self.assertTrue(self.api.streams[0].closed)

    def test_a_stream_cut_short_is_an_error(self):
        download = dockerops.open_backup_stream(self.api, self.instance, self.name)
        stream = self.api.streams[0]
        stream._pieces = iter(list(stream._pieces)[:20])  # the connection ends cleanly, but too early
        with self.assertRaisesRegex(DockerOpsError, "broke off"):
            b"".join(download.chunks())
        self.assertTrue(stream.closed)


class FindOdooContainerTests(SimpleTestCase):
    """The Odoo container is found by the named volume at /var/lib/odoo, not by its name."""

    def find(self, *odoo, instance=None):
        instance = instance or make_instance()
        api = FakeApi([inspect_result(), *odoo])
        api.inspects[f"id-{instance.slug}"] = api.inspects.pop("id-acme-staging-backup-odoo-backup-1")
        return dockerops.find_odoo_container(api, instance), api

    def test_the_one_running_container_with_the_volume_is_found(self):
        found, api = self.find(odoo_inspect())
        self.assertEqual((found.container_id, found.name, found.image), ("id-acme-odoo-1", "acme-odoo-1", "odoo:19.0"))
        self.assertEqual(api.filters, [{"volume": ["acme_odoo_data"]}])

    def test_the_result_carries_no_environment(self):
        found, _ = self.find(odoo_inspect())
        self.assertNotIn(SECRET, repr(found))
        self.assertEqual(set(asdict(found)), {"container_id", "name", "image"})

    def test_the_backup_container_itself_is_not_the_odoo_container(self):
        with self.assertRaisesRegex(dockerops.OdooNotFound, "No Odoo container"):
            self.find()

    def test_the_volume_must_be_mounted_at_the_odoo_data_dir(self):
        with self.assertRaisesRegex(dockerops.OdooNotFound, "No Odoo container"):
            self.find(odoo_inspect(destination="/mnt/other"))

    def test_a_stopped_container_is_reported_as_such(self):
        with self.assertRaisesRegex(dockerops.OdooNotFound, "acme-odoo-1 is exited"):
            self.find(odoo_inspect(status="exited"))

    def test_two_candidates_are_ambiguous_and_both_are_named(self):
        with self.assertRaisesRegex(dockerops.OdooNotFound, "acme-odoo-1, acme-odoo-2"):
            self.find(odoo_inspect(), odoo_inspect("acme-odoo-2"))

    def test_a_bind_mounted_data_directory_cannot_be_searched(self):
        instance = make_instance()
        object.__setattr__(instance, "odoo_data_volume", "/srv/odoo-data")
        with self.assertRaisesRegex(dockerops.OdooNotFound, "not a named volume"):
            self.find(odoo_inspect(), instance=instance)
        object.__setattr__(instance, "odoo_data_volume", "")
        with self.assertRaises(dockerops.OdooNotFound):
            self.find(odoo_inspect(), instance=instance)
