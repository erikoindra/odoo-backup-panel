# Contract with odoo-backup-service

The panel does not use any odoo-backup-service code. It depends only on the things below,
which are visible through the Docker API. If any of them changes in backup-service, the panel
must change with it. The contract test in CI (spec section 10, Step 7) runs the panel's flows
against the real image to catch such changes.

Tested with: **odoo-backup-service 0.2.x** (`ghcr.io/erikoindra/odoo-backup-service:pgNN-0.2`), which added
`BACKUP_FILESTORE`. The contract tests default to `pg17-0.2`. The database-only scenarios need that variable, so
they fail on a 0.1 image. Nothing in this file about `BACKUP_FILESTORE` has been run against the real 0.2.x image
yet: the scenarios are written, and the CI `contract` job runs them on the next push. Everything else here is unchanged from 0.1.x:
the default behaviour of the service did not change.

## Recognising an instance

| Item | Value |
|---|---|
| Service label | `com.docker.compose.service=odoo-backup` (the service name in `stacks/odoo-backup/docker-compose.yml`) |
| Image | starts with `BACKUP_IMAGE_PREFIX` (default `ghcr.io/erikoindra/odoo-backup-service`) |
| Slug | label `com.docker.compose.project` = name of the **backup stack** (e.g. `acme-staging-backup`), used as is; falls back to the container name |

## Mounts

| Path in the container | Contents | Mode |
|---|---|---|
| `/backups` | backup volume (zips, `.odoo-backup.lock`, `.work-*`, `.*.zip.partial`) | rw |
| `/odoo-data` | Odoo `data_dir` volume | ro |

## Env the panel may read (allowlist)

`PGHOST PGPORT PGUSER DB_NAME BACKUP_PREFIX BACKUP_FILESTORE RETENTION_DAYS CRON_SCHEDULE TZ FILESTORE_ROOT ODOO_BACKUP_PG_MAJOR`

`BACKUP_FILESTORE` is `true` (default) or `false` and is not sensitive. When it is `false`, ignoring spaces and
upper or lower case, the panel shows the label "DB saja" on the instance list and the instance page. A
container that does not set it (service 0.1.x) is a normal, full-backup stack. The panel has no button of its
own for a database-only backup: such a stack is a separate compose project with its own `BACKUP_PREFIX` and
volumes, so it is a separate instance.

`PGPASSWORD` and `PGPASSWORD_FILE` are **never** stored, shown, or logged, and the panel never
asks for their values. Honest note: the `docker inspect` response carries the container's whole
env, so the panel receives the value briefly in memory. `instance_from_inspect` immediately
filters it to the allowlist above and drops the rest (tested: the password is not in any
`Instance`, also against a real password in a real Portainer). The password is only used inside
the backup container, through exec. Avoiding that receipt entirely would mean replacing
`inspect` with one exec per container, and that is too slow.

## Commands and tools in the image

| Item | Behaviour |
|---|---|
| `odoo-backup` | exit 0 = all databases succeeded; exit 1 = failed, message on stderr |
| Lock | `flock -n` on `/backups/.odoo-backup.lock`. If the lock is held, `odoo-backup` exits 1 immediately with the message `another backup is already running` |
| Tools | `psql`, `createdb`, `unzip` (Info-ZIP), `stat` (BusyBox, `-c '%n\|%s\|%Y'`), `flock`, `grep`, `sh`; runs as root (uid 0) |
| DB connection | the `PG*` env is already available in the container. The DB user has CREATEDB |

## Backup files

- Name: `^<BACKUP_PREFIX><db>_\d{8}_\d{6}\.zip$`, created atomically (`.<name>.zip.partial` then rename).
- Contents: `dump.sql` (pg_dump `--no-owner`, plain) and `filestore/` (the contents of
  `FILESTORE_ROOT/<db>/` directly: `00/`, `0a/`, …).
- With `BACKUP_FILESTORE=false` the zip holds `dump.sql` only: there is no `filestore/` entry at all, not even an
  empty one. The name pattern is the same, so a database-only stack should have its own `BACKUP_PREFIX` (and its
  own backup volume): otherwise retention and the panel's grouping by name would mix its zips with a full
  stack's. Two stacks on one backup volume also share one lock, so their schedules must not overlap.
- File mode `0600` owned by root, so readable only through the Docker API (`get_archive`/exec)
  or a root container.
- Retention deletes a database's zips older than `RETENTION_DAYS` × 24 hours,
  only after the next backup succeeds.
- The panel removes a zip only on request (docs/decisions.md D13): one `rm -f -- /backups/<name>` per chosen
  file, under the same `flock` on `/backups/.odoo-backup.lock`, after checking that every name is a plain zip
  name (no path, no leading dot) and that every file exists. It never removes anything else in `/backups`.

## Filestore

- `FILESTORE_ROOT` defaults to `/odoo-data/filestore`. In a helper container that mounts the data
  volume at `/d`, the path becomes `/d${FILESTORE_ROOT#/odoo-data}`.
- The owner of the filestore folder follows the Odoo user (e.g. `100:101`). A restored folder is
  `chown`ed to match the owner of `filestore/`.
- Restoring a zip without `filestore/` (a database-only zip, or any zip made without one) still works: the
  database is created and an empty folder is made for it (`FILESTORE_SCRIPT`). Attachments that live in the
  filestore are then missing until the files are copied in by other means.
