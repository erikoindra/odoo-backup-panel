# Testing

Three layers, from the cheapest to the closest to reality. Results below are from 2026-10-29.

## 1. Unit tests (`manage.py test`, no Docker)

267 tests, 19 of them (contract) skipped unless enabled. Docker is replaced by `FakeApi`/`FakeClient`
(`panel/tests/fakes.py`), so these tests guard the logic, not the shell scripts.

| Area | What is guarded |
|---|---|
| Login and IP | lockout per client address behind the proxy, forged `X-Forwarded-For` entries ignored, logout only via POST |
| Endpoints | Portainer URL becomes a Docker URL with an explicit port, environment allowlist, duplicate names dropped, API key absent from `repr` |
| Discovery | only backup-service images, env filtered to the allowlist, `PGPASSWORD` never stored, duplicate stack names hidden |
| Commands in the container | exec without attach, arguments through `"$@"` and env, temporary files deleted even on failure |
| Download | strict names, must be in the listing, 404/409/502 before the first byte, a truncated stream raises an error, the HTTP stream is closed, audit `done`/`failed` |
| Back up now | one active job per stack (also against restore), CSRF, login, server down, scheduled-backup lock message |
| Restore | form validation, name from the zip name, filestore root outside `/odoo-data` refused, every failure reports what already exists, nothing is deleted, form values never end up in the script text |
| Worker | sequential claims, orphaned jobs failed at start, an error in job code does not kill the worker, `--once` |
| Job page | job panel: polling stops when finished, steps/progress/log, `HX-Trigger` reloads the table, expired session in a fragment → `HX-Redirect` |
| Instance status | cron (`*/n`, ranges, lists, weekdays, unparseable), expected interval = longest gap, Stale boundary exactly 1.5×, stopped container beats everything, age/duration formatting |
| List and detail | rows load themselves and are replaced by full rows, `?refresh=1` passed to every row, Status card via OOB swap, active job panel and last job |
| Restore wizard | file from `?file=` or the newest, name/confirmation/file validated, errors open the right step, one job per stack, neutralize switch stored in the job |
| Restore checks | database and folder free, other jobs, Odoo container (only when neutralizing), one failed check does not hide the others, POST + CSRF |
| Neutralize | Odoo container found through the volume (one, running, no environment in the result), the job stops before changing anything if there is none, a neutralize failure is reported as "NOT neutralized" without deleting, the script only accepts the name through a variable |
| Markup | all pages and fragments (including the wizard and the checks fragment); every id/attribute `app.js` looks for exists on the page; (including empty, stopped, error, failed states): balanced tags, unique ids, every `aria-*`/`for`/`href="#..."` points to an existing id, buttons and links have text, every internal URL resolves, every `{% include %}` exists |

**Mutation testing** (code deliberately broken, then restored): 5 in download, 9 in job/view/form/middleware, and during the redesign: the `checks` id in the wizard (markup test fails) and an ignored neutralize failure (job test fails).
All were caught, except one check that turned out to be unreachable and was removed.
One known weakness: the "two workers claim the same job" test cannot simulate a real race;
it only guards that a job already claimed is not claimed again.

## 2. Contract tests (`panel/tests/test_contract.py`, real Docker)

Builds its own lab (PostgreSQL, an Odoo data volume with a filestore, the real backup-service container),
then runs the panel code. Enable with `PANEL_CONTRACT_TESTS=1`. Also runs in CI. The lab builder is in the base class `LabCase`; a second class, `NeutralizeContractTests`, uses **real Odoo** and is active only when `CONTRACT_ODOO_IMAGE` is set (e.g. `odoo:19.0`).

| # | Scenario | Result |
|---|---|---|
| 01 | the panel finds the container, correct volumes, does not store the password | pass |
| 02 | Back up now produces a valid zip (`dump.sql` + `filestore/`, CRC passes) | pass |
| 03 | the download is byte-for-byte identical to the file in the container (sha256) | pass |
| 04 | `.hidden.zip`, `notes.txt`, `.partial`, `../`, unknown names are not served | pass |
| 05 | a scheduled backup holds the lock: backup and restore wait with a clear message, no changes | pass |
| 06 | restore: database and filestore created, same contents, folder owner same as Odoo, no `.restore-*` folder left, original database intact | pass |
| 07 | restore to an existing name: refused, nothing changed | pass |
| 08 | database exists without a folder: refused | pass |
| 09 | filestore folder already exists: stops before the database is created | pass |
| 10 | corrupt zip: half-made database reported, not deleted | pass |
| 11 | zip without filestore: database and an empty folder | pass |
| 12 | a database name containing `$(...)` is data, not code | pass |
| 13 | no temporary directory left in the container | pass |
| 14 | no new anonymous volumes (catches the D8 leak) | pass |
| 15 | stopped container: the job fails with a clear sentence, does not hang | pass |

**Neutralize with real Odoo** (`NeutralizeContractTests`, `CONTRACT_ODOO_IMAGE=odoo:19.0`; the lab initialises
the database with `odoo -i base`, then an Odoo container attached to the same data volume):

| # | Scenario | Result |
|---|---|---|
| 01 | Odoo container found through the named volume, no environment in the result | pass |
| 02 | restore + neutralize: `ir_config_parameter database.is_neutralized` = `true` in the copy, not in the source; Odoo itself can open the copy (`odoo shell`, counting `res.users`) | pass |
| 03 | restore without the switch: not neutralized, the summary reminds you | pass |
| 04 | Odoo container stopped: the job fails before anything is created (no database, no folder) | pass |

Two runs on the real server (before Odoo was added 15/15; after the redesign 19/19 in both modes): directly over the Docker socket (35 seconds per mode), and with the panel talking
through a **minimum-permission docker-socket-proxy** `CONTAINERS=1 EXEC=1 POST=1` (zero requests refused).

Findings from the Odoo tests: Odoo stores lowercase `true`; `odoo shell` through `exec` bypasses the image entrypoint, so it needs `--db_host/--db_user/--db_password` (the panel's neutralize script already adds them); the Odoo image declares `VOLUME /mnt/extra-addons`, so the test mounts a tmpfs there and removes the container with `v=True` so no anonymous volume leaks.

## 2b. Rendering in a headless browser (`scripts/ui_shots.py`, Chromium)

Optional developer tool, needs Playwright in the venv (not a project dependency). The panel runs as a live
server with Docker faked, then headless Chromium captures 22 page states at 375/768/1024/1440 px,
light and dark (176 PNGs to `~/odoo-backup-panel-screenshots`, outside the repo), and checks what is only
visible in a browser: no horizontal scroll at any width, no console or server errors,
`app.js` really runs (status tally filled, timer running, toast appears, "Dismiss" closes the panel),
wizard: `?file=` selected, "Next" disabled until all checks pass and disabled again when one fails,
summary correct, wrong confirmation refused, the submission becomes a job with the chosen file and switch.

Results 2026-10-29: all passed **after four fixes** that were only visible in a browser:

| Finding | Fix |
|---|---|
| At 1024 px the main column overflowed under the side column (Dismiss and Restore buttons covered): `.stack` used an implicit `auto` grid column that followed the table's `min-width` | `grid-template-columns: minmax(0, 1fr)`; two columns now start at 1140 px |
| The list page could scroll 321 px sideways at 375 px: the `span.sr-only` (`position: absolute`) in the Open button escaped the unpositioned `.tablewrap` | `.tablewrap { position: relative }` |
| The Stale reason had a comma at the end of the sentence (`replace(".", ",")` hit the sentence's full stop) | only the factor number is formatted |
| `?db=` in the wizard alert dropped onto its own line; the job page had a Dismiss button pointing to itself | `.alert .mono` inline; the panel on the job page has no Dismiss/link (`?page=1` while polling) |

Not yet seen by a human; the screenshots are there for the owner.

## 3. End-to-end tests

**Real Portainer (2 environments, 3 stacks, via Cloudflare)**, 2026-10-28/29:
instance list, zip list equal to an independent `ls -l`, times in WIB; 5 zips (6.6 and 17.7 MB)
downloaded with the same sha256 as `sha256sum` inside the container; the downloaded zip opens with
the correct layout; `../x.zip`, unlisted names, and non-zips → 404; an interrupted download is recorded
as `failed`; Back up now through the worker and Portainer finished in 5 seconds.

**Deploy package** (image built on the server, `docker compose --profile proxy up`):
http → 301 https; `Secure` cookie; `X-Frame-Options`, `nosniff`, referrer policy; unlisted Host → 400;
5 wrong logins lock one address (429) while another address behind the same proxy is not locked;
login; local Docker through the socket-proxy; HTMX fragment without a session → `HX-Redirect`; Back up now
by the worker on a real container; double click → 409; `HX-Trigger` reloads the table; download sha256
identical; the job history records everything. Containers run as uid 10001; web healthy, migrations
applied automatically.

## 4. NOT yet tested

- **Rendering seen by human eyes.** So far there are only headless screenshots (2b) with sample data;
  nobody has looked at the pages with real Portainer data in a browser, and the aesthetic judgement
  (spacing, hierarchy) has not been done by a person.
- **Logging in to Odoo over HTTP with a restored database** (`?db=<new>`): the copy is proven to open in
  real Odoo through `odoo shell` (registry loaded, `res.users` readable), but its HTTP login page has not
  been tried.
- **Zips of hundreds of MB** through Cloudflare (largest tested 17.7 MB), and `JOB_TIMEOUT` on a long restore.
- **Non-admin Portainer token** (S5): all tests used an admin token.
- **The panel behind real nginx and Cloudflare**: headers and flows were simulated with an HTTP client;
  nginx and the IP allowlist are not installed yet.
- **Two workers at once** (not supported; one worker is the design).
