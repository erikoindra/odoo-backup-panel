"""The checks the restore wizard shows before a restore is queued. They only read; the worker checks
again when the job runs, so these are early feedback, not a guarantee."""

import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from panel import dockerops, jobs
from panel.models import Job

DATABASE_FREE_SCRIPT = """\
set -eu
out=$(psql -X -d postgres -tAc "SELECT datname FROM pg_database") || exit 2
if printf '%s\\n' "$out" | grep -xF "$NEW_DB" >/dev/null; then exit 3; fi
"""


@dataclass(frozen=True, slots=True)
class Check:
    ok: bool
    label: str  # what was checked, as a sentence that is true when ok
    problem: str = ""  # what is wrong, when not ok


def database_is_free(api, instance, db):
    code, _, err = dockerops.run_detached(
        api, instance.container_id, ["sh", "-c", DATABASE_FREE_SCRIPT], environment={"NEW_DB": db}, timeout=30
    )
    if code == 0:
        return Check(True, f"Database {db} does not exist yet")
    if code == 3:
        return Check(False, f"Database {db} does not exist yet", f"Database {db} already exists.")
    return Check(
        False,
        f"Database {db} does not exist yet",
        f"Could not check the database: {err.decode(errors='replace')[:120]}",
    )


def folder_is_free(client, instance, db):
    root = instance.config.get("FILESTORE_ROOT", "/odoo-data/filestore")
    code, text = dockerops.run_helper(
        client,
        instance,
        jobs.FILESTORE_SCRIPT,
        {"NEW_DB": db, "ZIP": "x", "FILESTORE_ROOT": root, "MODE": "check"},
        f"obp-precheck-{secrets.token_hex(3)}",
        odoo_data_mode="ro",
    )
    label = f"Filestore folder {db} does not exist yet"
    if code == 0:
        return Check(True, label)
    return Check(
        False,
        label,
        f"The filestore folder {db} already exists." if code == 3 else f"Could not check the folder: {text[:120]}",
    )


def run_checks(endpoint, instance, db, neutralize):
    """All checks for restoring into `db`. Returns a list of Check, in display order."""
    client = dockerops.docker_client(endpoint)
    with ThreadPoolExecutor(max_workers=2) as pool:
        database = pool.submit(database_is_free, client.api, instance, db)
        folder = pool.submit(folder_is_free, client, instance, db)
        checks = []
        for future in (database, folder):
            try:
                checks.append(future.result())
            except dockerops.DOCKER_ERRORS as e:
                checks.append(Check(False, "Pemeriksaan", dockerops.describe(e)))
    active = Job.active_for(endpoint.name, instance.slug)
    checks.append(
        Check(True, "No other job is running for this instance")
        if not active
        else Check(
            False,
            "No other job is running for this instance",
            f"Job #{active.pk} is still {active.get_status_display().lower()}.",
        )
    )
    if neutralize:
        try:
            odoo = dockerops.find_odoo_container(client.api, instance)
            checks.append(Check(True, f"Odoo container found: {odoo.name} ({odoo.image})"))
        except dockerops.OdooNotFound as e:
            checks.append(Check(False, "Odoo container found", str(e)))
        except dockerops.DOCKER_ERRORS as e:
            checks.append(Check(False, "Odoo container found", dockerops.describe(e)))
    return checks
