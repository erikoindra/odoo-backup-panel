"""Optional env file, so secrets such as the Portainer API key stay out of the shell history."""

import os
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_env_file(path, environ=None):
    """Read KEY=VALUE lines from `path` into `environ` (default os.environ).

    A variable that is already set wins, so a real environment variable always overrides the
    file. Quotes around a value are removed; nothing is expanded, so a `$` stays a `$`.
    Error messages carry the line number only, never the line: it may hold a secret.
    """
    environ = os.environ if environ is None else environ
    file = Path(path).expanduser()
    if not file.is_file():
        raise ImproperlyConfigured(f"PANEL_ENV_FILE does not point to a file: {file}")
    for number, raw in enumerate(file.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not KEY.fullmatch(key):
            raise ImproperlyConfigured(f"{file}:{number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        environ.setdefault(key, value)
