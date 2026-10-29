# Contract with odoo-backup-service

The panel does not use any odoo-backup-service code. It depends only on the things below,
which are visible through the Docker API. If any of them changes in backup-service, the panel
must change with it. The contract test in CI (spec section 10, Step 7) runs the panel's flows
against the real image to catch such changes.

Tested with: **odoo-backup-service 0.1.x** (`ghcr.io/erikoindra/odoo-backup-service:pgNN-0.1`).

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

`PGHOST PGPORT PGUSER DB_NAME BACKUP_PREFIX RETENTION_DAYS CRON_SCHEDULE TZ FILESTORE_ROOT ODOO_BACKUP_PG_MAJOR`

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
- File mode `0600` owned by root, so readable only through the Docker API (`get_archive`/exec)
  or a root container.
- Retention deletes a database's zips older than `RETENTION_DAYS` × 24 hours,
  only after the next backup succeeds.

## Filestore

- `FILESTORE_ROOT` defaults to `/odoo-data/filestore`. In a helper container that mounts the data
  volume at `/d`, the path becomes `/d${FILESTORE_ROOT#/odoo-data}`.
- The owner of the filestore folder follows the Odoo user (e.g. `100:101`). A restored folder is
  `chown`ed to match the owner of `filestore/`.
