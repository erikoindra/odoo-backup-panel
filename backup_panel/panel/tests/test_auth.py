from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

PROXY = "10.0.0.2"  # nginx, as the panel sees it
PASSWORD = "correct-horse-battery-1"


@override_settings(PORTAINER_URL="", PORTAINER_API_KEY="", DOCKER_ENDPOINTS=[])  # never reach a real Docker in tests
class AuthTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("admin", password=PASSWORD)

    def login(self, password, forwarded=None):
        meta = {"REMOTE_ADDR": PROXY}
        if forwarded:
            meta["HTTP_X_FORWARDED_FOR"] = forwarded
        return self.client.post(reverse("login"), {"username": "admin", "password": password}, **meta)


class LoginFlowTests(AuthTestCase):
    def test_root_redirects_to_instance_list(self):
        response = self.client.get("/")
        self.assertRedirects(response, reverse("instance_list"), fetch_redirect_response=False)

    def test_instance_list_requires_login(self):
        response = self.client.get(reverse("instance_list"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('instance_list')}")

    def test_login_page_renders(self):
        response = self.client.get(reverse("login"))
        self.assertContains(response, 'autocomplete="current-password"')
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_successful_login_goes_to_instance_list(self):
        response = self.login(PASSWORD)
        self.assertRedirects(response, reverse("instance_list"))
        self.assertContains(self.client.get(reverse("instance_list")), "<h1>Instances</h1>", html=True)

    def test_failed_login_shows_one_generic_error(self):
        response = self.login("wrong-password")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Wrong username or password.")
        self.assertContains(response, 'aria-invalid="true"')

    def test_logged_in_user_skips_the_login_page(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("login"))
        self.assertRedirects(response, reverse("instance_list"))

    def test_logout_is_post_only(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))
        self.assertRedirects(
            self.client.get(reverse("instance_list")),
            f"{reverse('login')}?next={reverse('instance_list')}",
        )


@override_settings(TRUSTED_PROXY_COUNT=1, AXES_FAILURE_LIMIT=3)
class LockoutTests(AuthTestCase):
    def test_lockout_is_per_client_ip_behind_the_proxy(self):
        for _ in range(3):
            self.login("wrong-password", forwarded="203.0.113.5")
        locked = self.login(PASSWORD, forwarded="203.0.113.5")
        self.assertContains(locked, "Too many attempts", status_code=429)
        # Same nginx address, different client: not locked.
        self.assertRedirects(self.login(PASSWORD, forwarded="203.0.113.9"), reverse("instance_list"))

    def test_forged_forwarded_entries_do_not_dodge_the_lockout(self):
        for i in range(3):
            self.login("wrong-password", forwarded=f"198.51.100.{i}, 203.0.113.5")
        locked = self.login(PASSWORD, forwarded="1.2.3.4, 203.0.113.5")
        self.assertEqual(locked.status_code, 429)
