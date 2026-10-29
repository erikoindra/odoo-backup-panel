# odoo-backup-panel

Web panel for Odoo backups made by
[odoo-backup-service](https://github.com/erikoindra/odoo-backup-service): list the
backups, download a zip, run a backup now, and restore into a new database.

One panel can watch many servers at once. The panel connects to local Docker through a
socket-proxy, or to every Portainer CE environment that already uses an agent.

**Status: feature complete (Steps 1-8).** The panel reads servers from Portainer or Docker, shows the
backup stacks, streams zip downloads, runs a backup now, and restores into a new database, with an
audit trail. Test results: [`docs/testing.md`](docs/testing.md). Plan and decisions:
[`docs/spec.md`](docs/spec.md), [`docs/decisions.md`](docs/decisions.md).

## Features

| Page | Contents |
|---|---|
| Instances | every backup stack, grouped by server, with its last backup; search |
| Stack detail | schedule, retention, zip list, **Download**, **Back up now**, **Restore** |
| Job | steps, log, and result; refreshes automatically until it finishes |
| Activity | every download, backup, and restore: who, when, the result |

A restore always goes into a **new** database, never overwrites, and never deletes anything (even when
it fails halfway: the job message states exactly what already exists).

## Running locally

Needs Python 3.12. The commands below call `.venv/bin/python` directly, so there is no
need to `activate` and they are the same in bash and fish.

```bash
cd odoo-backup-panel
python3 -m venv .venv
.venv/bin/pip install -r backup_panel/requirements.txt

cd backup_panel
env DJANGO_DEBUG=1 ../.venv/bin/python manage.py migrate
env DJANGO_DEBUG=1 ../.venv/bin/python manage.py createsuperuser
env DJANGO_DEBUG=1 ../.venv/bin/python manage.py runserver
```

For the Instances page to read your servers, give `runserver` the Portainer env file from
the "Connecting to a server" section below (the token stays out of the shell history):

```bash
env DJANGO_DEBUG=1 PANEL_ENV_FILE=$HOME/.config/odoo-backup-panel/portainer.env \
  ../.venv/bin/python manage.py runserver
```

**Back up now and Restore need the worker.** The buttons only queue a job; a separate
process does the work. Open a second terminal and run:

```bash
cd backup_panel
env DJANGO_DEBUG=1 PANEL_ENV_FILE=$HOME/.config/odoo-backup-panel/portainer.env \
  ../.venv/bin/python manage.py run_jobs
```

Without the worker, jobs stay "Queued". Downloads and listing do not need the worker.

The first call to Portainer can take 7-10 seconds (cold connection through Cloudflare and
the agent), after that under 1 second. Good results are cached for 15 seconds.

Open http://127.0.0.1:8000. The local database is `backup_panel/panel.sqlite3` (not
in git). `DJANGO_DEBUG=1` is for local use only: cookies are not required to be HTTPS and the secret
key uses a dev value.

Tests and lint, the same as CI:

```bash
cd backup_panel
env DJANGO_SECRET_KEY=test ../.venv/bin/python manage.py test
../.venv/bin/pip install ruff==0.17.3 && ../.venv/bin/ruff check . ../scripts
```

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `DJANGO_SECRET_KEY` | required when DEBUG is off | Django secret |
| `DJANGO_DEBUG` | `0` | `1` for local use only |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` under DEBUG | panel domains, comma-separated |
| `CSRF_TRUSTED_ORIGINS` | empty | e.g. `https://backup.example.com` |
| `TRUSTED_PROXY_COUNT` | `0` | number of reverse proxies in front of the panel (nginx = `1`). Used to read the real client IP from `X-Forwarded-For` |
| `DJANGO_SECURE_COOKIES` | the inverse of DEBUG | cookies over HTTPS only |
| `PANEL_DB_PATH` | `backup_panel/panel.sqlite3` | SQLite location (in the container: `/data/panel.sqlite3`) |
| `AXES_FAILURE_LIMIT` | `5` | failed sign-ins per IP before a 1-hour lockout |
| `SESSION_COOKIE_AGE` | `28800` | seconds (8 hours) |
| `TIME_ZONE` | `Asia/Jakarta` | display time zone |
| `PANEL_ENV_FILE` | empty | path of a `KEY=VALUE` file (outside the repo) read at start. Real environment variables win over the file contents |
| `PORTAINER_URL` | empty | e.g. `https://portainer.example.com`. Every Docker environment visible to the API key becomes one server |
| `PORTAINER_API_KEY` | empty | Portainer access token (My account → Access tokens) |
| `PORTAINER_ENVIRONMENTS` | empty | comma-separated list of environment names to use. Empty = all |
| `DOCKER_ENDPOINTS` | empty | direct Docker endpoints, `name=url` comma-separated, e.g. `local=tcp://socket-proxy:2375` |
| `BACKUP_IMAGE_PREFIX` | `ghcr.io/erikoindra/odoo-backup-service` | containers whose image starts with this prefix count as backup instances |

## Connecting to a server (connection test, Step 2)

The panel itself connects with `PORTAINER_URL` and `PORTAINER_API_KEY` (see Environment). Before
pointing it at a new Portainer, `scripts/spike_portainer.py` is a standalone check that proves Docker
can be driven through that Portainer (S1–S4 in [`docs/assumptions.md`](docs/assumptions.md)).

1. In Portainer: click your user name at the top right → **My account** → **Access tokens** →
   **Add access token**. Copy the token; it is shown only once.
2. Save it in a file **outside the repo**, readable only by you (these commands are the same in bash and fish):
   ```bash
   mkdir -p ~/.config/odoo-backup-panel
   install -m 600 /dev/null ~/.config/odoo-backup-panel/portainer.env
   nano ~/.config/odoo-backup-panel/portainer.env
   ```
   with the contents:
   ```
   PORTAINER_URL=https://portainer.example.com
   PORTAINER_API_KEY=ptr_xxxxxxxx
   ```
3. Run from the repo root:
   ```bash
   .venv/bin/pip install -r backup_panel/requirements.txt
   .venv/bin/python scripts/spike_portainer.py
   ```
   Options: `--endpoint <environment name>` for a single server, `--insecure` when the
   Portainer certificate is self-signed, `--env-file <path>` for another file.

The script changes nothing that persists: only one throwaway helper container (read-only
volume) and a temporary directory in the backup container's `/tmp`, both removed again.
The result is `PASS` / `FAIL` / `SKIP` per server for S1–S4, plus `INFO` for S3a (exec with
attach; behind Cloudflare it does fail and the panel does not use it). `SKIP` on S2 means
there is no zip in `/backups` yet. This token equals root access to every environment it reaches, so
keep it only in that file and delete it from Portainer when it is no longer needed.

## Contents

```
odoo-backup-panel/
├── docs/
│   ├── contributing.md      rules for changing this code (secrets, Docker access, restore safety)
│   ├── spec.md              flows & technical requirements (functional source of truth)
│   ├── decisions.md         decisions D1–D11 and their reasons
│   ├── ui.md                pages, instance states, restore wizard, accessibility
│   ├── contract.md          what the panel uses from odoo-backup-service
│   └── assumptions.md       results of verified assumptions + spikes still pending
├── design-system/odoo-backup-panel/MASTER.md   design system (ui-ux-pro-max, redesign D10)
├── backup_panel/            Django application (config/, panel/, tests)
├── Dockerfile, docker-compose.yml, .env.example, docker/, deploy/   deploy package
├── scripts/check_contrast.py  checks color token contrast (WCAG 4.5:1)
└── scripts/ui_shots.py      screenshots every page in headless Chromium (optional, needs Playwright)
```

## Seeing the UI

Run the panel locally (above), or render every page with sample data in headless Chromium:
`scripts/ui_shots.py` (optional, needs Playwright; see `docs/testing.md` section 2b). The pages, the
instance status rules and the restore wizard are described in [`docs/ui.md`](docs/ui.md); the design
tokens and components in `design-system/odoo-backup-panel/MASTER.md`. Light/dark follows the OS.

## Pages → Django templates

| Page | Template | View |
|---|---|---|
| login, locked | `registration/login.html`, `registration/locked.html` | `LoginView` |
| Instances | `panel/instance_list.html` + `_instance_row.html` (rows loaded afterwards) | `instance_list`, `summary_partial` |
| Detail | `panel/instance_detail.html` + `_backup_table.html`, `_side_status.html`, `_job_panel.html` | `instance_detail`, `backups_partial`, `download`, `backup_now` |
| Restore wizard | `panel/restore.html` + `_restore_checks.html` | `restore`, `restore_check` |
| Job, Activity | `panel/job_status.html` + `_job_panel.html`; `panel/job_list.html` | `job_status`, `job_list` |

Tokens live in `:root` in `backup_panel/panel/static/panel/app.css` (light and dark).

```bash
python3 scripts/check_contrast.py   # every color pair must be ≥4.5:1
```

## Relationship with odoo-backup-service

A separate project that accompanies odoo-backup-service (decision D2). The panel does not use its
code, only the contract through Docker (labels, mounts, env, commands, zip names). That contract
is written down in [`docs/contract.md`](docs/contract.md).

Tested with odoo-backup-service **0.1.x**.

## Security in short

Anyone who can sign in to the panel can run commands in containers on every connected
server, which equals root. Therefore the panel must only be reachable through a domain
with an IP allowlist, its port closed by the firewall for everything but the proxy, and its Portainer
token owned by a dedicated user with limited access. Details in [`docs/spec.md`](docs/spec.md) section 7.

## Deploy

```bash
cp .env.example .env            # fill in: secret, domain, PORTAINER_URL/API_KEY or DOCKER_ENDPOINTS
docker compose up -d            # web + worker   (add --profile proxy for Docker on the same host)
docker compose exec web python manage.py createsuperuser
```

This panel equals root access to every connected server, so: put it behind nginx with HTTPS and an
IP allowlist (`deploy/nginx.conf.example`), restrict the panel port with the cloud firewall (Docker bypasses
ufw), and use a dedicated Portainer user. Details and reasons: [`docs/spec.md`](docs/spec.md) sections 7 and 9.
The image is published to GHCR when a `v*` tag is pushed (`.github/workflows/image.yml`).

Contract tests against real Docker: `PANEL_CONTRACT_TESTS=1 python manage.py test panel.tests.test_contract`
(add `CONTRACT_ODOO_IMAGE=odoo:19.0` to test neutralize with a real Odoo).

## Vendored third-party files

| File | Version | License |
|---|---|---|
| `backup_panel/panel/static/panel/htmx.min.js` | htmx 2.0.11 (from `unpkg.com/htmx.org@2.0.11/dist/htmx.min.js`) | 0BSD |
| `backup_panel/panel/templates/panel/icons/*.svg` | Lucide (lucide-static 0.460.0) | ISC |
| `backup_panel/panel/static/panel/fonts/ibm-plex-sans.woff2` | IBM Plex Sans (variable, latin subset) | SIL OFL 1.1 |
| `backup_panel/panel/static/panel/fonts/jetbrains-mono.woff2` | JetBrains Mono (variable, latin subset) | SIL OFL 1.1 |

## License

MIT
