# Assumption verification

## A1–A6: backup-service contract (verified 2026-10-28)

Checked read-only against a running backup container (odoo-backup-service 0.1.0, pg17)
on the first server.

| | Assumption | Result |
|---|---|---|
| A1 | container recognised by the label `com.docker.compose.service=odoo-backup` + image prefix | ✓. Project label = name of the **backup stack** (`<name>-backup`), not the Odoo stack. The slug uses this label as is |
| A2 | `/backups` rw, `/odoo-data` ro | ✓ |
| A3 | `psql createdb unzip stat flock grep` present, runs as root | ✓ uid 0; `stat -c '%n\|%s\|%Y' /backups/*.zip` → `/backups/<file>.zip\|6740230\|1793146803` |
| A4 | DB user has CREATEDB | ✓ |
| A5 | lock behaviour | `flock -n`: a second run exits 1 immediately with `another backup is already running` |
| A6 | `FILESTORE_ROOT` | ✓ `/odoo-data/filestore` (default); filestore owner `100:101` |

Commands to repeat the check on another server (replace `<c>`; they do not print env contents):

```bash
C=<backup-container>
docker inspect "$C" --format 'service={{index .Config.Labels "com.docker.compose.service"}} project={{index .Config.Labels "com.docker.compose.project"}} image={{.Config.Image}}'
docker inspect "$C" --format '{{range .Mounts}}{{.Name}} -> {{.Destination}} rw={{.RW}}{{println}}{{end}}'
docker exec "$C" sh -c 'which psql createdb unzip stat flock grep; id -u; stat -c "%n|%s|%Y" /backups/*.zip'
docker exec "$C" sh -c 'grep -n flock /usr/local/bin/odoo-backup; echo FILESTORE_ROOT=$FILESTORE_ROOT'
```

## S1–S4: Docker API through the Portainer proxy (Step 2: PASSED, exec without attach)

Must pass before `dockerops` is built on top of Portainer (decision D5). Tested with
`scripts/spike_portainer.py` against real agent environments. The token is read from an env
file **outside the repo**. How to run: README, section "Connecting to a server".

The script changes nothing that persists: only a single-use helper container (read-only volume,
removed at the end) and a temporary directory in the backup container's `/tmp` (removed after
every command). Its output contains no API key, container env, or file contents.

Tested offline first (fake Portainer on localhost, 2026-10-28):
- the `X-API-Key` header is sent on every request, including `GET .../docker/version`;
- the path `/api/endpoints/<id>/docker/v<api>/...` is correct;
- the helper container request: `Cmd` with one element, `Binds` `:ro`, `NetworkMode=none`, `User=0`;
- failures (401, exec refused, missing environment) are reported, no crash.

Run against real Portainer (2026-10-28): S1, S2, S3, S4 passed in 2 agent environments.
The first time, S3 using exec with attach failed (502 from Cloudflare); S3 was then changed to
exec without attach (decision D7) and passed.

Findings while writing the script (apply to `dockerops`):
- the Docker SDK rejects `https://host/path` without a port; the default port must be written explicitly.
- the helper container command must be `[script]` (one element), not a string.

| | What is proven | Result |
|---|---|---|
| S1 | `GET /api/endpoints` with `X-API-Key`; list containers with a label filter through `/api/endpoints/<id>/docker` | **PASS** (2 agent environments, 2026-10-28) |
| S2 | `GET /containers/<id>/archive` streaming (whole zip, same sha256) | **PASS** (2 environments, zip of about 6.6 MB; sha256 equal to `sha256sum` in the container; `X-Docker-Container-Path-Stat` header present, size matches) |
| S3 | exec **without attach** (detach + file in `/tmp` + `exec_inspect` + `get_archive`): exit code, separate stdout/stderr, env with spaces/quotes/`$`, 300 KB of output, no leftover files | **PASS** in both environments (decision D7) |
| S3a | exec **with attach** (connection upgrade), informational only | **FAIL** in both environments: `POST /exec/<id>/start` answered with **502 from Cloudflare** (Portainer sits behind Cloudflare). Not used by the panel |
| S4 | create/start/wait/remove a helper container with a named volume, `network_mode=none` | **PASS** (read-only volume; helper removed; `GET .../logs` without upgrade works) |
| S5 | a **non-admin user** token with access only to the environments with backups can still do S1–S4 | not yet. Risk: Portainer's resource access control may hide containers/volumes owned by admin or refuse exec. To be tested before Step 8 |

If S3 or S4 fails: STOP and ask. Proposed fallback: a socket-proxy endpoint per host over a
private network (WireGuard/Tailscale), not a public port.

### Why exec without attach (D7)

`exec_start` with attach uses `Connection: Upgrade` + `Upgrade: tcp`. Cloudflare only forwards
WebSocket upgrades, so the result is 502 (S3a). Other requests (GET, plain POST, logs, archive)
go through. The chosen way out: `exec_start(detach=True)`, output to a file in `/tmp/<JOB>/` in
the container, status through `exec_inspect` (polling), the file through `get_archive`, then
the file is deleted. Details: spec section 4, "Running commands in the container (D7)".

Not yet tested:
- downloading zips of hundreds of MB through Cloudflare: Step 5 proved 6.6 and 17.7 MB (same sha256
  as `sha256sum` in the container); sizes that large have not been tried. Speed varies,
  0.1 to 1 MB/s from the developer's laptop, and Portainer's own static assets are the same;
- long-running commands (restore): the `JOB_TIMEOUT` limit in Steps 6-7;
- S5 (non-admin user): before Step 8.
