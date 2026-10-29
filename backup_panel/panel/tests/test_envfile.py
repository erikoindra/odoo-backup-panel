import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from config.envfile import load_env_file


class EnvFileTests(SimpleTestCase):
    def load(self, text, environ=None):
        environ = {} if environ is None else environ
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "panel.env"
            path.write_text(text)
            load_env_file(path, environ)
        return environ

    def test_reads_plain_quoted_and_exported_values(self):
        environ = self.load(
            "# comment\n\nPORTAINER_URL=https://portainer.example.com\n"
            "PORTAINER_API_KEY='ptr_abc'\n"
            'DOUBLE="two words"\n'
            "export EXPORTED=yes\n"
            "  SPACED  =  value  \n"
            "EMPTY=\n"
        )
        self.assertEqual(
            environ,
            {
                "PORTAINER_URL": "https://portainer.example.com",
                "PORTAINER_API_KEY": "ptr_abc",
                "DOUBLE": "two words",
                "EXPORTED": "yes",
                "SPACED": "value",
                "EMPTY": "",
            },
        )

    def test_nothing_is_expanded(self):
        self.assertEqual(self.load("TOKEN=pa$$word${HOME}`id`\n"), {"TOKEN": "pa$$word${HOME}`id`"})

    def test_a_value_may_contain_an_equals_sign(self):
        self.assertEqual(self.load("URL=https://x.example.com/?a=b\n"), {"URL": "https://x.example.com/?a=b"})

    def test_real_environment_wins_over_the_file(self):
        environ = self.load("A=from-file\nB=from-file\n", {"A": "from-env"})
        self.assertEqual(environ, {"A": "from-env", "B": "from-file"})

    def test_a_missing_file_is_an_error(self):
        with self.assertRaisesRegex(ImproperlyConfigured, "does not point to a file"):
            load_env_file("/nonexistent/panel.env", {})

    def test_a_bad_line_is_reported_by_number_and_never_echoed(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            self.load("GOOD=1\nthis-line-holds-a-secret-token\n")
        self.assertIn(":2:", str(caught.exception))
        self.assertNotIn("secret-token", str(caught.exception))

    def test_a_bad_name_is_an_error(self):
        with self.assertRaises(ImproperlyConfigured):
            self.load("1BAD=x\n")
