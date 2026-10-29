from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from panel import dockerops
from panel.dockerops import Server
from panel.tests.fakes import make_instance as instance

SOURCES = {"PORTAINER_URL": "https://portainer.example.com", "PORTAINER_API_KEY": "k", "DOCKER_ENDPOINTS": []}


@override_settings(**SOURCES)
class InstanceListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("admin", password="x" * 14)

    def get(self, servers, query=""):
        self.client.force_login(self.user)
        with patch.object(dockerops, "discover_all", return_value=servers) as discover:
            response = self.client.get(reverse("instance_list") + query)
        self.discover = discover
        return response

    def test_requires_login_and_never_contacts_docker_before_that(self):
        with patch.object(dockerops, "discover_all") as discover:
            response = self.client.get(reverse("instance_list"))
        self.assertEqual(response.status_code, 302)
        discover.assert_not_called()

    def test_servers_are_grouped_with_their_stacks(self):
        response = self.get(
            [
                Server("vps-1", "portainer", [instance(), instance("demo-backup", status="exited", database="")]),
                Server("vps-2", "docker", [instance("other-backup")]),
            ]
        )
        self.assertContains(response, "3 instances on 2 servers")
        self.assertContains(response, "Connected · Portainer")
        self.assertContains(response, "Connected · Docker")
        self.assertContains(response, "acme-staging-backup")
        self.assertContains(response, "acme_staging")
        self.assertContains(response, "Instance</th>")
        self.assertContains(response, "Last backup</th>")
        self.assertContains(response, "Container stopped")
        self.assertContains(response, "all databases")

    def test_every_stack_links_to_its_detail_page(self):
        response = self.get([Server("vps-1", "portainer", [instance()])])
        self.assertContains(response, 'href="/e/vps-1/acme-staging-backup/"', count=2)  # the name and "Open"
        self.assertContains(response, 'Open<span class="sr-only"> acme-staging-backup</span>')

    def test_running_stacks_load_their_backup_summary_on_demand_and_stopped_ones_do_not(self):
        response = self.get([Server("vps-1", "portainer", [instance(), instance("demo-backup", status="exited")])])
        self.assertContains(response, 'hx-get="/e/vps-1/acme-staging-backup/summary/"', count=1)
        self.assertContains(response, 'hx-swap="outerHTML"', count=1)  # the loading row is replaced by the real one
        self.assertContains(response, 'data-level="loading"', count=1)
        self.assertContains(response, "skel")
        self.assertNotContains(response, "/e/vps-1/demo-backup/summary/")
        self.assertContains(response, 'data-level="stopped"', count=1)
        self.assertContains(response, "Backups cannot be read while the container is stopped")

    def test_reloading_the_status_reads_again_and_passes_that_on_to_every_row(self):
        response = self.get([Server("vps-1", "portainer", [instance()])])
        self.assertContains(response, 'href="/instances/?refresh=1"')
        self.discover.assert_called_once_with(use_cache=True)
        self.assertNotContains(response, "summary/?refresh=1")
        response = self.get([Server("vps-1", "portainer", [instance()])], "?refresh=1")
        self.discover.assert_called_once_with(use_cache=False)
        self.assertContains(response, 'hx-get="/e/vps-1/acme-staging-backup/summary/?refresh=1"')

    def test_the_header_has_places_for_the_counts_the_script_fills_in(self):
        response = self.get([Server("vps-1", "portainer", [instance()])])
        self.assertContains(response, 'id="result-count"')
        self.assertContains(response, 'id="health-count"')

    def test_search_box_is_there_when_there_are_servers(self):
        self.assertContains(self.get([Server("vps-1", "portainer", [instance()])]), "data-search")
        self.assertContains(self.get([Server("vps-1", "portainer", [instance()])]), 'data-group="vps-1"')

    def test_an_unreachable_server_shows_an_error_and_retry_and_leaves_the_rest(self):
        response = self.get(
            [
                Server("vps-1", "portainer", [instance()]),
                Server("vps-3", "portainer", [], error="timeout after 20 seconds"),
            ]
        )
        self.assertContains(response, "Unreachable")
        self.assertContains(response, "timeout after 20 seconds")
        self.assertContains(response, "Retry")
        self.assertContains(response, "acme-staging-backup")

    def test_a_warning_is_shown_next_to_the_instances_that_remain(self):
        response = self.get([Server("vps-1", "portainer", [instance()], warning="Duplicate stack names: same")])
        self.assertContains(response, "Duplicate stack names: same")
        self.assertContains(response, "acme-staging-backup")

    def test_a_server_without_backup_containers_says_so(self):
        response = self.get([Server("vps-1", "portainer", [])])
        self.assertContains(response, "No odoo-backup containers on this server.")

    def test_error_text_from_a_server_is_escaped(self):
        response = self.get([Server("vps-1", "portainer", [], error="<script>alert(1)</script>")])
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")

    @override_settings(PORTAINER_URL="", PORTAINER_API_KEY="", DOCKER_ENDPOINTS=[])
    def test_nothing_configured_explains_what_to_set(self):
        response = self.get([])
        self.assertContains(response, "No server connected yet")
        self.assertContains(response, "PORTAINER_API_KEY")
        self.assertNotContains(response, "data-search")
