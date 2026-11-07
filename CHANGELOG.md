# Changelog

## [0.3.0] - UNRELEASED
### Added
- "DB saja" label on stacks running with `BACKUP_FILESTORE=false` (instance list and instance page).
- Contract tests for a database-only stack: Back up now makes a zip with `dump.sql` only, and restoring it
  gives the database and an empty filestore folder. They need odoo-backup-service 0.2.x: on a 0.1 image
  they fail.

### Changed
- `ENV_ALLOWLIST` includes `BACKUP_FILESTORE` (not sensitive).
- Contract documented for odoo-backup-service 0.2.x (`docs/contract.md`). The default
  `CONTRACT_BACKUP_IMAGE` is `pg17-0.2` (odoo-backup-service 0.2.0 is released).

## [0.2.0] - released
- Delete backup files (superuser only).
