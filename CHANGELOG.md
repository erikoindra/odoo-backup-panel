# Changelog

## [0.4.0] - UNRELEASED
### Added
- **Back up database only** button next to Back up now, on a stack that makes full backups. It runs `odoo-backup`
  with `BACKUP_FILESTORE=false` and `BACKUP_PREFIX=<prefix>dbonly-`, so the zip holds `dump.sql` and no
  `filestore/`, and is a group of its own (`dbonly-<db>_<stamp>.zip`). Needs odoo-backup-service 0.2 or newer:
  below that the button is off and says why. A stack that is database-only already does not show it.
- `Job.database_only` (migration `0005`).
- Contract tests for the button (`DatabaseOnlyButtonContractTests`).

### Changed
- Health, the Status card's "Last backup" and the instance list read the newest backup that is not a
  database-only one, so a manual database-only zip cannot hide a stack whose schedule stopped. A stack with
  only database-only zips shows "No full backup yet".
- `ODOO_BACKUP_VERSION` joins the env allowlist (not sensitive). Design: `docs/decisions.md` D14.

## [0.3.0] - 2026-11-07
### Added
- "DB saja" label on stacks running with `BACKUP_FILESTORE=false` (instance list and instance page).
- Contract tests for a database-only stack: Back up now makes a zip with `dump.sql` only, and restoring it
  gives the database and an empty filestore folder. They need odoo-backup-service 0.2.x: on a 0.1 image
  they fail.

### Changed
- `ENV_ALLOWLIST` includes `BACKUP_FILESTORE` (not sensitive).
- Contract documented for odoo-backup-service 0.2.x (`docs/contract.md`). The default
  `CONTRACT_BACKUP_IMAGE` is `pg17-0.2` (odoo-backup-service 0.2.0 is released).

## [0.2.0] - 2026-11-01
- Delete backup files (superuser only).
