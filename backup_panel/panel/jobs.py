"""Backup and restore jobs, run by the worker (`manage.py run_jobs`). See docs/spec.md sections 5.2, 5.3.

All Docker access goes through panel.dockerops. Values for a command (database name, file name) reach
the shell only as environment variables, never inside the script text.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from panel import dockerops
from panel.models import Job

log = logging.getLogger(__name__)

LOG_LIMIT = 200_000  # characters of command output kept in Job.log
RUNNING_BACKUP = "another backup is already running"  # what odoo-backup says when it holds the lock

# exit codes the restore scripts use for the situations that have their own message
EXISTS, LOCKED, GONE = 3, 4, 5

# Runs in the backup container (it has psql, createdb, unzip and the PG* environment). It holds the same
# lock as odoo-backup, so a scheduled backup or its retention cannot run in the middle of a restore.
# `grep` has no -q: with pipefail an early exit would make psql die of SIGPIPE and look like "no match".
DATABASE_SCRIPT = """\
set -eu
set -o pipefail
exec 9>/backups/.odoo-backup.lock
flock -n 9 || exit 4
[ -f "/backups/$ZIP" ] || exit 5
if psql -X -d postgres -tAc "SELECT datname FROM pg_database" | grep -xF "$NEW_DB" >/dev/null; then exit 3; fi
createdb "$NEW_DB"
echo db-created
unzip -p "/backups/$ZIP" dump.sql | psql -X -v ON_ERROR_STOP=1 -q -d "$NEW_DB"
echo dump-loaded
"""

# Runs in a helper container: /b is the backup volume (read-only), /d the Odoo data volume.
# MODE=check only looks; MODE=copy extracts filestore/ next to the new database's name.
FILESTORE_SCRIPT = """\
set -eu
FS="/d${FILESTORE_ROOT#/odoo-data}"
T="$FS/$NEW_DB"
W="$FS/.restore-$NEW_DB"
if [ -e "$T" ] || [ -e "$W" ]; then exit 3; fi
[ "$MODE" = copy ] || exit 0
[ -f "/b/$ZIP" ] || exit 5
if [ ! -d "$FS" ]; then mkdir -p "$FS"; chown "$(stat -c %u:%g /d)" "$FS"; fi
mkdir "$W"
unzip -q "/b/$ZIP" 'filestore/*' -d "$W" || [ $? -eq 11 ]
if [ -d "$W/filestore" ]; then mv "$W/filestore" "$T"; else mkdir "$T"; fi
rmdir "$W"
chown -R "$(stat -c %u:%g "$FS")" "$T"
"""


# Runs inside the ODOO container, which already has its database settings (the official image keeps
# them in HOST, USER and PASSWORD) and its odoo.conf. The new database name arrives as NEW_DB.
NEUTRALIZE_SCRIPT = """\
set -eu
set -- odoo neutralize -d "$NEW_DB"
[ -z "${HOST:-}" ] || set -- "$@" --db_host "$HOST"
[ -z "${PORT:-}" ] || set -- "$@" --db_port "$PORT"
[ -z "${USER:-}" ] || set -- "$@" --db_user "$USER"
[ -z "${PASSWORD:-}" ] || set -- "$@" --db_password "$PASSWORD"
exec "$@"
"""


class JobFailed(Exception):
    """The job cannot continue. The message is what the admin reads on the job page."""


# --- bookkeeping --------------------------------------------------------------------------


def now():
    return timezone.now()


def plan(job, names):
    job.steps = [{"name": name, "status": "todo", "at": ""} for name in names]
    job.save(update_fields=["steps"])


def mark(job, index, status):
    job.steps[index].update(status=status, at=now().isoformat(timespec="seconds"))
    job.save(update_fields=["steps"])


def note(job, text):
    """Append a timestamped line to the job's log, keeping only the last LOG_LIMIT characters."""
    stamp = timezone.localtime().strftime("%H:%M:%S")
    job.log = (job.log + f"{stamp} {text.rstrip()}\n")[-LOG_LIMIT:]
    job.save(update_fields=["log"])


def finish(job, status, summary=""):
    job.status = status
    job.finished_at = now()
    if summary:
        job.summary = summary[:500]
    job.save(update_fields=["status", "finished_at", "summary"])


def output(code, out, err):
    text = (out + err).decode(errors="replace") if isinstance(out, bytes) else (out + err)
    return text.strip()


# --- locating the instance ------------------------------------------------------------------


def locate(job):
    endpoint = dockerops.find_endpoint(job.endpoint)
    if endpoint is None:
        raise JobFailed(f"Server {job.endpoint} is no longer known (check the panel configuration).")
    server = dockerops.look_at(endpoint, use_cache=False)
    if server.error:
        raise JobFailed(f"Server {job.endpoint} cannot be reached: {server.error}")
    instance = next((i for i in server.instances if i.slug == job.instance_slug), None)
    if instance is None:
        raise JobFailed(f"Stack {job.instance_slug} was not found on {job.endpoint}.")
    if not instance.is_running:
        raise JobFailed(f"Container {instance.container_name} is {instance.status}; start it in Portainer first.")
    return endpoint, instance


# --- backup now -----------------------------------------------------------------------------


def run_backup(job, endpoint, instance):
    plan(job, ["Run odoo-backup", "Done"])
    mark(job, 0, "running")
    api = dockerops.docker_client(endpoint).api
    code, out, err = dockerops.run_detached(
        api,
        instance.container_id,
        ["odoo-backup"],
        timeout=settings.JOB_TIMEOUT,
        poll_interval=settings.JOB_POLL_INTERVAL,
    )
    text = output(code, out, err)
    note(job, text or "(no output)")
    if code != 0:
        mark(job, 0, "failed")
        if RUNNING_BACKUP in text:
            raise JobFailed("A scheduled backup is running. Try again in a few minutes.")
        raise JobFailed(f"odoo-backup exited with code {code}. See the log below.")
    mark(job, 0, "done")
    mark(job, 1, "done")
    job.summary = "Backup finished."


# --- restore --------------------------------------------------------------------------------


def run_restore(job, endpoint, instance):
    db, zip_name = job.target_db, job.filename
    root = instance.config.get("FILESTORE_ROOT", "/odoo-data/filestore")
    if not root.startswith("/odoo-data/"):
        raise JobFailed(f"FILESTORE_ROOT ({root}) is outside /odoo-data, so the restore is refused.")
    client = dockerops.docker_client(endpoint)
    api = client.api
    odoo = None
    if job.neutralize:
        # Looked for first, so a missing Odoo container stops the job before anything has been created.
        try:
            odoo = dockerops.find_odoo_container(api, instance)
        except dockerops.OdooNotFound as e:
            raise JobFailed(f"Neutralize was requested, but {e} Nothing was changed.") from e
    names = ["Check filestore folder", "Create database and load dump.sql", "Copy filestore"]
    plan(job, [*names, *(["Neutralize"] if odoo else []), "Done"])
    env = {"NEW_DB": db, "ZIP": zip_name, "FILESTORE_ROOT": root}

    mark(job, 0, "running")
    code, text = dockerops.run_helper(
        client, instance, FILESTORE_SCRIPT, {**env, "MODE": "check"}, f"obp-check-{job.pk}", odoo_data_mode="ro"
    )
    note(job, f"filestore check: code {code} {text}".strip())
    if code != 0:
        mark(job, 0, "failed")
        if code == EXISTS:
            raise JobFailed(f"The filestore folder for {db} already exists. Nothing was changed.")
        raise JobFailed(f"The filestore folder check failed (code {code}): {text[:200]}")
    mark(job, 0, "done")

    mark(job, 1, "running")
    code, out, err = dockerops.run_detached(
        api,
        instance.container_id,
        ["sh", "-c", DATABASE_SCRIPT],
        environment=env,
        timeout=settings.JOB_TIMEOUT,
        poll_interval=settings.JOB_POLL_INTERVAL,
    )
    text = output(code, out, err)
    note(job, text or "(no output)")
    if code != 0:
        mark(job, 1, "failed")
        if code == EXISTS:
            raise JobFailed(f"Database {db} already exists. Nothing was changed.")
        if code == LOCKED:
            raise JobFailed("A backup is running, so the restore did not start. Nothing was changed.")
        if code == GONE:
            raise JobFailed(
                f"{zip_name} is no longer in /backups (retention may have removed it). Nothing was changed."
            )
        if "db-created" in text:
            raise JobFailed(
                f"Database {db} was created, but dump.sql failed to load (code {code}). Nothing is deleted "
                "automatically: inspect it and drop it yourself if it is not needed."
            )
        raise JobFailed(f"Creating the database failed (code {code}). See the log below.")
    mark(job, 1, "done")

    mark(job, 2, "running")
    code, text = dockerops.run_helper(
        client, instance, FILESTORE_SCRIPT, {**env, "MODE": "copy"}, f"obp-copy-{job.pk}", odoo_data_mode="rw"
    )
    note(job, f"filestore copy: code {code} {text}".strip())
    if code != 0:
        mark(job, 2, "failed")
        reason = {EXISTS: "the target folder already exists", GONE: f"{zip_name} is gone"}.get(code, text[:200])
        raise JobFailed(
            f"Database {db} was created and the dump loaded, but the filestore was not copied ({reason}). Nothing is "
            f"deleted automatically: inspect the folders {db} and .restore-{db} in the Odoo data volume."
        )
    mark(job, 2, "done")

    if odoo:
        mark(job, 3, "running")
        code, out, err = dockerops.run_detached(
            api,
            odoo.container_id,
            ["sh", "-c", NEUTRALIZE_SCRIPT],
            environment={"NEW_DB": db},
            timeout=settings.JOB_TIMEOUT,
            poll_interval=settings.JOB_POLL_INTERVAL,
        )
        note(job, f"neutralize in {odoo.name}: code {code}\n{output(code, out, err)}")
        if code != 0:
            mark(job, 3, "failed")
            raise JobFailed(
                f"Database {db} was created and the filestore copied, but neutralize failed (code {code}). This copy is "
                f"NOT neutralized: do not open it yet. Run odoo neutralize -d {db} in container {odoo.name}, or drop "
                "the copy yourself."
            )
        mark(job, 3, "done")
    mark(job, len(job.steps) - 1, "done")
    job.summary = (
        f"Database {db} created and filestore copied"
        + (" and neutralized." if odoo else ".")
        + f" Open Odoo with ?db={db} (or adjust dbfilter)."
        + ("" if odoo else " If this is a copy of production, run odoo neutralize first.")
    )


# --- running ----------------------------------------------------------------------------------

RUNNERS = {Job.Kind.BACKUP: run_backup, Job.Kind.RESTORE: run_restore}


def execute(job):
    """Run a claimed job to the end. Whatever happens, the job ends as done or failed."""
    try:
        endpoint, instance = locate(job)
        RUNNERS[job.kind](job, endpoint, instance)
    except JobFailed as e:
        finish(job, Job.Status.FAILED, str(e))
    except dockerops.DOCKER_ERRORS as e:
        finish(job, Job.Status.FAILED, f"Could not talk to Docker: {dockerops.describe(e)}")
    except Exception:
        log.exception("job %s crashed", job.pk)
        finish(job, Job.Status.FAILED, "Internal error in the worker. See the worker log.")
    else:
        finish(job, Job.Status.DONE)


def claim_next():
    """The oldest waiting job, switched to running; a conditional update keeps two workers from
    taking the same one."""
    waiting = Job.objects.filter(status=Job.Status.QUEUED, kind__in=list(RUNNERS)).order_by("created_at", "id")
    for job in waiting[:5]:
        if Job.objects.filter(pk=job.pk, status=Job.Status.QUEUED).update(status=Job.Status.RUNNING, started_at=now()):
            return Job.objects.get(pk=job.pk)
    return None


def run_next():
    job = claim_next()
    if job:
        execute(job)
    return job


def recover_orphans():
    """At worker start: a backup or restore still marked running was cut off by a restart. A download
    marked running belongs to the web process, so only very old ones (long gone) are closed."""
    reason = "Stopped: the worker or the panel restarted while the job was running."
    cut_off = Job.objects.filter(status=Job.Status.RUNNING, kind__in=(Job.Kind.BACKUP, Job.Kind.RESTORE)).update(
        status=Job.Status.FAILED, summary=reason, finished_at=now()
    )
    stale = Job.objects.filter(
        status=Job.Status.RUNNING, kind=Job.Kind.DOWNLOAD, started_at__lt=now() - timedelta(hours=6)
    ).update(status=Job.Status.FAILED, summary=reason, finished_at=now())
    return cut_off + stale


def sweep_temp_dirs():
    """At worker start: remove command leftovers in the backup containers. Never raises."""
    removed = 0
    try:
        servers = dockerops.discover_all(use_cache=False)
    except dockerops.DOCKER_ERRORS as e:
        log.warning("temp sweep skipped: %s", dockerops.describe(e))
        return removed
    for server in servers:
        endpoint = dockerops.find_endpoint(server.name)
        for instance in server.instances:
            if not instance.is_running or endpoint is None:
                continue
            try:
                dockerops.sweep_temp(dockerops.docker_client(endpoint).api, instance)
                removed += 1
            except dockerops.DOCKER_ERRORS as e:
                log.warning("temp sweep failed for %s/%s: %s", server.name, instance.slug, dockerops.describe(e))
    return removed
