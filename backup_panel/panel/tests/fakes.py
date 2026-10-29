"""A stand-in for the Docker SDK's low-level API, enough for panel.dockerops."""

import io
import tarfile

import docker

SECRET = "hunter2-very-secret"  # a container's PGPASSWORD: must never appear in anything the panel keeps
IMAGE = "ghcr.io/erikoindra/odoo-backup-service:pg17"
ENV = [
    "PATH=/usr/local/bin:/usr/bin",
    "PGHOST=db",
    "PGUSER=odoo",
    f"PGPASSWORD={SECRET}",
    "PGPASSWORD_FILE=/run/secrets/db",
    "DB_NAME=acme_staging",
    "CRON_SCHEDULE=0 2 * * *",
    "TZ=Asia/Jakarta",
    "RETENTION_DAYS=3",
    "ODOO_BACKUP_PG_MAJOR=17",
]
MOUNTS = [
    {"Type": "volume", "Name": "acme_backups", "Destination": "/backups"},
    {"Type": "volume", "Name": "acme_odoo_data", "Destination": "/odoo-data"},
]


def inspect_result(name="acme-staging-backup-odoo-backup-1", project="acme-staging-backup", **overrides):
    """What `docker inspect` returns for a backup container."""
    labels = {"com.docker.compose.service": "odoo-backup"}
    if project:
        labels["com.docker.compose.project"] = project
    info = {
        "Id": f"id-{name}",
        "Name": f"/{name}",
        "Config": {"Image": IMAGE, "Env": list(ENV), "Labels": labels},
        "State": {"Status": "running"},
        "Mounts": [dict(m) for m in MOUNTS],
    }
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            info[section][field] = value
        else:
            info[key] = value
    return info


def make_instance(slug="acme-staging-backup", status="running", database="acme_staging", endpoint="vps-1", **config):
    """A panel Instance, as dockerops would build it."""
    from panel.dockerops import Instance

    return Instance(
        endpoint=endpoint,
        slug=slug,
        container_id=f"id-{slug}",
        container_name=f"{slug}-odoo-backup-1",
        image=IMAGE,
        status=status,
        backup_volume="acme_backups",
        odoo_data_volume="acme_odoo_data",
        config={"CRON_SCHEDULE": "0 2 * * *", "TZ": "Asia/Jakarta", "ODOO_BACKUP_PG_MAJOR": "17", "RETENTION_DAYS": "3"}
        | ({"DB_NAME": database} if database else {})
        | config,
    )


class FakeContainers:
    """`client.containers`: `run` records its arguments and answers like the SDK, including ContainerError."""

    def __init__(self, helper):
        self.helper = helper or (lambda call: (0, b""))
        self.calls = []

    def run(self, image, command=None, **kwargs):
        call = {"image": image, "command": command, **kwargs}
        self.calls.append(call)
        code, output = self.helper(call)
        if code != 0:
            raise docker.errors.ContainerError(None, code, command, image, output)
        return output


class FakeClient:
    def __init__(self, api, helper=None):
        self.api = api
        self.containers = FakeContainers(helper)


class FakeApi:
    """Containers by name, plus exec / get_archive that run `runner(argv, env) -> (rc, out, err)`."""

    def __init__(self, inspects=(), runner=None):
        self.inspects = {info["Id"]: info for info in inspects}
        self.runner = runner or (lambda argv, env: (0, b"", b""))
        self.filters = []
        self.execs = {}
        self.job_dirs = {}
        self.running_forever = False
        self.inspect_calls = 0
        self.files = {}  # "/backups/x.zip" -> bytes
        self.stat_size = None  # override what the Docker "stat" header says
        self.fail_after = None  # raise in the stream after this many tar bytes
        self.archive_chunk = 700
        self.streams = []

    # discovery
    def containers(self, all=False, filters=None):
        self.filters.append(filters)
        return [{"Id": cid} for cid in self.inspects]

    def inspect_container(self, container_id):
        return self.inspects[container_id]

    # exec without attach
    def exec_create(self, container_id, cmd, environment=None, **_):
        exec_id = f"exec{len(self.execs) + 1}"
        self.execs[exec_id] = {"cmd": cmd, "env": environment or {}, "rc": None, "container": container_id}
        return {"Id": exec_id}

    def exec_start(self, exec_id, detach=False, **_):
        assert detach is True, "the panel must never attach to an exec"
        spec = self.execs[exec_id]
        cmd = spec["cmd"]
        if cmd[0] == "rm":
            self.job_dirs.pop(cmd[-1].rsplit("/", 1)[-1], None)
            spec["rc"] = 0
        else:
            assert cmd[:2] == ["sh", "-c"] and cmd[3] == "sh", cmd
            job = spec["env"]["JOB"]
            self.job_dirs[job] = (b"", b"")  # `mkdir` happens before the command runs
            code, out, err = self.runner(cmd[4:], spec["env"])
            self.job_dirs[job] = (out, err)
            spec["rc"] = code

    def exec_inspect(self, exec_id):
        self.inspect_calls += 1
        if self.running_forever:
            return {"Running": True, "ExitCode": None}
        return {"Running": False, "ExitCode": self.execs[exec_id]["rc"]}

    def get_archive(self, container_id, path):
        if path in self.files:
            return self._file_archive(path)
        job = path.rsplit("/", 1)[-1]
        if job not in self.job_dirs:
            raise docker.errors.NotFound("no such path")
        out, err = self.job_dirs[job]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            directory = tarfile.TarInfo(job)
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
            for name, data in (("out", out), ("err", err)):
                member = tarfile.TarInfo(f"{job}/{name}")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return iter([buffer.getvalue()]), {"size": len(out) + len(err)}

    def _file_archive(self, path):
        data = self.files[path]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            member = tarfile.TarInfo(path.rsplit("/", 1)[-1])
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        tar = buffer.getvalue()
        stream = RecordingStream(tar, self.archive_chunk, self.fail_after)
        self.streams.append(stream)
        size = len(data) if self.stat_size is None else self.stat_size
        return stream, ({"name": path.rsplit("/", 1)[-1], "size": size} if size is not False else None)


class RecordingStream:
    """The byte stream get_archive returns: an iterator in small pieces that notes whether it was closed."""

    def __init__(self, data, chunk, fail_after=None):
        self.closed = False
        self.sent = 0
        self._pieces = (data[i : i + chunk] for i in range(0, len(data), chunk))
        self._fail_after = fail_after

    def __iter__(self):
        return self

    def __next__(self):
        if self._fail_after is not None and self.sent >= self._fail_after:
            raise ConnectionError("koneksi terputus")
        piece = next(self._pieces)
        self.sent += len(piece)
        return piece

    def close(self):
        self.closed = True


def odoo_inspect(name="acme-odoo-1", volume="acme_odoo_data", destination="/var/lib/odoo", status="running"):
    """What `docker inspect` returns for an Odoo container. Its environment is the dangerous part (it holds the
    database password), so it is included here to prove the panel never keeps it."""
    return {
        "Id": f"id-{name}",
        "Name": f"/{name}",
        "Config": {"Image": "odoo:19.0", "Env": [f"PASSWORD={SECRET}", "HOST=db", "USER=odoo"]},
        "State": {"Status": status},
        "Mounts": [{"Type": "volume", "Name": volume, "Destination": destination}],
    }
