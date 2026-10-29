# Contributing

Rules for anyone who changes this code, human or tool. They exist because the panel holds root-level
access to every connected server and runs commands against production data.

- Functional and technical specification: `docs/spec.md`. Decisions and their reasons: `docs/decisions.md`.
  Contract with odoo-backup-service: `docs/contract.md`. Pages and UI rules: `docs/ui.md` and
  `design-system/odoo-backup-panel/MASTER.md`. Tests and what is still untested: `docs/testing.md`.
- Keep changes small and reviewable; every change should trace to a documented need. Before a larger
  piece of work, write down what will be verified and how, and do not move on before that passes.
- Assumptions in `docs/assumptions.md` that are not verified yet must be checked first. If one fails,
  stop and discuss; do not guess.
- All Docker access goes through `panel/dockerops.py` only, for local endpoints as well as Portainer.
- Never store, display, or log `PGPASSWORD`, `PORTAINER_API_KEY`, or a container's full environment.
  The `inspect` response does carry the full environment: filter it to the allowlist right there
  (`docs/contract.md`) and never pass the raw object on.
- Arguments to the shell always go through environment variables, never through f-strings.
- A restore must never overwrite or delete an existing database or folder. The panel has no delete
  command at all; keep it that way.
- Do not change the odoo-backup-service repo from here.
- This repo is public: never put real server, stack, database, user, or domain names in any file,
  including test data and screenshots. Examples use `vps-1`, `acme-staging-backup`, `acme_staging`,
  `admin`.
- Do not add dependencies beyond `docs/spec.md` section 6 without discussing it first. Dev-only tools
  (Playwright for `scripts/ui_shots.py`) stay out of `requirements.txt` and the image.
- Tests first for input validation and parsing (fake Docker client in `panel/tests/fakes.py`);
  anything that touches a real Docker belongs in the contract tests. Before handing a change over:
  `ruff check`, `ruff format --check`, `manage.py test`, `scripts/check_contrast.py`.
