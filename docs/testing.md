# Testing

Three layers, from the cheapest to the closest to reality. Results below are from 2026-10-29.

## 1. Unit tests (`manage.py test`, no Docker)

367 tests, 30 of them (contract) skipped unless enabled. Docker is replaced by `FakeApi`/`FakeClient`
(`panel/tests/fakes.py`), so these tests guard the logic, not the shell scripts.

| Area | What is guarded |
|---|---|
| Login and IP | lockout per client address behind the proxy, forged `X-Forwarded-For` entries ignored, logout only via POST |
| Endpoints | Portainer URL becomes a Docker URL with an explicit port, environment allowlist, duplicate names dropped, API key absent from `repr` |
| Discovery | only backup-service images, env filtered to the allowlist (it includes `BACKUP_FILESTORE`), `PGPASSWORD` never stored, duplicate stack names hidden |
| Commands in the container | exec without attach, arguments through `"$@"` and env, temporary files deleted even on failure |
| Download | strict names, must be in the listing, 404/409/502 before the first byte, a truncated stream raises an error, the HTTP stream is closed, audit `done`/`failed` |
| Back up now | one active job per stack (also against restore), CSRF, login, server down, scheduled-backup lock message |
| Back up database only | the scope field decides (`database` only; no scope or `full` is a full backup), the job runs `odoo-backup` with `BACKUP_FILESTORE=false` and the stack's prefix + `dbonly-` (a plain backup sets neither), a service older than 0.2 or with an unknown version is refused with a 409 and again when the queued job runs (nothing is started), a stack that is database-only already has no button, the button is off with its reason, version parsing (`v0.2.0`, `0.10.0`, `v1.0.0` yes; `v0.1.9`, `master`, `v0.2.0-rc1`, none no) |
| Health with database-only zips | `latest_full` skips `dbonly-` zips, so a newer one cannot hide a stale schedule; only database-only zips say "No full backup yet"; the stack's own prefix is part of the database-only prefix |
| Restore | form validation, name from the zip name, filestore root outside `/odoo-data` refused, every failure reports what already exists, nothing is deleted, form values never end up in the script text |
| Worker | sequential claims, orphaned jobs failed at start, an error in job code does not kill the worker, `--once` |
| Job page | job panel: polling stops when finished, steps/progress/log, `HX-Trigger` reloads the table, expired session in a fragment → `HX-Redirect` |
| Instance status | cron (`*/n`, ranges, lists, weekdays, unparseable), expected interval = longest gap, Stale boundary exactly 1.5×, stopped container beats everything, age/duration formatting |
| List and detail | rows load themselves and are replaced by full rows, `?refresh=1` passed to every row, Status card via OOB swap, active job panel and last job, the "DB saja" label only for `BACKUP_FILESTORE=false` (any case, spaces ignored) in the list, the finished row and the detail page |
| Restore wizard | file from `?file=` or the newest, name/confirmation/file validated, errors open the right step, one job per stack, neutralize switch stored in the job |
| Restore checks | database and folder free, other jobs, Odoo container (only when neutralizing), one failed check does not hide the others, POST + CSRF |
| Neutralize | Odoo container found through the volume (one, running, no environment in the result), the job stops before changing anything if there is none, a neutralize failure is reported as "NOT neutralized" without deleting, the script only accepts the name through a variable |
| Markup | all pages and fragments (including the wizard and the checks fragment); every id/attribute `app.js` looks for exists on the page; (including empty, stopped, error, failed states): balanced tags, unique ids, every `aria-*`/`for`/`href="#..."` points to an existing id, buttons and links have text, every internal URL resolves, every `{% include %}` exists |
| Delete | the job (only the chosen zips go, names reach the script only as a variable, the newest of each database is kept, a file that is gone, a held lock and a failing `rm` each stop it with a clear message, a delete cut off by a restart is failed), the views (confirmation page, every refusal, the word `DELETE`, one working job per stack, superusers only with a 403 for others, CSRF), the table controls for superusers and for others, the job page |
| Delete script | the real `DELETE_SCRIPT` in a real shell with `/backups` pointed at a temporary folder: valid names removed, bad names exit 6, a missing file exits 5 and removes nothing, a held lock exits 4 (`test_delete_script.py`, needs `sh` and `flock`) |

**Mutation testing** (code deliberately broken, then restored): 5 in download, 9 in job/view/form/middleware, and during the redesign: the `checks` id in the wizard (markup test fails) and an ignored neutralize failure (job test fails). For delete: the newest-backup protection in the job and in the view, grouping by database instead of one newest overall, the superuser check, and the confirmation word: every one was caught.
All were caught, except one check that turned out to be unreachable and was removed.
One known weakness: the "two workers claim the same job" test cannot simulate a real race;
it only guards that a job already claimed is not claimed again.

## 2. Contract tests (`panel/tests/test_contract.py`, real Docker)

Builds its own lab (PostgreSQL, an Odoo data volume with a filestore, the real backup-service container),
then runs the panel code. Run it with `scripts/run_contract_tests.sh` (below), which sets
`PANEL_CONTRACT_TESTS=1`. Also runs in CI, on GitHub's disposable runners.

**Where to run it.** The runner needs the Docker socket, so whoever can run code in it controls the host.
Run it only on a disposable host (a throwaway VM), never on a server that runs live stacks. The script builds
the runner image with its dependencies baked in, runs it with `--network none`, names it and removes it when
the run ends, and refuses to start until `OBP_CONTRACT_ON_DISPOSABLE_HOST=yes` is set. Details: decision D12.
Example: `OBP_CONTRACT_ON_DISPOSABLE_HOST=yes scripts/run_contract_tests.sh`. The lab builder is in the base class `LabCase`; a second class, `NeutralizeContractTests`, uses **real Odoo** and is active only when `CONTRACT_ODOO_IMAGE` is set (e.g. `odoo:19.0`).

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
| 16 | delete: the chosen zips (of two databases) are removed and nothing else (not the lock, not other files, not the database or filestore) | **written, not run yet** |
| 17 | delete: the newest zip of each database is refused and nothing is removed | **written, not run yet** |
| 18 | delete: a held backup lock stops it, nothing removed | **written, not run yet** |
| 19 | delete script alone, in the real BusyBox shell: paths, leading dots, non-zips, patterns, a missing file and a good name next to a bad one remove nothing | **written, not run yet** |
| 20 | delete: a zip that vanished before the job ran is reported | **written, not run yet** |

**Neutralize with real Odoo** (`NeutralizeContractTests`, `CONTRACT_ODOO_IMAGE=odoo:19.0`; the lab initialises
the database with `odoo -i base`, then an Odoo container attached to the same data volume):

| # | Scenario | Result |
|---|---|---|
| 01 | Odoo container found through the named volume, no environment in the result | pass |
| 02 | restore + neutralize: `ir_config_parameter database.is_neutralized` = `true` in the copy, not in the source; Odoo itself can open the copy (`odoo shell`, counting `res.users`) | pass |
| 03 | restore without the switch: not neutralized, the summary reminds you | pass |
| 04 | Odoo container stopped: the job fails before anything is created (no database, no folder) | pass |

**Database-only mode** (`DatabaseOnlyContractTests`; the backup container runs with `BACKUP_FILESTORE=false` and
`BACKUP_PREFIX=dbonly-`, while the data volume still holds a filestore). It needs odoo-backup-service 0.2.x (the default `pg17-0.2` image); on a 0.1 image the zip would still hold the
filestore and the scenarios fail. Scenario 11 above restores a hand-made zip without `filestore/`; these use a
zip the service made itself:

| # | Scenario | Result |
|---|---|---|
| 01 | the panel reads `BACKUP_FILESTORE=false` from the container and still keeps no secret | pass (CI, `577949a`) |
| 02 | Back up now makes a prefixed zip that holds `dump.sql` only (CRC passes, no `filestore/` entry) | pass (CI, `577949a`) |
| 03 | restore of that zip: database with the same rows, an empty filestore folder owned like Odoo's, no `.restore-*` folder left, the original untouched | pass (CI, `577949a`) |

**Database-only button** (`DatabaseOnlyButtonContractTests`; a stack that makes full backups; it needs the 0.2.x image, which reports `ODOO_BACKUP_VERSION`). They share one lab and run in order:

| # | Scenario | Result |
|---|---|---|
| 01 | the real image reports a version that allows the button, through `docker inspect` and the allowlist | **written, not run yet** |
| 02 | the container already defines `BACKUP_FILESTORE=true` and `BACKUP_PREFIX=prod_`, as one from the sample compose file does; the exec environment replaces both and reaches the script: a `prod_dbonly-shop_<stamp>.zip` that holds `dump.sql` only, the log says "database only", and a stack with only such zips is "No full backup yet", not Fresh | **written, not run yet** |
| 03 | a plain backup after it keeps the filestore (`prod_shop_<stamp>.zip`), `latest_full` is that one, and Health is Fresh | **written, not run yet** |

Two runs on the real server (before Odoo was added 15/15; after the redesign 19/19 in both modes): directly over the Docker socket (35 seconds per mode), and with the panel talking
through a **minimum-permission docker-socket-proxy** `CONTAINERS=1 EXEC=1 POST=1` (zero requests refused).

Findings from the Odoo tests: Odoo stores lowercase `true`; `odoo shell` through `exec` bypasses the image entrypoint, so it needs `--db_host/--db_user/--db_password` (the panel's neutralize script already adds them); the Odoo image declares `VOLUME /mnt/extra-addons`, so the test mounts a tmpfs there and removes the container with `v=True` so no anonymous volume leaks.

## 2b. Rendering in a headless browser (`scripts/ui_shots.py`, Chromium)

Optional developer tool, needs Playwright in the venv (not a project dependency). The panel runs as a live
server with Docker faked, then headless Chromium captures 27 page states at 375/768/1024/1440 px,
light and dark (216 PNGs to `~/odoo-backup-panel-screenshots`, outside the repo), and checks what is only
visible in a browser: no horizontal scroll at any width, no console or server errors,
`app.js` really runs (status tally filled, timer running, toast appears, "Dismiss" closes the panel),
wizard: `?file=` selected, "Next" disabled until all checks pass and disabled again when one fails,
summary correct, wrong confirmation refused, the submission becomes a job with the chosen file and switch; delete: the button waits for a choice, "select all" and single boxes update the count, the newest backup's box is disabled, the wrong word is refused, and the confirmed delete becomes a job with exactly the chosen files.

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
- **Delete in the contract tests (16-20)** against real Docker. They are written, but contract tests may only run on a disposable host (D12) and none was available. The script itself is proven in `dash` and `bash` (`test_delete_script.py`); BusyBox, the shell of the backup container, is exactly what scenario 19 checks.
- **Database-only button contract tests** (`DatabaseOnlyButtonContractTests`) against real Docker. They have not run yet: the CI `contract` job (a throwaway runner) runs them on the push that carries them. Until it is green, nothing proves that the exec environment replaces the `BACKUP_FILESTORE` and `BACKUP_PREFIX` that a container already defines, as one from the sample compose file does. When it is green, replace "written, not run yet" in the table above. To run them by hand: `OBP_CONTRACT_ON_DISPOSABLE_HOST=yes scripts/run_contract_tests.sh` on a disposable host.
- **Zips of hundreds of MB** through Cloudflare (largest tested 17.7 MB), and `JOB_TIMEOUT` on a long restore.
- **Non-admin Portainer token** (S5): all tests used an admin token.
- **The panel behind real nginx and Cloudflare**: headers and flows were simulated with an HTTP client;
  nginx and the IP allowlist are not installed yet.
- **Two workers at once** (not supported; one worker is the design).
