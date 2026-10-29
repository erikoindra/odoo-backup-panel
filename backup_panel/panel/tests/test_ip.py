from django.test import RequestFactory, SimpleTestCase, override_settings

from panel.ip import client_ip

PROXY = "10.0.0.2"  # nginx, as the panel sees it


def request(forwarded=None):
    meta = {"REMOTE_ADDR": PROXY}
    if forwarded is not None:
        meta["HTTP_X_FORWARDED_FOR"] = forwarded
    return RequestFactory().get("/", **meta)


class ClientIpTests(SimpleTestCase):
    @override_settings(TRUSTED_PROXY_COUNT=0)
    def test_no_proxy_uses_remote_addr_and_ignores_header(self):
        self.assertEqual(client_ip(request("203.0.113.5")), PROXY)

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_one_proxy_takes_the_address_nginx_appended(self):
        self.assertEqual(client_ip(request("203.0.113.5")), "203.0.113.5")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_forged_entries_from_the_client_are_ignored(self):
        self.assertEqual(client_ip(request("1.2.3.4, 198.51.100.7,203.0.113.5")), "203.0.113.5")

    @override_settings(TRUSTED_PROXY_COUNT=2)
    def test_two_proxies_take_the_second_entry_from_the_right(self):
        self.assertEqual(client_ip(request("1.2.3.4, 203.0.113.5, 10.0.0.9")), "203.0.113.5")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_missing_or_short_header_falls_back_to_remote_addr(self):
        self.assertEqual(client_ip(request()), PROXY)
        self.assertEqual(client_ip(request(" , ")), PROXY)
