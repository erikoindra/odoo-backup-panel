# Odoo Backup Panel — Flow & Technical Requirements

Version 0.3 · 2026-10-28
Related repo: https://github.com/erikoindra/odoo-backup-service (companion, not modified)
Decisions: `docs/decisions.md` · Contract: `docs/contract.md` · Assumptions: `docs/assumptions.md`

Changes since 0.2: multi-host is now a goal (D5), landing page removed (D3), public access + IP
allowlist (D4), slug = backup stack name, filestore path from `FILESTORE_ROOT`, restore
uses `pipefail` + lock, contrast tokens fixed (D1).

Karpathy principles applied:
- Think before coding → section 2 (assumptions, tradeoffs, open questions).
- Simplicity first → no Redis/Celery, no SPA, no Instance model, no agent of our own.
- Surgical changes → the odoo-backup-service repo is not modified.
- Goal-driven execution → every step in section 10 has a `verify:`.

---

## 1. Goal & limits

A lightweight web panel for all Odoo backup stacks (`odoo-backup-service` containers) on
**one or many** Docker hosts:
- list the backup zips per instance, grouped by server,
- download a zip,
- run a backup now (`odoo-backup`),
- restore a zip to a new database on the same Odoo instance.

User flow: Sign in → Instance list (per server) → Instance detail (download / back up
now / restore) → Job status.

MVP non-goals (do not build unless asked):
- Overwriting a database that is in use.
- Uploading a zip from outside.
- Editing the cron schedule/retention from the panel (stays in Portainer).
- Tiered roles, 2FA, notifications.
- Deleting backups from the panel.
- Public landing page.
- An agent of our own on every host (Portainer or socket-proxy is used instead).

---

## 2. Assumptions, tradeoffs, open questions

### Assumptions
- A1–A6 (backup-service contract): **verified**, see `docs/assumptions.md` and
  `docs/contract.md`.
- S1–S4 (Docker API through the Portainer proxy: list, archive, exec, helper container):
  **passed** in Step 2 (2026-10-28), with one change: exec runs **without attach** (D7),
  because Portainer behind Cloudflare does not forward the connection upgrade.
  See `docs/assumptions.md`.

### Chosen tradeoffs
- T1. **Docker access = a list of endpoints** (D5). A local endpoint through
  `tecnativa/docker-socket-proxy`, or each Portainer CE environment through
  `/api/endpoints/<id>/docker` + `X-API-Key`. The panel must use the Docker API (exec without
  attach, get_archive, run). This goes against the backup-service design, which has no docker.sock.
  Honest note: anyone who can sign in to the panel can be root on every connected host.
  Mitigations are in section 7.
- T2. Dynamic discovery per endpoint (no Instance model, no volume mounted into the panel).
  New instances appear automatically. Zips are mode 0600 and owned by root, so the panel reads
  them through `get_archive`, not a mount.
- T3. The DB restore runs inside the backup container (it already has the credentials, the DB
  network, the zip and the tools). The filestore is copied by a one-off helper container that
  mounts the backup volume (ro) + the Odoo data volume (rw). The panel never stores or shows
  PGPASSWORD. Duplication with `tools/restore.sh` is accepted (D6).
- T4. Job runner: a `Job` table + a `manage.py run_jobs` process (polling SQLite). No
  Redis/Celery. Enough for 1–3 admins.
- T5. Frontend: Django templates + Tailwind (standalone CLI) + HTMX for polling + a small
  JavaScript file (`app.js`) for the restore wizard.

### Open questions (defaults apply until answered)
- Q1. Is an "overwrite the old database" option needed? Default: no (phase 2).
- Q2. Does a prod → staging restore need automatic `odoo neutralize`? Yes, as a switch in the restore
  wizard (D11); off by default.
- Q3. Is one admin account enough? Default: yes (`createsuperuser`).
- Q4. ~~Accessed via VPN or publicly?~~ Answered: public domain + IP allowlist (D4).

---

## 3. Architecture

```
Browser ──HTTPS──► nginx (another server: TLS, allow/deny IP)
                          │  only the nginx IP may reach the panel port (cloud firewall)
                   ┌──────┴───────┐
                   │  panel-web   │  Django + gunicorn
                   │ panel-worker │  manage.py run_jobs
                   └──────┬───────┘  (same image, SQLite in the panel_data volume)
            ┌─────────────┴──────────────┐
   endpoint "local"                Portainer endpoint (per environment)
   tcp://socket-proxy:2375         https://portainer/api/endpoints/<id>/docker
   (optional)                      + X-API-Key ──► portainer-agent on every host
            │                                  │
   [acme-staging-backup-odoo-backup-1]   [acme-prod-backup-odoo-backup-1] ...
     /backups   (backup volume)
     /odoo-data (Odoo data volume, ro)
     DB network, PG* env
```

The panel never joins any DB network.

---

## 4. Endpoints & discovery

### Endpoints
`endpoints()` reads env:
- `DOCKER_ENDPOINTS=name=url[,name=url...]`, for example `local=tcp://socket-proxy:2375`.
- `PORTAINER_URL` + `PORTAINER_API_KEY`: `GET {PORTAINER_URL}/api/endpoints` with the
  `X-API-Key` header. Each environment becomes an endpoint named `<Name>` with
  `base_url={PORTAINER_URL}/api/endpoints/<Id>/docker` + the same header. The Docker SDK rejects
  an `http(s)://` URL without a port, so the default port (443/80) is written explicitly. Do not
  use `tcp://`: the SDK turns it into http without TLS. The `X-API-Key` header is set in
  `client.api.headers` after the API version is read from `GET .../docker/version` (the SDK is
  created with `version=<ApiVersion>`, so no request goes out without the header).
  `PORTAINER_ENVIRONMENTS` (optional) = allowlist of names, comma-separated. The list is cached
  for 60 seconds.
- Endpoint names are unique (`^[A-Za-z0-9._-]+$`) and used in URLs. A clash → configuration
  error, do not guess.
- Tokens are never logged or displayed.

One failing endpoint (timeout, 401, 5xx) does not take the others down. The UI shows the error
per server with a short reason and a Retry button.

### Instance discovery
`discover_instances(endpoint)`:
1. `client.containers.list(all=True, filters={"label": "com.docker.compose.service=odoo-backup"})`
2. Keep only images that start with `BACKUP_IMAGE_PREFIX`
   (default `ghcr.io/erikoindra/odoo-backup-service`).
3. Build an `Instance` dataclass per container:
   - `endpoint`, `slug` = the `com.docker.compose.project` label as is (fallback: container name)
   - `container_name`, `status` (running/exited), `image`
   - `backup_volume` = the mount with Destination `/backups`
   - `odoo_data_volume` = the mount with Destination `/odoo-data`
   - from env (allowlist only): `PGHOST, PGPORT, PGUSER, DB_NAME, BACKUP_PREFIX,
     RETENTION_DAYS, CRON_SCHEDULE, TZ, FILESTORE_ROOT, ODOO_BACKUP_PG_MAJOR`
     (the container's full env is read once through `inspect`, then discarded except for the allowlist)
   - never read, store or log `PGPASSWORD` / `PGPASSWORD_FILE`.
4. In-memory cache, because through Portainer + Cloudflare one request takes 0.1–1.5 seconds
   (6–10 seconds for the first cold connection; measured 2026-10-28). What is cached: the
   Portainer environment list for 60 seconds; one `DockerClient` per endpoint for 10 minutes (the
   connection stays alive and the API version is not read again; dropped when the endpoint
   errors); the result of reading one server for 15 seconds, **only on success**, so that
   "Retry" really retries. `discover_all(use_cache=False)` for fresh data. Measured: cold 7–11
   seconds, warm 0.8 seconds, from cache 0 seconds.
5. Duplicate slugs within one endpoint → show an error, do not guess.

### Backup list
The container must be running:

```python
run_detached(api, container_id, ["sh", "-c", "stat -c '%n|%s|%Y' /backups/*.zip 2>/dev/null || true"])
```

Parse each line → `Backup(name=basename, size, mtime)`. Ignore anything whose name does not match
`^[A-Za-z0-9._-]+\.zip$` (including `.odoo-backup.lock`, `.*.partial`, `.work-*`). Sort newest
first.

### Running commands in the container (D7)
A normal `exec` needs `Connection: Upgrade` + `Upgrade: tcp`; Cloudflare in front of Portainer
does not forward it (S3a: 502). So every command goes through
`dockerops.run_detached(api, container_id, argv, environment=None, timeout=...)`:

1. Create an exec that runs
   `sh -c 'd="/tmp/$JOB"; mkdir "$d" && "$@" >"$d/out" 2>"$d/err"; rc=$?; exit $rc' sh <argv...>`
   with `JOB=obp-<hex>` in the env. Arguments pass through `"$@"` and env, never spliced into
   the shell string.
2. `exec_start(detach=True)`: a plain request, no Upgrade header.
3. Poll `exec_inspect` (0.4 seconds) until `Running` is false. The exit code comes from there.
   Past the time limit → error (the limit for backup/restore comes from settings, not 60 seconds).
4. `get_archive("/tmp/<JOB>")` → a tar containing `out` and `err`.
5. `finally`: exec `rm -rf /tmp/<JOB>`. The request is sent but not awaited: that saves three
   requests through Portainer, and nothing depends on it.

Tested in S3 against a real Portainer: exit code, separate stdout/stderr, env containing
spaces/quotes/`$`, 300 KB of output, no leftover files. Prototype: `scripts/spike_portainer.py`.

Consequence: output is read **after** the command finishes (also on failure), not live; the job
page already polls every 2 seconds, so that is enough. Temporary files live in the container's
`/tmp` (not a volume). If the worker dies midway, the leftovers are cleaned up when the worker
starts (`/tmp/obp-*` older than 1 hour).

---

## 5. Flow per action

Instance URL: `/e/<endpoint>/<slug>/`.

### 5.1 Download
1. GET `/e/<endpoint>/<slug>/backups/<filename>/download` (GET only; login required). The
   Download button in the table is a plain link with the `download` attribute, not HTMX.
2. Validate `filename` against `^[A-Za-z0-9._-]+\.zip$` (no `/`, so no path traversal; `%2F`
   is rejected too), and the name must be in the `/backups` listing (the listing the table uses;
   if it is not there, the listing is read once more, for a backup that was just created).
   Otherwise 404, without touching Docker.
3. `open_backup_stream`: `api.get_archive("/backups/<filename>")`, then **everything that can go
   wrong before the first byte** is checked here, so the answer is an error page rather than a
   broken download: file missing (404), container stopped (409), size not given by Docker or not
   matching the tar contents (502), unreadable archive (502).
4. The SDK stream is wrapped in `IterStream(io.RawIOBase)` (memoryview, no copying),
   `tarfile.open(mode="r|")`, first member, `extractfile`. The zip is never written to disk and
   never read into memory at once: tarfile reads about 10 KB ahead.
5. `StreamingHttpResponse` with 1 MB chunks, `Content-Length = stat["size"]`,
   `Content-Type: application/zip`, `Content-Disposition: attachment; filename="..."`,
   `Cache-Control: private, no-store`, `X-Accel-Buffering: no`. nginx: `proxy_buffering off`.
6. Audit: a `Job(kind=download)` is created `running` when the stream starts, then `done` ("N
   bytes sent") or `failed` (reason: the browser disconnected, or the connection to Docker broke).
   A truncated stream raises an error, so an interrupted download never looks complete.
   The HTTP stream to Docker is closed explicitly (tarfile does not close the file-like object it
   is given). If the process dies before finishing, the row is left `running`: it is cleaned up to
   `failed` when the worker starts (Step 6, all job kinds).

Speed is set by the network path, not the code: through Portainer and Cloudflare from a developer
laptop it measured 0.1 to 1 MB/s (varying), the same as Portainer's own static assets. Large
downloads (hundreds of MB) have not been tested.

### 5.2 Back up now
1. POST `/e/<endpoint>/<slug>/backup` (login + CSRF; GET → 405). The container must be running.
2. Reject (409) if a backup/restore Job is `queued|running` for the same `(endpoint, slug)`.
   The check and the insert are one write transaction (SQLite `BEGIN IMMEDIATE`), so two
   simultaneous clicks cannot both get through.
3. Create `Job(kind=backup, status=queued)` and redirect back to the instance detail page. Its job
   panel (the `/jobs/<id>/` fragment) reloads itself every 2 seconds through HTMX until `done`/`failed`
   (the final fragment has no `hx-get`, so polling stops), and the "Back up now" button is disabled with
   a visible reason while a job is active.
4. Worker (`manage.py run_jobs`): take the oldest `queued` job with a conditional update, run
   `run_detached(["odoo-backup"], timeout=JOB_TIMEOUT)`, store stdout+stderr in `Job.log` (last
   200 KB), and write a one-sentence summary to `Job.summary`. Exit code 0 → `done`. Otherwise
   `failed`; if the log contains `another backup is already running`, the summary is "A scheduled
   backup is running. Try again in a few minutes."
5. When the page sees the job finish, the fragment carries the header `HX-Trigger: backups-changed`;
   the backup table on the detail page reloads itself, and the server's listing cache is dropped first.

### 5.3 Restore (always to a new database)
Input: `filename` (must be in the listing), `new_db`, `neutralize` (optional, D11), `confirm` (must equal
`new_db`). The wizard lives at `/e/<server>/<stack>/restore/` (`?file=` preselects the zip; see `docs/ui.md`).
`new_db` validation: `^[a-z0-9_]{1,63}$`, not `postgres`/`template0`/`template1`. Prefill:
`<db>_restore_<YYYYMMDD>` from the zip name (other characters become `_`). The Restore button in the table
opens the wizard; without JavaScript the wizard is one plain form, and form errors are shown on the step
that has the problem (status 400). Rejected (409) if the instance's `FILESTORE_ROOT` does not start with
`/odoo-data/`, the container is stopped, or a job is active. The zip name must be in the listing (same as
download).

All values are passed as ENV (`NEW_DB`, `ZIP`, `FILESTORE_ROOT`, `MODE`), never as part of the script
text; the script is fixed (constants in `panel/jobs.py`).

Worker steps (`Job.steps` records progress, `Job.log` holds the output):

1. **Check filestore folder** (helper container, `MODE=check`, data volume `ro`): exists → fail,
   without changing anything.
2. **Database**: one exec in the backup container that holds the same lock as `odoo-backup`
   (`flock -n /backups/.odoo-backup.lock`, so the scheduled backup and its retention do not run in
   the middle of a restore): does the zip exist? does the database already exist? then `createdb`,
   load `dump.sql` through `unzip -p | psql -v ON_ERROR_STOP=1` with `pipefail`. Exit code 3 =
   database already exists, 4 = a backup is running, 5 = zip missing. The `db-created` and
   `dump-loaded` markers in the output are used to report what was created. `grep` here is
   **without `-q`**: with `pipefail`, an early exit kills `psql` with SIGPIPE and reads as "no match".
3. **Filestore**: a helper container from the backup image itself (`MODE=copy`): `user=0`,
   `network_mode=none`, `entrypoint=["sh","-c"]`, `command=[script]` (one element), backup volume
   `ro` at `/b` and data volume `rw` at `/d`, **`tmpfs` at `/backups`** (the image declares
   `VOLUME /backups`; without a mount there every helper leaves one anonymous volume behind).
   Extract `filestore/*` to `.restore-<db>`, move it to `<db>`, `chown` following the owner of the
   `filestore/` folder (exit 11 from `unzip` = zip without attachments → empty folder).
4. **Neutralize** (only when the switch is on, D11): `odoo neutralize -d "$NEW_DB"` runs inside the
   stack's Odoo container, found beforehand by the named volume mounted at `/var/lib/odoo` (exactly one,
   running); the container's own `HOST`/`USER`/`PASSWORD` provide the connection. A missing container
   fails the job before anything is created; a failed neutralize fails the job and says the copy is NOT
   neutralized.
5. Done: the summary says how to open `?db=<db>` and, without the switch, reminds about `odoo neutralize`.

Failure midway: **nothing is deleted automatically**. The summary states exactly what already
exists ("database X was created, but dump.sql failed to load", "…and the dump loaded, but the
filestore was not copied; inspect the folders X and .restore-X"). The panel has no command at all
that deletes a database or folder.

Note: the restore holds the backup lock while the dump loads. A large restore that collides with
the scheduled backup time makes that night's backup fail ("another backup is already running").

---

## 6. Django technical specification

### Stack
- Python 3.12, Django 5.x, gunicorn, `docker` (Docker SDK for Python; `requests` comes with it
  and is used for Portainer's `/api/endpoints`), `django-axes` (login rate limiting),
  `whitenoise` (static files).
- Tailwind standalone CLI in the build stage; HTMX and fonts (IBM Plex Sans, JetBrains Mono, woff2)
  as static files, not a CDN. Lucide icons as inline SVG.
- SQLite (WAL) in the `panel_data` volume.
- Dev/CI only (not in the image): `ruff` for lint + format, one `ruff.toml` at the repo root.
- `docker` has been in `requirements.txt` since Step 2 (used by `scripts/spike_portainer.py`).
  `gunicorn` was added at deploy time.

### Structure
```
backup_panel/
  manage.py
  config/            settings.py, urls.py, wsgi.py
  panel/
    models.py        Job
    dockerops.py     endpoints, discover_instances, list_backups, run_detached,
                     open_backup_stream, run_backup, restore_steps (all Docker access lives here)
    jobs.py          job executor (called by the worker)
    views.py
    forms.py         RestoreForm
    management/commands/run_jobs.py
    templates/panel/ base.html, instance_list.html, instance_detail.html,
                     _instance_row.html, _backup_table.html, _side_status.html, _job_panel.html,
                     restore.html, _restore_checks.html
    templates/registration/login.html
    tests/           test_dockerops.py (fake client), test_views.py, test_jobs.py,
                     test_contract.py (real Docker, CI only)
  static/src/input.css
  tailwind.config.js
scripts/
  check_contrast.py  checks token contrast in panel/static/panel/app.css
  spike_portainer.py Step 2
Dockerfile
docker-compose.yml
```

### Model
```python
class Job(models.Model):
    # kind: download | backup | restore
    # status: queued | running | done | failed
    kind, status
    endpoint        = CharField
    instance_slug   = CharField
    container_name  = CharField
    filename        = CharField(blank=True)   # download/restore
    target_db       = CharField(blank=True)   # restore
    neutralize      = BooleanField(default=False)  # restore: run `odoo neutralize` afterwards (D11)
    steps           = JSONField(default=list) # [{"name", "status", "at"}]
    summary         = CharField(blank=True)   # the outcome in one sentence, or why it failed
    log             = TextField(blank=True)
    created_by      = ForeignKey(User)
    created_at, started_at, finished_at
```

No Instance model (live discovery).

### URLs
| Method | Path | View | Notes |
|---|---|---|---|
| GET | `/` | redirect | to `/instances/` |
| GET/POST | `/login/` | LoginView | django-axes |
| POST | `/logout/` | LogoutView | |
| GET | `/instances/` | instance_list | login_required, grouped by endpoint |
| GET | `/e/<endpoint>/<slug>/` | instance_detail | detail page: chips and status; backup table loaded afterwards |
| GET | `/e/<endpoint>/<slug>/backups/` | backups_partial | HTMX fragment: backup table (`?refresh=1` re-reads, no cache) |
| GET | `/e/<endpoint>/<slug>/summary/` | summary_partial | HTMX fragment: last backup + count, for one row in the list |
| GET | `/e/<endpoint>/<slug>/backups/<filename>/download` | download | streaming |
| POST | `/e/<endpoint>/<slug>/backup` | backup_now | creates a Job |
| GET/POST | `/e/<endpoint>/<slug>/restore/` | restore | wizard (`?file=` preselects a zip); POST creates the Job |
| POST | `/e/<endpoint>/<slug>/restore/check/` | restore_check | HTMX fragment: the wizard's checks (database, folder, job, Odoo container) |
| GET | `/jobs/<id>/` | job_status | page, or the job panel fragment for HTMX; polling stops at done/failed |
| GET | `/jobs/` | job_list | audit (Step 8) |

### Worker
`manage.py run_jobs`: loops every 2 seconds, takes the oldest `queued` job with a conditional
update (`queued → running`), runs it, stores the result; `--once` runs what is waiting, then exits.
On start: backups/restores still `running` become `failed` ("the worker or the panel
restarted"), `running` downloads older than 6 hours are closed too (newer ones belong to the web
process and are left alone), then `/tmp/obp-*` older than one hour in the backup containers is cleaned up.
SIGTERM: finish the running job, then stop. One worker = sequential jobs, by design.
Every job ends `done` or `failed`, even when the worker's own code errors (logged in the worker
log, and the worker stays alive).

---

## 7. Security

- Access: a public domain behind nginx with HTTPS and `allow <ip>; deny all;` (D4).
- The panel port on the host is reachable only from the nginx IP, through the cloud firewall /
  security group, because Docker ports bypass ufw. Verified from another IP.
- django-axes reads the client IP from `X-Forwarded-For` with `TRUSTED_PROXY_COUNT=1`.
  Tested: locking out one IP does not lock out another.
- Portainer: a dedicated panel user that is only given the environments with backups, the token
  only in the panel's env, rotated regularly. The token = root power in those environments.
- socket-proxy (if used): enable only `CONTAINERS=1 EXEC=1 POST=1`, everything else 0; the
  socket is mounted `:ro`; the proxy only on the internal network. Verify the minimum permissions
  `containers.run` needs (create, start, wait, remove).
- Every write action = POST + CSRF. Restore requires retyping the DB name.
- Endpoint, slug, filename and DB name are validated via regex + an allowlist from the listing.
- Shell arguments go through ENV, never f-strings into the command.
- Env allowlist; the DB password and the Portainer token never reach the log/UI.
- `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_PROXY_SSL_HEADER`, HSTS in nginx.
- Audit: every download/backup/restore is recorded in Job (who, when, what, which endpoint).

---

## 8. Design system

Source: `design-system/odoo-backup-panel/MASTER.md` (ui-ux-pro-max, D10 redesign), tokens in
`backup_panel/panel/static/panel/app.css`, and page descriptions in `docs/ui.md`.
Category: internal operations / developer tool dashboard.

- Style: Minimalism & Swiss, data-dense (density 8), light by default, dark follows the OS.
- Colour: warm neutral surfaces; **teal** for actions, focus and running processes; **amber only for
  warnings and Stale**; green for healthy; red-orange for failed. Exact values are in MASTER.md.
- Typography: IBM Plex Sans (UI), JetBrains Mono (file names, DB names, logs). Self-hosted.
- Icons: Lucide SVG, no emoji.
- Components: bordered table rows, job panel (progress, steps, log), 4-step restore wizard, per-server groups.
- One primary button per view. Restore uses the danger button.
- Status is never colour alone: always text plus an icon/dot.
- Checklist: 4.5:1 contrast (`scripts/check_contrast.py`), visible focus ring, skip link,
  `cursor-pointer`, `prefers-reduced-motion`, 16px inputs on mobile, small buttons ≥40px at
  ≤720px, long file names wrap without truncation, responsive at 375/768/1024/1440.

### Pages
Described in `docs/ui.md`, which is the reference since the D10 redesign:
1. Sign in: username, password, "access is recorded" note, generic error; lockout page.
2. Instances: search (instance, database, server), one panel per server (name, connection status),
   one row per stack (database, last backup + age, file count + size, health status with reason).
   Unreachable server = error + Retry. Stopped container = status with reason, actions unavailable.
3. Instance detail: breadcrumb (Instances / server / stack), meta, "Back up now", job panel for the
   active or recent job, backup table (Download, Restore → wizard), Status and Last job cards.
4. Restore wizard: choose backup → target database (+ neutralize switch, checks) → confirm → progress.
5. Job page and Activity: steps with status, log, start/finish, creator; the audit list.

---

## 9. Deployment

Files in the repo: `Dockerfile` (python:3.12-slim, user `panel` uid 10001, whitenoise, `/data`),
`docker/entrypoint.sh` (`web`: migrate, then gunicorn; `worker`: wait for migrations, then `run_jobs`),
`docker-compose.yml`, `.env.example`, `deploy/nginx.conf.example`.

```
docker compose up -d                       # web + worker
docker compose --profile proxy up -d       # + socket-proxy for Docker on the same host
docker compose exec web python manage.py createsuperuser
```

- `web` and `worker` use the same image and the same `panel_data` volume (SQLite). The web port is
  published on `127.0.0.1` by default; `PANEL_BIND` is changed to a private address when nginx is on
  another host, and the cloud firewall allows only the nginx host (Docker ports bypass ufw).
- `socket-proxy` (`proxy` profile): `CONTAINERS=1 EXEC=1 POST=1`, socket `:ro`, on the `internal`
  network that connects only to the panel. Proven sufficient: the whole contract test suite passes
  through this proxy without a single request being rejected. With Portainer alone, the proxy is not used.
- The image HEALTHCHECK is a TCP port check (http would be redirected to https or rejected because of
  Host); the worker healthcheck is disabled in compose.
- `DJANGO_SSL_REDIRECT` turns on automatically when `TRUSTED_PROXY_COUNT > 0`; `DJANGO_HSTS_SECONDS` is
  optional (do not turn it on before https is proven to work). `check --deploy` is clean when HSTS is
  set (CI).
- The image is published to GHCR by `.github/workflows/image.yml` on `v*` tags.

---

## 10. Step-by-step execution plan

Each step = one small PR that the user reviews and commits before moving on.

0. Bootstrap the repo: working rules (now `docs/contributing.md`), decisions, contract, assumptions,
   spec 0.3, the first prototype
   (removed after the D10 redesign), design system.
   → verify: no real server/stack/user names in the repo; `scripts/check_contrast.py`
   all ≥4.5.
1. Django scaffold + env settings + Job model (with `endpoint`) + auth (login/logout, axes
   with `X-Forwarded-For`) + `/` redirect + basic CI (lint + test).
   → verify: `manage.py check` is clean; login, redirect and per-IP lockout tests pass.
2. Portainer spike S1–S4 with `scripts/spike_portainer.py` against a real environment
   (token from an env file outside the repo).
   → verify: S1–S4 results written in `docs/assumptions.md`; anything fails → STOP, ask.
3. `dockerops` (`load_endpoints`, `discover_instances`, `list_backups`, `run_detached`),
   the `PANEL_ENV_FILE` loader, and the instance **list** page per server filled with real
   data (no backup column and no search; that is Step 4).
   → verify: unit tests with a fake API pass (endpoint/mount/env/stat parsing, PGPASSWORD
   never present on the Instance object, a failing endpoint does not take the others down, exec
   never attaches); on a real Portainer the instances appear, the zip list matches
   `ls`, no password leaks, no leftover files.
4. **Detail** page (read-only: chips, status, backup list) and list refinements
   (last backup + count, search, links) per the design system. Reading
   `/backups` takes a few seconds through Portainer, so the table and the summary are loaded
   afterwards through HTMX (vendored, `static/panel/htmx.min.js`) and successful results are cached
   for 15 seconds. The Download, Back up now and Restore buttons do not exist yet: they follow in
   Steps 5–7.
   → verify: the zips on the page match `ls /backups` at that moment exactly; pre-delivery
   checklist; 375/768/1024/1440.
5. Streaming download.
   → verify: the `sha256sum` of the download matches the file in the volume; `../x.zip` and
   non-existent names → 404; an interrupted download is recorded as `failed`.
6. Job + worker + Back up now + HTMX polling. **(done)**
   → verify: click → a new zip appears; double click → rejected; lock held → clear message;
   container stopped → clear message; orphaned job → failed when the worker restarts.
7. Restore (5.3) **(done)** + contract test in CI (`panel/tests/test_contract.py`: builds its own
   lab with the real backup-service image; does not need `dev/docker-compose.yml`).
   → verify: the new DB exists; the count of non-null `ir_attachment.store_fname` equals the
   number of files in the new filestore; Odoo login at `?db=<new>` works; existing DB/folder →
   rejected without changes; corrupt zip → failed + a report of what was created; invalid name →
   form error.
8. Hardening + public deployment **(package and tests done; production installation is in the owner's hands)**: dedicated Portainer user, secure cookies, audit page, nginx
   allowlist, firewall.
   → verify: the section 7 checklist ticked one by one; IP outside the allowlist → 403; the panel
   port on the host is unreachable from the internet.
