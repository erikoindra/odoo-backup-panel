"""All access to Docker lives here (see CLAUDE.md): endpoints, discovery of the backup
containers, and running commands in them. Contract with odoo-backup-service: docs/contract.md.

Secrets: the Portainer API key is only ever sent as a header, and a container's full
environment (it holds PGPASSWORD) is read once in `instance_from_inspect` and reduced to an
allowlist; nothing else is kept, logged or shown.
"""

import io
import logging
import re
import secrets
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import docker
import requests
from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

TIMEOUT = 20  # seconds for any single request to Docker or Portainer
DOWNLOAD_CHUNK = 1024 * 1024  # bytes per piece of a streamed download
SERVICE_LABEL = "com.docker.compose.service=odoo-backup"
PROJECT_LABEL = "com.docker.compose.project"
DOCKER_ENVIRONMENT_TYPES = frozenset({1, 2, 4})  # Portainer: Docker, Agent, Edge Agent
NAME = re.compile(r"[A-Za-z0-9._-]+")
ZIP_NAME = re.compile(r"[A-Za-z0-9._-]+\.zip")

# The only container variables the panel keeps (docs/contract.md). Never PGPASSWORD*.
ENV_ALLOWLIST = (
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "DB_NAME",
    "BACKUP_PREFIX",
    "RETENTION_DAYS",
    "CRON_SCHEDULE",
    "TZ",
    "FILESTORE_ROOT",
    "ODOO_BACKUP_PG_MAJOR",
)

# Runs inside the backup container as: sh -c WRAPPER sh <argv...>. Output goes to files because
# Cloudflare (and most proxies) drop the `Upgrade: tcp` an attached exec needs (docs/decisions.md D7).
DETACHED_WRAPPER = 'd="/tmp/$JOB"; mkdir "$d" && "$@" >"$d/out" 2>"$d/err"; rc=$?; exit $rc'
LIST_BACKUPS_SCRIPT = "stat -c '%n|%s|%Y' /backups/*.zip 2>/dev/null || true"


class DockerOpsError(Exception):
    """A problem worth showing to the admin as is."""


class ContainerNotRunning(DockerOpsError):
    pass


class BackupNotFound(DockerOpsError):
    """The file is not (or no longer) in /backups, e.g. retention removed it."""


# Everything a call to Docker or Portainer can fail with. Views turn these into a message on the
# page; anything else is a bug and should stay loud.
DOCKER_ERRORS = (
    DockerOpsError,
    docker.errors.DockerException,
    requests.RequestException,
    TimeoutError,
    KeyError,
    ValueError,
)


def describe(error):
    """One readable line for an exception. A proxy such as Cloudflare answers with a whole HTML page."""
    if isinstance(error, requests.HTTPError) and error.response is not None:
        if error.response.status_code == 401:
            return "Portainer rejected the API key (401). Check PORTAINER_API_KEY."
        if error.response.status_code == 403:
            return "Access denied (403)."
    text = re.sub(r"<[^>]+>", " ", f"{type(error).__name__}: {error}")
    return " ".join(text.split())[:300]


# --- data ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One Docker API the panel talks to. `error` is set when it is known to be unusable."""

    name: str
    kind: str  # "portainer", "docker" or "config"
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    error: str = ""


@dataclass(frozen=True, slots=True)
class Instance:
    endpoint: str
    slug: str
    container_id: str
    container_name: str
    image: str
    status: str  # Docker state: running, exited, ...
    backup_volume: str  # what to bind to /backups in a helper container (volume name, or host path)
    odoo_data_volume: str  # same, for /odoo-data
    config: dict  # the allowlisted container variables

    @property
    def is_running(self):
        return self.status == "running"

    @property
    def status_label(self):
        return {"running": "Running", "exited": "Container stopped"}.get(self.status, self.status.capitalize())

    @property
    def status_class(self):
        return "status-ok" if self.is_running else "status-warn"

    @property
    def database(self):
        return self.config.get("DB_NAME", "")

    @property
    def pg_major(self):
        return self.config.get("ODOO_BACKUP_PG_MAJOR", "")

    @property
    def schedule(self):
        cron, zone = self.config.get("CRON_SCHEDULE", ""), self.config.get("TZ", "")
        return f"{cron} · {zone}" if cron and zone else cron

    @property
    def retention_days(self):
        days = self.config.get("RETENTION_DAYS", "")
        return int(days) if days.isdigit() else None

    @property
    def retention_label(self):
        days = self.retention_days
        if days is None:
            return ""
        return "No automatic deletion" if days == 0 else f"Retention {days} days"


@dataclass(frozen=True, slots=True)
class Server:
    """Result of looking at one endpoint. `error`: could not be read. `warning`: read, with a caveat."""

    name: str
    kind: str
    instances: list
    error: str = ""
    warning: str = ""

    @property
    def source_label(self):
        return {"portainer": "Portainer", "docker": "Docker"}.get(self.kind, "Configuration")


def suggest_db_name(filename):
    """`<db>_restore_<YYYYMMDD>` from `<db>_<YYYYMMDD>_<HHMMSS>.zip`, cleaned up to a valid database name."""
    match = re.fullmatch(r"(.*)_(\d{8})_\d{6}\.zip", filename)
    if match:
        base, day = match.groups()
    else:
        base, day = filename.removesuffix(".zip"), timezone.localdate().strftime("%Y%m%d")
    suffix = f"_restore_{day}"
    return re.sub(r"[^a-z0-9_]", "_", base.lower())[: 63 - len(suffix)] + suffix


@dataclass(frozen=True, slots=True)
class Backup:
    name: str
    size: int
    mtime: datetime

    @property
    def suggested_db(self):
        return suggest_db_name(self.name)


@dataclass(frozen=True, slots=True)
class BackupListing:
    backups: list
    read_at: datetime

    @property
    def latest(self):
        return self.backups[0] if self.backups else None

    @property
    def total_size(self):
        return sum(b.size for b in self.backups)


# --- small TTL cache -----------------------------------------------------------------------
# Through Portainer and Cloudflare a single request costs 0.1-1.5 s (6 s on a cold connection), so
# these are kept: Portainer's environment list (60 s), one client per endpoint (10 min: its
# connections stay open and the API version is not read again), and the result of looking at a
# server (15 s, only when it worked, so "Retry" really retries).

_cache = {}
_cache_lock = threading.Lock()


def _cached(key, ttl, loader, keep=None):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = loader()  # an exception is never cached
    if keep is None or keep(value):
        with _cache_lock:
            _cache[key] = (now + ttl, value)
    return value


def clear_cache():
    with _cache_lock:
        _cache.clear()


# --- endpoints ----------------------------------------------------------------------------


def _http_get_json(url, api_key):
    response = requests.get(url, headers={"X-API-Key": api_key}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def docker_base_url(portainer_url, environment_id):
    """Docker API of one Portainer environment. The Docker SDK insists on an explicit port."""
    parts = urlsplit(portainer_url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    return f"{parts.scheme}://{host}:{port}{parts.path.rstrip('/')}/api/endpoints/{environment_id}/docker"


def is_configured():
    return bool(settings.PORTAINER_URL or settings.DOCKER_ENDPOINTS)


def _portainer_endpoints():
    if not settings.PORTAINER_API_KEY:
        return [Endpoint("portainer", "config", error="PORTAINER_URL is set but PORTAINER_API_KEY is not.")]
    url, key = settings.PORTAINER_URL, settings.PORTAINER_API_KEY
    try:
        environments = _cached(("portainer", url), 60, lambda: _http_get_json(f"{url}/api/endpoints", key))
    except (requests.RequestException, ValueError) as e:
        return [Endpoint("portainer", "portainer", error=f"Portainer could not be read: {describe(e)}")]
    allowed = set(settings.PORTAINER_ENVIRONMENTS)
    endpoints = []
    for environment in environments:
        name = str(environment.get("Name", ""))
        if environment.get("Type") not in DOCKER_ENVIRONMENT_TYPES or (allowed and name not in allowed):
            continue
        if not NAME.fullmatch(name):
            endpoints.append(
                Endpoint(
                    name,
                    "portainer",
                    error="The environment name cannot be used in a panel URL. Rename it in Portainer "
                    "(letters, digits, dots, underscores, dashes).",
                )
            )
        elif environment.get("Status") != 1:
            endpoints.append(Endpoint(name, "portainer", error="Portainer reports this environment as down."))
        else:
            endpoints.append(Endpoint(name, "portainer", docker_base_url(url, environment["Id"]), key))
    return endpoints


def _docker_endpoints():
    endpoints = []
    for item in settings.DOCKER_ENDPOINTS:
        name, separator, url = item.partition("=")
        name, url = name.strip(), url.strip()
        if not separator or not url or not NAME.fullmatch(name):
            endpoints.append(Endpoint(name or "docker", "config", error="DOCKER_ENDPOINTS must be name=url."))
        else:
            endpoints.append(Endpoint(name, "docker", url))
    return endpoints


def load_endpoints():
    """Every endpoint the panel should look at, in a stable order. A name used twice is dropped
    and reported, never guessed."""
    endpoints = _docker_endpoints()
    if settings.PORTAINER_URL:
        endpoints += _portainer_endpoints()
    counts = {}
    for endpoint in endpoints:
        counts[endpoint.name] = counts.get(endpoint.name, 0) + 1
    clashes = sorted(name for name, count in counts.items() if count > 1)
    endpoints = [e for e in endpoints if e.name not in clashes]
    if clashes:
        endpoints.append(
            Endpoint("config", "config", error=f"Endpoint name used more than once: {', '.join(clashes)}.")
        )
    return endpoints


def docker_client(endpoint):
    """DockerClient for one endpoint, reused for 10 minutes."""
    key = ("client", endpoint.kind, endpoint.base_url, endpoint.api_key)
    return _cached(key, 600, lambda: _new_docker_client(endpoint))


def _new_docker_client(endpoint):
    """For Portainer the API version is read first with our own request, so that the SDK (which
    would otherwise start with an unauthenticated GET /version) never talks without the key."""
    if endpoint.kind == "portainer":
        version = _http_get_json(f"{endpoint.base_url}/version", endpoint.api_key)["ApiVersion"]
        client = docker.DockerClient(base_url=endpoint.base_url, version=version, timeout=TIMEOUT)
        client.api.headers["X-API-Key"] = endpoint.api_key
        return client
    return docker.DockerClient(base_url=endpoint.base_url, version="auto", timeout=TIMEOUT)


# --- discovery ----------------------------------------------------------------------------


def instance_from_inspect(endpoint_name, info):
    """Instance for an inspected container, or None when it is not a backup-service image."""
    config = info.get("Config") or {}
    image = config.get("Image", "")
    if not image.startswith(settings.BACKUP_IMAGE_PREFIX):
        return None
    container_name = info["Name"].lstrip("/")
    mounts = {m["Destination"]: (m.get("Name") or m.get("Source", "")) for m in info.get("Mounts") or []}
    variables = dict(item.partition("=")[::2] for item in config.get("Env") or [])
    return Instance(
        endpoint=endpoint_name,
        slug=(config.get("Labels") or {}).get(PROJECT_LABEL) or container_name,
        container_id=info["Id"],
        container_name=container_name,
        image=image,
        status=info["State"]["Status"],
        backup_volume=mounts.get("/backups", ""),
        odoo_data_volume=mounts.get("/odoo-data", ""),
        config={key: variables[key] for key in ENV_ALLOWLIST if key in variables},
    )


def discover_instances(endpoint):
    api = docker_client(endpoint).api
    entries = api.containers(all=True, filters={"label": [SERVICE_LABEL]})
    with ThreadPoolExecutor(max_workers=min(4, len(entries) or 1)) as pool:
        inspected = pool.map(lambda entry: api.inspect_container(entry["Id"]), entries)
        return [i for i in (instance_from_inspect(endpoint.name, info) for info in inspected) if i]


def forget_client(endpoint):
    """Drop the cached client, so the next attempt starts from a fresh connection."""
    with _cache_lock:
        _cache.pop(("client", endpoint.kind, endpoint.base_url, endpoint.api_key), None)


def discover_server(endpoint):
    if endpoint.error:
        return Server(endpoint.name, endpoint.kind, [], error=endpoint.error)
    try:
        instances = discover_instances(endpoint)
    except DOCKER_ERRORS as e:
        forget_client(endpoint)
        return Server(endpoint.name, endpoint.kind, [], error=describe(e))
    by_slug = {}
    for instance in instances:
        by_slug.setdefault(instance.slug, []).append(instance)
    duplicated = {slug: found for slug, found in by_slug.items() if len(found) > 1}
    warning = ""
    if duplicated:
        details = "; ".join(
            f"{slug} ({', '.join(i.container_name for i in found)})" for slug, found in duplicated.items()
        )
        warning = f"Duplicate stack names, hidden: {details}."
    kept = sorted((i for i in instances if i.slug not in duplicated), key=lambda i: i.slug)
    return Server(endpoint.name, endpoint.kind, kept, warning=warning)


def look_at(endpoint, use_cache=True):
    """Server for one endpoint. A server that was read fine is remembered for 15 seconds; one
    with an error never is."""
    if not use_cache:
        return discover_server(endpoint)
    return _cached(("server", endpoint), 15, lambda: discover_server(endpoint), keep=lambda s: not s.error)


def find_endpoint(name):
    return next((e for e in load_endpoints() if e.name == name), None)


def discover_all(use_cache=True):
    """Look at every endpoint at once. One endpoint failing never hides the others."""
    endpoints = load_endpoints()
    with ThreadPoolExecutor(max_workers=min(8, len(endpoints) or 1)) as pool:
        return list(pool.map(lambda endpoint: look_at(endpoint, use_cache), endpoints))


# --- running commands in a container ------------------------------------------------------


def wait_for_exec(api, exec_id, timeout, poll_interval):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = api.exec_inspect(exec_id)
        if not info["Running"]:
            return info["ExitCode"]
        time.sleep(poll_interval)
    raise TimeoutError(f"command still running after {timeout} seconds")


def read_job_files(api, container_id, job):
    stream, _ = api.get_archive(container_id, f"/tmp/{job}")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream)), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isfile():
                files[Path(member.name).name] = archive.extractfile(member).read()
    return files.get("out", b""), files.get("err", b"")


def run_detached(api, container_id, argv, environment=None, timeout=60, poll_interval=0.4):
    """Run a command in a container WITHOUT attaching to it; return (exit code, stdout, stderr).

    Only plain requests are used: exec create, exec start with Detach, exec inspect (polled) and
    get_archive for the output files. Arguments go through "$@" and the environment, never into
    a shell string. The temp directory in the container is removed at the end (the removal is
    sent but not waited for: it costs a request through Portainer, and nothing depends on it).
    """
    job = f"obp-{secrets.token_hex(6)}"
    command = ["sh", "-c", DETACHED_WRAPPER, "sh", *argv]
    try:
        exec_id = api.exec_create(container_id, command, environment={**(environment or {}), "JOB": job})["Id"]
        api.exec_start(exec_id, detach=True)
        code = wait_for_exec(api, exec_id, timeout, poll_interval)
        out, err = read_job_files(api, container_id, job)
        return code, out, err
    finally:
        try:
            cleanup = api.exec_create(container_id, ["rm", "-rf", f"/tmp/{job}"])["Id"]
            api.exec_start(cleanup, detach=True)
        except (docker.errors.DockerException, requests.RequestException) as e:
            log.warning("could not remove /tmp/%s in container %s: %s", job, container_id[:12], describe(e))


def list_backups(api, instance):
    """Zip files in /backups, newest first. Needs a running container."""
    if not instance.is_running:
        raise ContainerNotRunning(f"Container {instance.container_name} is {instance.status}.")
    code, out, err = run_detached(api, instance.container_id, ["sh", "-c", LIST_BACKUPS_SCRIPT])
    if code != 0:
        raise DockerOpsError(f"Could not read /backups (code {code}): {err.decode(errors='replace')[:200]}")
    backups = []
    for line in out.decode(errors="replace").splitlines():
        parts = line.rsplit("|", 2)  # "<path>|<size>|<mtime>"
        if len(parts) != 3:
            continue
        path, size, mtime = parts
        name = PurePosixPath(path).name
        if ZIP_NAME.fullmatch(name) and size.isdigit() and mtime.isdigit():
            backups.append(Backup(name, int(size), datetime.fromtimestamp(int(mtime), UTC)))
    return sorted(backups, key=lambda b: b.mtime, reverse=True)


def forget_listing(endpoint_name, slug):
    """Drop the remembered /backups listing, e.g. after a backup or restore finished."""
    with _cache_lock:
        _cache.pop(("backups", endpoint_name, slug), None)


def list_backups_cached(endpoint, instance, use_cache=True):
    """BackupListing for an instance. Reading /backups takes a few seconds through Portainer, so a
    listing that worked is kept for 15 seconds; use_cache=False reads again and keeps that."""
    key = ("backups", endpoint.name, instance.slug)
    if not use_cache:
        forget_listing(endpoint.name, instance.slug)
    return _cached(
        key,
        15,
        lambda: BackupListing(list_backups(docker_client(endpoint).api, instance), datetime.now(UTC)),
    )


# --- downloading a backup ------------------------------------------------------------------


class IterStream(io.RawIOBase):
    """File-like view of a byte-chunk iterator, so tarfile can read an archive as a stream."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        # A memoryview, because slicing bytes would copy the rest of a 2 MB chunk on every small read.
        self._buf = memoryview(b"")

    def readable(self):
        return True

    def readinto(self, target):
        while not self._buf:
            try:
                self._buf = memoryview(next(self._chunks))
            except StopIteration:
                return 0
        n = min(len(target), len(self._buf))
        target[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n

    def close(self):
        close = getattr(self._chunks, "close", None)
        if close:
            close()  # stops the HTTP stream early when the reader gives up
        super().close()


@dataclass(slots=True)
class BackupDownload:
    """A zip being streamed out of a container. `size` is known and checked before any byte is sent."""

    name: str
    size: int
    _archive: tarfile.TarFile
    _member: tarfile.TarInfo
    _reader: io.BufferedReader

    def chunks(self):
        """The file in pieces of DOWNLOAD_CHUNK bytes. A stream that ends early raises (tarfile reports
        the missing data), so a cut-off transfer can never look complete."""
        sent = 0
        try:
            source = self._archive.extractfile(self._member)
            while chunk := source.read(DOWNLOAD_CHUNK):
                sent += len(chunk)
                yield chunk
        except (tarfile.TarError, EOFError) as e:
            raise DockerOpsError(f"Stream from the container broke off after {sent} bytes: {e}") from e
        finally:
            self.close()

    def close(self):
        """tarfile does not close a file object it was given, so the HTTP stream is closed here."""
        self._archive.close()
        self._reader.close()


def open_backup_stream(api, instance, filename):
    """Start reading /backups/<filename> from the container, without ever holding it all in memory
    or writing it to disk. Everything that can go wrong before the first byte (file gone, unexpected
    archive, size missing) is raised here, so the caller can still answer with a proper error page."""
    if not ZIP_NAME.fullmatch(filename):
        raise BackupNotFound("Invalid file name.")
    if not instance.is_running:
        raise ContainerNotRunning(f"Container {instance.container_name} is {instance.status}.")
    try:
        raw, stat = api.get_archive(instance.container_id, f"/backups/{filename}")
    except docker.errors.NotFound as e:
        raise BackupNotFound(f"{filename} is no longer in /backups (retention may have removed it).") from e
    reader = io.BufferedReader(IterStream(raw))
    archive = None
    try:
        size = stat.get("size") if isinstance(stat, dict) else None
        if not isinstance(size, int) or size < 0:
            raise DockerOpsError("Docker did not report the file size, so the download is refused.")
        # Stays open on purpose: BackupDownload.chunks() reads it and BackupDownload.close() closes it.
        archive = tarfile.open(fileobj=reader, mode="r|")  # noqa: SIM115
        member = archive.next()
        if member is None or not member.isfile() or member.size != size:
            raise DockerOpsError("The archive from the container is not what was expected.")
    except (tarfile.TarError, EOFError) as e:
        reader.close()
        raise DockerOpsError(f"The archive from the container cannot be read: {e}") from e
    except Exception:
        if archive is not None:
            archive.close()
        reader.close()
        raise
    return BackupDownload(filename, size, archive, member, reader)


# --- helper containers and housekeeping ------------------------------------------------------


def _text(value):
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return value or ""


def run_helper(client, instance, script, environment, name, odoo_data_mode="rw"):
    """Run `sh -c script` once in a throw-away container made from the backup image, with the backup
    volume at /b (read-only) and the Odoo data volume at /d. No network, and removed afterwards.
    Returns (exit code, output); output is stdout on success and stderr on failure."""
    if not instance.backup_volume or not instance.odoo_data_volume:
        raise DockerOpsError("This backup container has no usable /backups and /odoo-data volumes.")
    volumes = {
        instance.backup_volume: {"bind": "/b", "mode": "ro"},
        instance.odoo_data_volume: {"bind": "/d", "mode": odoo_data_mode},
    }
    try:
        out = client.containers.run(
            instance.image,
            command=[script],  # one element: the SDK would split a plain string into words
            entrypoint=["sh", "-c"],  # so the image's own entrypoint (crond) does not start
            user="0",
            network_mode="none",
            volumes=volumes,
            # The image declares VOLUME /backups. Without a mount there, every helper would leave an
            # anonymous volume behind (the SDK removes the container but not that volume).
            tmpfs={"/backups": "size=1m"},
            environment=environment,
            name=name,
            labels={"odoo-backup-panel": "helper"},
            remove=True,
            detach=False,
        )
    except docker.errors.ContainerError as e:
        return e.exit_status, _text(e.stderr)
    return 0, _text(out)


def sweep_temp(api, instance):
    """Remove /tmp/obp-* left by a command that was cut off (older than an hour). Best effort."""
    run_detached(
        api,
        instance.container_id,
        ["sh", "-c", "find /tmp -maxdepth 1 -name 'obp-*' -mmin +60 -exec rm -rf {} +"],
        timeout=30,
    )


# --- the Odoo container of a stack (for `odoo neutralize`) ---------------------------------------

ODOO_DATA_DIR = "/var/lib/odoo"


@dataclass(frozen=True, slots=True)
class OdooContainer:
    container_id: str
    name: str
    image: str


class OdooNotFound(DockerOpsError):
    pass


def find_odoo_container(api, instance):
    """The running Odoo container that uses the same data volume as this backup stack.

    Found by the named volume mounted at /var/lib/odoo, not by name, so it works for any naming. It must be
    exactly one running container other than the backup container itself. The inspect result carries that
    container's whole environment (with its database password); only name, image, state and mounts are read.
    """
    volume = instance.odoo_data_volume
    if not volume or volume.startswith("/"):
        raise OdooNotFound("The Odoo data volume is not a named volume, so the Odoo container cannot be looked up.")
    found = []
    for entry in api.containers(all=True, filters={"volume": [volume]}):
        if entry["Id"] == instance.container_id:
            continue
        info = api.inspect_container(entry["Id"])
        mounted = any(
            m.get("Name") == volume and m.get("Destination") == ODOO_DATA_DIR for m in info.get("Mounts") or []
        )
        if mounted:
            found.append(
                OdooContainer(info["Id"], info["Name"].lstrip("/"), (info.get("Config") or {}).get("Image", ""))
            )
            if info["State"]["Status"] != "running":
                raise OdooNotFound(f"Odoo container {found[-1].name} is {info['State']['Status']}.")
    if not found:
        raise OdooNotFound(f"No Odoo container uses volume {volume} at {ODOO_DATA_DIR}.")
    if len(found) > 1:
        raise OdooNotFound("More than one Odoo container uses this volume: " + ", ".join(o.name for o in found) + ".")
    return found[0]
