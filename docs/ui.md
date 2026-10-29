# User interface

Redesign 2026-10-29 (decisions D10, D11). Tokens and components: `design-system/odoo-backup-panel/MASTER.md`.
All text is in English; server, stack, and database names in the examples are generic.

## Pages

| Page | URL | Contents |
|---|---|---|
| Instances | `/instances/` | one panel per server; one table: Instance, Database, Last backup (+age), Files (count, total size), Status, Open. Search, count per status, "Refresh status" (`?refresh=1`). |
| Detail | `/e/<server>/<stack>/` | title + meta, **Back up now**; left: job panel (if any) and the backup list (File, Size, Created, Age, Download, Restore); right: Status, Last job, "Before you restore" note. |
| Restore wizard | `/e/<server>/<stack>/restore/?file=` | 4 steps: Choose backup, Target database, Confirm, Progress (job page). |
| Job | `/jobs/<id>/` | full job panel; polls every 2 seconds while active. |
| Activity | `/jobs/` | every download, backup, restore: who, when, result. |

## Instance status

`panel/health.py` rates one instance from the `/backups` listing and the container's cron schedule (`CRON_SCHEDULE`):

| Level | Shown as | Condition |
|---|---|---|
| `fresh` | Fresh | the newest backup is no older than 1.5× the expected interval |
| `stale` | Stale + reason | older than that (the reason names the hours and the limit) |
| `empty` | No backups yet | `/backups` has no zips |
| `stopped` | Container stopped | the backup container is not running; `/backups` is not read |
| `unreadable` | Unreadable + message | Docker/Portainer failed to answer |
| `loading` | Loading | a list row that has not finished fetching its summary |

**Expected interval = the longest gap between two cron runs in the next 60 days**, not the average:
a weekday schedule is quiet over the weekend (Friday night to Monday = 72 hours) and that is not a missed
backup. An unparseable cron counts as 24 hours. Supports `*`, `*/n`, `a`, `a-b`, `a-b/n`, comma lists.

## The instance list loads in stages

The page arrives immediately with all rows. Rows of running containers load their own summary
(`hx-get` to `summary/`, `hx-swap="outerHTML"`), because reading `/backups` through Portainer takes a few
seconds per stack; rows of stopped containers are already complete. The script recounts the per-status
tally each time a row finishes. "Refresh status" reloads the page with `?refresh=1`, which bypasses the cache.

## Jobs on the detail page

Back up now returns to the detail page; the job panel appears above the backup list and polls until finished
(progress bar from the steps, elapsed time, steps, log). On completion, the backup list and the Status card
reload automatically (`HX-Trigger: backups-changed`; the Status card through an OOB swap). A finished job
panel stays visible for 15 minutes after finishing or until closed with "Dismiss".

## Restore wizard

Without script, the wizard is one long form that can still be submitted. With script:

1. **Choose backup**: a radio list (the newest selected, or `?file=`).
2. **Target database**: name (suggested `<db>_restore_<date>`, refilled when the choice changes as
   long as it has not been edited), the **Neutralize database** switch, and the checks (POST
   `restore/check/`): database does not exist yet, filestore folder does not exist yet, no other job,
   and Odoo container found (only when neutralize is on). "Next" stays disabled until all of them pass;
   changing the name or the switch reruns the checks. The checks only read; the worker checks again
   when the job runs.
3. **Confirm**: summary, explanation of what will happen, type the database name to confirm, the danger
   button "Restore now". The server re-checks everything (file in the list, valid name, confirmation matches).
4. **Progress**: the job page.

Errors from the server reopen the step that has the problem.

## Accessibility

Contrast checked by script (48 pairs). Status is always icon + word. Visible focus, skip link to the content,
`aria-current` on navigation and steps, the progress bar uses `role="progressbar"`, results appear in an
`aria-live` region, 44px targets on touch screens, `prefers-reduced-motion` disables animation, 16px inputs
on phones. Two columns on the detail page start at 1140 px; below that the side cards drop underneath.
Headless checks and their screenshots: `docs/testing.md` section 2b.
