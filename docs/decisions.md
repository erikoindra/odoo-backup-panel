# Decisions

Format: short context, decision, reason, consequences. New decisions are added at the bottom.

## D1. Design system: mockup tokens kept

> **Superseded by D10** (palette and fonts replaced in the 2026-10-29 redesign). The structure of the
> decision (Minimalism & Swiss style, density 8, contrast checked by script) still applies.

- **Context**: ui-ux-pro-max (`--design-system`, density 8) recommended the *Minimalism & Swiss*
  style with a navy/green palette (light-first) and the Fira Code/Fira Sans fonts.
- **Decision**: style and density taken, palette and fonts not. Keep the tokens in
  `mockups/styles.css` (since removed; slate/sky, dark-first, light follows the OS setting), Inter for the UI,
  JetBrains Mono for file names, databases, and logs.
- **Reason**: the generator's palette is generic and no clearer for status
  (running/stopped/failed). The mockup tokens are already consistent across pages.
- **Consequences**: four tokens were fixed because their contrast was below 4.5:1 (checked with
  `scripts/check_contrast.py`):

  | Token | Old | New |
  |---|---|---|
  | dark `--faint` | `#64748B` (3.07–4.24) | `#8794AA` |
  | dark `--danger-text` (new) | error text used `#E11D48` (3.80) | `#FB7185` |
  | light `--faint` | `#64748B` (4.34 on surface-2) | `#5B6B80` |
  | light `--warning` | `#B45309` (4.40 in the banner) | `#92400E` |

## D2. Separate project, companion to odoo-backup-service

- **Decision**: own repo, own image. The odoo-backup-service repo is not changed.
- **Reason**: the dependency is a runtime contract through Docker (labels, mounts, env, commands,
  zip names), not code. The stacks, release cycles, and security models differ: backup-service
  deliberately has no docker.sock, while the panel needs the Docker API.
- **Consequences**: the contract is written down in `docs/contract.md` and guarded by a contract
  test in CI against the real backup-service image.

## D3. No landing page

- **Decision**: `/` redirects to `/instances/`, which requires login.
- **Reason**: the panel is for admins only and is equivalent to root access on the host. A public
  page adds no function and only tells people that this panel exists.

## D4. Access through a public domain + IP allowlist

- **Decision**: nginx (on another server) with HTTPS and per-IP `allow`/`deny`, in front of the
  Django login + django-axes.
- **Consequences**:
  - the panel port on the host is restricted through the cloud firewall, because Docker ports
    bypass ufw;
  - django-axes reads the real client IP from `X-Forwarded-For` (one trusted proxy), so a
    lockout does not lock the nginx IP and everyone with it.

## D5. Multi-host: a list of Docker API endpoints

- **Decision**: the panel reads the list of endpoints from env. An endpoint can be local through
  a socket-proxy (`DOCKER_ENDPOINTS`), or any Portainer CE environment the panel token can reach
  (`PORTAINER_URL` + `PORTAINER_API_KEY`). One code path, `dockerops`; only `base_url` and the
  auth header differ per endpoint.
- **Reason**: Portainer CE + agent is already installed on every server, so no new agent, new
  port, or socket-proxy per host is needed.
- **Consequences**:
  - the panel token has the same power as its Portainer user in every environment it can reach.
    Use a dedicated Portainer user that is only given the environments with backups;
  - exec and helper containers through the Portainer proxy must be proven first (spikes S1–S4 in
    `docs/assumptions.md`). If that fails, the fallback is a socket-proxy per host over a private
    network.

## D6. Restore logic lives in the panel (MVP)

- **Context**: odoo-backup-service already has `tools/restore.sh`, but that script needs the docker
  CLI on the host and the names of the Odoo/DB containers.
- **Decision**: the panel runs the restore itself through the Docker API: exec in the backup
  container for the database, a helper container for the filestore (spec 5.3).
- **Consequences**: there are two restore implementations. Phase 2 candidate: move the restore
  command into the backup-service image so both use the same code. That is a separate decision in
  the backup-service repo.

## D7. Exec without attach

- **Context**: Step 2 spike (2026-10-28). Portainer sits behind Cloudflare. An attached `docker exec`
  needs `Connection: Upgrade` + `Upgrade: tcp`; Cloudflare only forwards WebSocket upgrades, so
  `POST /exec/<id>/start` is answered with 502. Other requests (GET, plain POST, streaming logs,
  archive) go through.
- **Decision**: every command in a container runs without attach: exec with `Detach`, output
  written to a file in the container's `/tmp`, status read through `exec_inspect` (polling),
  output fetched through `get_archive`, then the file is deleted (`run_detached`, spec section 4).
- **Reason**: only plain requests are used, so it does not depend on whether the proxy in front
  of Portainer forwards upgrades (Cloudflare, nginx, or direct). Proven in two environments: exit
  code, separate stdout/stderr, env, 300 KB of output, zip stream with the same sha256.
- **Consequences**:
  - output is only readable after the command finishes (good enough: the job page polls every
    2 seconds);
  - there are temporary files in the backup container's `/tmp`; a delete command is sent at the
    end of every command (not awaited) and the rest is cleaned up when the worker starts;
  - download through Cloudflare proven correct for 6.6 and 17.7 MB zips (same sha256, Step 5);
    zips of hundreds of MB are untested. Measured speed 0.1-1 MB/s from the developer's laptop,
    the same as Portainer's static assets, so that is a network limit, not code.

## D8. Helper container mounts tmpfs at /backups

- **Context**: the backup-service image declares `VOLUME /backups`. `containers.run(remove=True)` in the
  Docker SDK removes the container but **not** its anonymous volumes, so every helper without a mount at
  `/backups` left one empty anonymous volume behind (seen in the first contract test run: 38 volumes,
  up from 8; the Step 2 spike on the real server also left a few).
- **Decision**: `run_helper` always mounts a `tmpfs` at `/backups`. One line, no extra API calls.
  Guarded by a unit test and a contract test that count new anonymous volumes.
- **Consequences**: anonymous volumes that already piled up on the server from spikes and tests before
  the fix must be cleaned up manually; the panel has no delete command of any kind.

## D9. Restore holds the backup lock; no automatic deletion

- **Decision**: the database step holds the same `flock` as `odoo-backup`, and the panel never
  deletes a database or folder, not even when a restore fails halfway.
- **Reason**: without the lock, retention could delete a zip that is being read; deleting automatically
  would mean the panel can destroy something that turns out to be in use already.
- **Consequences**: a large restore that collides with the scheduled backup hour makes that night's
  backup fail; leftovers of a failed restore are cleaned up manually (the job message says exactly what
  exists). The guard hook in the development environment also blocks database-deleting commands, which
  is in line with this decision.

## D10. Redesign: warm palette + teal accent, IBM Plex Sans and JetBrains Mono

- **Context**: the owner made three new mockups outside the repo (instance list, detail, restore wizard)
  with two different palettes. The names in them belong to real servers, so those mockups do **not** go
  into the repo (this repo is public); only the design decisions do. The old prototype folder `mockups/`
  was removed from the repo after this redesign.
- **Decision**: the palettes are merged. Warm neutral surfaces and page structure from the new mockups;
  **teal** (`#0F6B5F` light, `#2DD4BF` dark) for actions, focus, active navigation, and running processes;
  **amber only for warnings and Stale**; green for healthy, red-orange for failed. Fonts IBM Plex Sans
  (UI) and JetBrains Mono (file names, databases, logs), both SIL OFL, self-hosted (variable woff2,
  latin subset). Brand "Backup Console"; navigation Instances and Activity. Dark mode follows the OS, no
  toggle button.
- **Reason**: both mockups used the same colour for accent and warning; merged as-is, the primary
  button would look like a warning. Separating them keeps the meaning of each colour.
- **Consequences**: `scripts/check_contrast.py` now reads the tokens in `app.css` (24 pairs per theme, light
  and dark, text 4.5:1, control boundary 3:1) and all of them pass. Faint text got more contrast than
  in the mockups. The developer has not yet looked at the result in a browser (see `docs/testing.md`).

## D11. Neutralize as an option during restore, run in the Odoo container

- **Decision**: the restore wizard has a "Neutralize database" switch. When on, after the dump is loaded
  and the filestore copied, the panel runs `odoo neutralize -d <db>` **in the Odoo container of that
  stack**, using the `HOST`/`USER`/`PASSWORD` variables already present in that container. The Odoo
  container is found through the named volume mounted at `/var/lib/odoo` (not by name); there must be
  exactly one and it must be running.
- **Reason**: a copy of production opened without neutralizing can send mail and run cron jobs against
  the real world. The command needs the same Odoo code and configuration as that database, so it is not
  run in the backup container. The panel never reads or forwards the Odoo password; it only runs a fixed
  script inside that container, with the database name as a variable.
- **Consequences**:
  - the Odoo container is looked up **before** anything is created; if it is not found, the job fails
    with "Nothing was changed";
  - if neutralize fails after the restore, the job fails with a firm message that the copy is NOT
    neutralized (nothing is deleted, in line with D9);
  - a data volume that is a bind mount (not a named volume) is not supported for neutralize;
  - without the switch, the job summary reminds you to run `odoo neutralize` yourself.
