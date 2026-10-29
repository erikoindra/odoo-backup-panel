#!/usr/bin/env python3
"""Step 2 spike (docs/spec.md section 10): can the panel drive Docker through Portainer?

Checks S1-S4 from docs/assumptions.md against a real Portainer, for every environment that
runs an odoo-backup-service container:

  S1   list environments, then list the backup containers (label filter) on each
  S3   run commands in the container WITHOUT attaching (detached exec, output through files).
       Portainer behind Cloudflare cannot do the attached kind (S3a, informational only)
  S2   stream a zip out of /backups (get_archive) and compare size + sha256
  S4   create / start / wait / logs / remove a throw-away helper container with read-only mounts

It changes nothing that lasts: the writes are the helper container (removed at the end, read-only
volumes) and short-lived directories under /tmp inside the backup container (removed after each
command). It never prints the API key, container environments or file contents.

    python scripts/spike_portainer.py [--env-file PATH] [--endpoint NAME] [--insecure]

The env file lives outside the repo (chmod 600). Default: ~/.config/odoo-backup-panel/portainer.env

    PORTAINER_URL=https://portainer.example.com
    PORTAINER_API_KEY=ptr_...
"""

import argparse
import hashlib
import io
import os
import re
import secrets
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import docker
import requests
import urllib3

DEFAULT_ENV_FILE = Path.home() / ".config" / "odoo-backup-panel" / "portainer.env"
LABEL = "com.docker.compose.service=odoo-backup"
IMAGE_PREFIX = "ghcr.io/erikoindra/odoo-backup-service"
DOCKER_ENVIRONMENT_TYPES = {1, 2, 4}  # Docker, Agent, Edge Agent (3 = Azure, 5-7 = Kubernetes)
TIMEOUT = 20
CHUNK = 1024 * 1024


class IterStream(io.RawIOBase):
    """File-like view of a byte-chunk generator, so tarfile can read the archive as a stream."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buf = b""

    def readable(self):
        return True

    def readinto(self, target):
        while not self._buf:
            try:
                self._buf = next(self._chunks)
            except StopIteration:
                return 0
        n = min(len(target), len(self._buf))
        target[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


def describe(error):
    """One readable line for an exception. A proxy such as Cloudflare answers with a whole HTML page."""
    text = re.sub(r"<[^>]+>", " ", f"{type(error).__name__}: {error}")
    return " ".join(text.split())[:300]


def load_env(path):
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    values.update({k: os.environ[k] for k in ("PORTAINER_URL", "PORTAINER_API_KEY") if k in os.environ})
    return values


def docker_base_url(portainer_url, endpoint_id):
    """Docker API of one environment, through Portainer. The Docker SDK insists on a port."""
    parts = urlsplit(portainer_url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
    return f"{parts.scheme}://{host}:{port}{parts.path.rstrip('/')}/api/endpoints/{endpoint_id}/docker"


class Report:
    def __init__(self):
        self.rows = []  # (scope, check, status, note)

    def add(self, scope, check, status, note=""):
        self.rows.append((scope, check, status, note))
        print(f"  [{check}] {status:<4} {note}")

    @property
    def failed(self):
        return any(row[2] == "FAIL" for row in self.rows)


# Runs inside the backup container as: sh -c WRAPPER sh <argv...>. The output goes to files
# because Cloudflare (and most proxies) drop the `Upgrade: tcp` that an attached exec needs.
DETACHED_WRAPPER = 'd="/tmp/$JOB"; mkdir "$d" && "$@" >"$d/out" 2>"$d/err"; rc=$?; exit $rc'


def wait_for_exec(api, exec_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = api.exec_inspect(exec_id)
        if not info["Running"]:
            return info["ExitCode"]
        time.sleep(0.4)
    raise TimeoutError(f"exec still running after {timeout}s")


def read_job_files(api, container_id, job):
    stream, _ = api.get_archive(container_id, f"/tmp/{job}")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream)), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isfile():
                files[Path(member.name).name] = archive.extractfile(member).read()
    return files.get("out", b""), files.get("err", b"")


def run_detached(api, container_id, argv, environment=None, timeout=60):
    """Run a command in a container WITHOUT attaching to it; return (exit code, stdout, stderr).

    Only plain requests are used: exec create, exec start with Detach, exec inspect (polled)
    and get_archive for the output files. The temp directory is removed at the end.
    """
    job = f"obp-{secrets.token_hex(6)}"
    command = ["sh", "-c", DETACHED_WRAPPER, "sh", *argv]
    try:
        exec_id = api.exec_create(container_id, command, environment={**(environment or {}), "JOB": job})["Id"]
        api.exec_start(exec_id, detach=True)
        code = wait_for_exec(api, exec_id, timeout)
        out, err = read_job_files(api, container_id, job)
        return code, out, err
    finally:
        try:
            cleanup = api.exec_create(container_id, ["rm", "-rf", f"/tmp/{job}"])["Id"]
            api.exec_start(cleanup, detach=True)
            wait_for_exec(api, cleanup, 20)
        except Exception as e:
            print(f"  WARN could not remove /tmp/{job} in the container: {describe(e)}")


def run_attached(api, container_id, command):
    exec_id = api.exec_create(container_id, command, stdout=True, stderr=True)["Id"]
    stdout, stderr = api.exec_start(exec_id, demux=True)
    return api.exec_inspect(exec_id)["ExitCode"], stdout or b"", stderr or b""


def check_attach(api, container, report, scope):
    """S3a, informational: the normal attached exec. The panel does not need it."""
    try:
        got = run_attached(api, container["Id"], ["sh", "-c", "echo out; echo err >&2; exit 7"])
    except Exception as e:
        report.add(scope, "S3a", "INFO", f"attached exec not available here: {describe(e)}")
        return
    state = "works here" if got == (7, b"out\n", b"err\n") else f"answered oddly: {got!r}"
    report.add(scope, "S3a", "INFO", f"attached exec {state}")


def check_detached(api, container, report, scope):
    """S3. Returns True when exec without attach works; S2 builds on it."""
    cid = container["Id"]
    try:
        script = 'echo out; echo err >&2; printf %s "$X"; exit 7'
        code, out, err = run_detached(api, cid, ["sh", "-c", script], {"X": "a b'c$d"})
        small = (code, out, err) == (7, b"out\na b'c$d", b"err\n")
        code2, out2, _ = run_detached(api, cid, ["sh", "-c", "head -c 300000 /dev/zero | tr '\\0' x"])
        large = code2 == 0 and len(out2) == 300000
        # The listing is itself a job, so exactly one /tmp/obp-* directory (its own) may exist.
        _, listing, _ = run_detached(api, cid, ["sh", "-c", "ls -d /tmp/obp-* 2>/dev/null"])
        leftovers = len(listing.split()) - 1
    except Exception as e:  # any transport problem is the answer we are looking for
        report.add(scope, "S3", "FAIL", describe(e))
        return False
    if small and large and leftovers == 0:
        report.add(
            scope, "S3", "PASS", "detached exec: exit code, stdout/stderr, env, 300 KB output, no temp files left"
        )
        return True
    report.add(
        scope,
        "S3",
        "FAIL",
        f"small={small} (got {code}, {out!r}, {err!r}) large={large} ({len(out2)} bytes) leftover dirs={leftovers}",
    )
    return False


def check_archive(api, container, report, scope, exec_ok):
    """S2. Stream the smallest zip out of /backups and compare it with what the container says."""
    cid = container["Id"]
    if not exec_ok:
        report.add(scope, "S2", "SKIP", "needs detached exec (S3) to find a zip")
        return
    try:
        code, out, _ = run_detached(
            api, cid, ["sh", "-c", "stat -c '%s %n' /backups/*.zip 2>/dev/null | sort -n | head -n 1"]
        )
        line = out.decode().strip()
        if code != 0 or not line:
            report.add(scope, "S2", "SKIP", "no zip in /backups yet; run a backup first")
            return
        size_text, _, path = line.partition(" ")
        expected_size = int(size_text)

        stream, stat = api.get_archive(cid, path)
        digest, total = hashlib.sha256(), 0
        with tarfile.open(fileobj=io.BufferedReader(IterStream(stream)), mode="r|") as archive:
            source = archive.extractfile(archive.next())
            while chunk := source.read(CHUNK):
                digest.update(chunk)
                total += len(chunk)

        code, out, _ = run_detached(api, cid, ["sha256sum", path])
        remote = out.decode().split()[0] if code == 0 and out else ""
        stat_size = stat.get("size") if isinstance(stat, dict) else None
    except Exception as e:
        report.add(scope, "S2", "FAIL", describe(e))
        return
    problems = []
    if total != expected_size:
        problems.append(f"streamed {total} bytes, expected {expected_size}")
    if digest.hexdigest() != remote:
        problems.append("sha256 differs from sha256sum inside the container")
    if stat_size != expected_size:
        problems.append(f"X-Docker-Container-Path-Stat size is {stat_size!r} (Content-Length needs it)")
    if problems:
        report.add(scope, "S2", "FAIL", "; ".join(problems))
    else:
        report.add(scope, "S2", "PASS", f"{total} bytes streamed, sha256 and stat size match")


def check_helper(client, container, report, scope):
    """S4. Same flow the restore will use for the filestore, but with read-only mounts."""
    api = client.api
    name = f"obp-spike-{secrets.token_hex(4)}"
    volumes = {}
    for mount in container.get("Mounts", []):
        if mount.get("Type") == "volume" and mount["Destination"] == "/backups":
            volumes[mount["Name"]] = {"bind": "/b", "mode": "ro"}
        if mount.get("Type") == "volume" and mount["Destination"] == "/odoo-data":
            volumes[mount["Name"]] = {"bind": "/d", "mode": "ro"}
    if len(volumes) != 2:
        report.add(scope, "S4", "FAIL", f"expected a /backups and an /odoo-data volume, found {len(volumes)}")
        return
    try:
        output = client.containers.run(
            container["Image"],
            # A one-element list: the SDK would split a plain string into words, and `sh -c` only
            # runs its first argument.
            command=["echo helper-ok; ls /b | wc -l; ls /d | wc -l"],
            entrypoint=["sh", "-c"],
            user="0",
            network_mode="none",
            volumes=volumes,
            tmpfs={"/backups": "size=1m"},  # the image's VOLUME /backups would leave an anonymous volume
            name=name,
            remove=True,
            detach=False,
        )
        left_over = api.containers(all=True, filters={"name": [name]})
    except Exception as e:
        report.add(scope, "S4", "FAIL", describe(e))
        return
    finally:
        try:
            api.remove_container(name, force=True)  # only does something if the run above died half way
        except docker.errors.NotFound:
            pass
        except Exception as e:
            print(f"  [S4] WARN could not clean up helper container {name}: {describe(e)}")
    if b"helper-ok" in output and not left_over:
        report.add(scope, "S4", "PASS", "helper created, started, waited, logs read, removed (mounts read-only)")
    else:
        report.add(scope, "S4", "FAIL", f"output={output[:80]!r} still_present={bool(left_over)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--endpoint", help="only this Portainer environment (by name)")
    parser.add_argument("--insecure", action="store_true", help="do not verify the TLS certificate")
    args = parser.parse_args()

    env = load_env(args.env_file)
    url = env.get("PORTAINER_URL", "").split("#")[0].rstrip("/")
    key = env.get("PORTAINER_API_KEY", "")
    if not url or not key:
        sys.exit(f"PORTAINER_URL and PORTAINER_API_KEY are required (env or {args.env_file})")
    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    report = Report()
    http = requests.Session()
    http.headers["X-API-Key"] = key
    http.verify = not args.insecure

    print(f"Portainer: {url}")
    try:
        response = http.get(f"{url}/api/endpoints", timeout=TIMEOUT)
        if response.status_code == 401:
            sys.exit("S1 FAIL: Portainer answered 401: the API key is wrong, expired or revoked")
        response.raise_for_status()
        environments = response.json()
    except (requests.RequestException, ValueError) as e:
        sys.exit(f"S1 FAIL: cannot read {url}/api/endpoints: {describe(e)}")

    print(f"Environments ({len(environments)}):")
    for e in environments:
        state = "up" if e.get("Status") == 1 else "down"
        print(f"  id={e['Id']} name={e['Name']} type={e['Type']} {state}")
    targets = [
        e
        for e in environments
        if e.get("Status") == 1
        and e.get("Type") in DOCKER_ENVIRONMENT_TYPES
        and (not args.endpoint or e["Name"] == args.endpoint)
    ]
    if not targets:
        sys.exit(
            "No usable Docker environment (up, Docker type"
            + (f", named {args.endpoint!r}" if args.endpoint else "")
            + ")"
        )

    for environment in targets:
        scope = environment["Name"]
        print(f"\n== {scope}")
        base = docker_base_url(url, environment["Id"])
        try:
            api_version = http.get(f"{base}/version", timeout=TIMEOUT).json()["ApiVersion"]
            client = docker.DockerClient(base_url=base, version=api_version, timeout=TIMEOUT)
            client.api.headers["X-API-Key"] = key
            client.api.verify = not args.insecure
            found = client.api.containers(all=True, filters={"label": [LABEL]})
        except Exception as e:
            report.add(scope, "S1", "FAIL", describe(e))
            continue
        found = [c for c in found if c["Image"].startswith(IMAGE_PREFIX)]
        names = ", ".join(c["Names"][0].lstrip("/") + f" ({c['State']})" for c in found) or "none"
        report.add(scope, "S1", "PASS" if found else "SKIP", f"docker api {api_version}; backup containers: {names}")
        running = [c for c in found if c["State"] == "running"]
        if not running:
            continue
        container = running[0]
        print(f"  using {container['Names'][0].lstrip('/')}")
        check_attach(client.api, container, report, scope)
        detached_ok = check_detached(client.api, container, report, scope)
        check_archive(client.api, container, report, scope, detached_ok)
        check_helper(client, container, report, scope)

    print("\nSummary")
    for scope, check, status, _ in report.rows:
        print(f"  {scope:<24} {check}  {status}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
