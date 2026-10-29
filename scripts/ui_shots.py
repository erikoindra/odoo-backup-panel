"""Render every page of the panel in headless Chromium with Docker faked, save screenshots locally, and
check the things only a browser can show: app.js really runs, no horizontal page scroll at any width, the
wizard's gates, the toast, the elapsed timer, the status tally.

Optional developer tool (docs/testing.md section 2b). Needs Playwright, which is NOT a project dependency:
    ../.venv/bin/pip install playwright && ../.venv/bin/python -m playwright install chromium
Run from backup_panel/:
    DJANGO_DEBUG=1 ../.venv/bin/python ../scripts/ui_shots.py
Output: $UI_SHOTS_DIR (default ~/odoo-backup-panel-screenshots)/<page>-<width>-<scheme>.png, a report on
stdout, exit 1 when something is wrong. Everything shown is generic sample data.
"""

import os
import pathlib
import sys
import types
import unittest
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from unittest.mock import patch

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"  # the ORM is used while Playwright's loop is running
import django

django.setup()
import logging

logging.basicConfig(level=logging.ERROR)  # tracebacks of 500s from the live server go to stderr
from django.db import connections

# a file, not :memory:, so the live server's threads do not share one sqlite connection
connections["default"].settings_dict["TEST"]["NAME"] = str(pathlib.Path(os.getcwd()) / "ui-shots-test.sqlite3")

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.test.utils import get_runner
from playwright.sync_api import sync_playwright

from panel import checks, dockerops
from panel.dockerops import Backup, BackupListing, Endpoint, Server
from panel.models import Job
from panel.tests.fakes import make_instance

OUT = pathlib.Path(os.environ.get("UI_SHOTS_DIR", pathlib.Path.home() / "odoo-backup-panel-screenshots"))
WIDTHS = (375, 768, 1024, 1440)
SCHEMES = ("light", "dark")
NOW = datetime.now(UTC)
PASSWORD = "x" * 14


def zips(db, ages_hours):
    return [
        Backup(
            f"{db}_{(NOW - timedelta(hours=h)).strftime('%Y%m%d_%H%M%S')}.zip",
            6_740_230 + i * 9_000,
            NOW - timedelta(hours=h),
        )
        for i, h in enumerate(ages_hours)
    ]


LISTINGS = {
    "acme-staging-backup": BackupListing(zips("acme_staging", [6, 30, 54, 78, 102]), NOW),
    "acme-prod-backup": BackupListing(zips("acme_prod", [60, 84]), NOW),  # stale for a daily schedule
    "demo-shop-backup": BackupListing([], NOW),
    "wiki-backup": BackupListing(zips("wiki", [2]), NOW),
}
ENDPOINTS = {
    "vps-1": Endpoint("vps-1", "portainer", "https://p.example.com:443/api/endpoints/4/docker", "k"),
    "vps-2": Endpoint("vps-2", "portainer", "https://p.example.com:443/api/endpoints/5/docker", "k"),
    "vps-3": Endpoint("vps-3", "docker", "tcp://10.0.0.3:2375"),
}
SERVERS = {
    "vps-1": Server(
        "vps-1",
        "portainer",
        [
            make_instance(),
            make_instance("acme-prod-backup", database="acme_prod", endpoint="vps-1", RETENTION_DAYS="14"),
            make_instance("demo-shop-backup", database="demo_shop", endpoint="vps-1", CRON_SCHEDULE="0 */6 * * *"),
        ],
    ),
    "vps-2": Server(
        "vps-2",
        "portainer",
        [
            make_instance("wiki-backup", database="wiki", endpoint="vps-2"),
            make_instance("old-erp-backup", status="exited", database="old_erp", endpoint="vps-2"),
        ],
    ),
    "vps-3": Server("vps-3", "docker", [], error="Could not connect to tcp://10.0.0.3:2375: timeout after 20 seconds."),
}


class FakeStream:
    name, size = "x.zip", 4

    def chunks(self):
        yield b"PK\x05\x06"


class Shots(StaticLiveServerTestCase):
    problems: ClassVar[list] = []

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        OUT.mkdir(exist_ok=True)
        for old in OUT.glob("*.png"):
            old.unlink()

    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password=PASSWORD)
        for target, kwargs in (
            ("find_endpoint", {"side_effect": lambda n: ENDPOINTS.get(n)}),
            ("look_at", {"side_effect": lambda e, **k: SERVERS[e.name]}),
            ("discover_all", {"side_effect": lambda **k: list(SERVERS.values())}),
            ("list_backups_cached", {"side_effect": lambda e, i, **k: LISTINGS[i.slug]}),
            ("open_backup_stream", {"return_value": FakeStream()}),
            ("docker_client", {"return_value": types.SimpleNamespace(api=None)}),
            ("is_configured", {"return_value": True}),
        ):
            p = patch.object(dockerops, target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.check_calls = []

        def run_checks(endpoint, instance, db, neutralize):
            self.check_calls.append((db, neutralize))
            ok = db not in ("acme_staging", "acme_prod")
            items = [
                checks.Check(ok, f"Database {db} does not exist yet", "" if ok else f"Database {db} already exists."),
                checks.Check(True, f"Filestore folder {db} does not exist yet"),
                checks.Check(True, "No other job is running for this instance"),
            ]
            if neutralize:
                items.append(checks.Check(True, "Odoo container found: acme-odoo-1 (odoo:19.0)"))
            return items

        p = patch.object(checks, "run_checks", side_effect=run_checks)
        p.start()
        self.addCleanup(p.stop)

    def job(self, **fields):
        base = {
            "kind": "backup",
            "endpoint": "vps-1",
            "instance_slug": "acme-staging-backup",
            "container_name": "acme-staging-backup-odoo-backup-1",
            "created_by": self.user,
        }
        return Job.objects.create(**{**base, **fields})

    # --- browser helpers ---

    def login(self, page):
        page.goto(self.live_server_url + "/login/")
        page.fill("#id_username", "admin")
        page.fill("#id_password", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url("**/instances/")

    def shot(self, page, name, width, scheme):
        page.screenshot(path=str(OUT / f"{name}-{width}-{scheme}.png"), full_page=True)
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        if overflow > 0:
            self.problems.append(f"{name} @ {width} {scheme}: horizontal page scroll of {overflow}px")

    def test_everything(self):
        running = self.job(
            status="running",
            started_at=NOW - timedelta(seconds=41),
            log="[10:00:01] start\n[10:00:02] [acme_staging] dumping...\n",
        )
        running.steps = [
            {"name": "Check filestore folder", "status": "done", "at": ""},
            {"name": "Create database and load dump.sql", "status": "running", "at": ""},
            {"name": "Copy filestore", "status": "todo", "at": ""},
            {"name": "Neutralize", "status": "todo", "at": ""},
            {"name": "Done", "status": "todo", "at": ""},
        ]
        running.kind, running.filename, running.target_db, running.neutralize = (
            "restore",
            LISTINGS["acme-staging-backup"].backups[0].name,
            "acme_staging_restore_20261029",
            True,
        )
        running.save()
        failed = self.job(
            instance_slug="acme-prod-backup",
            container_name="acme-prod-backup-odoo-backup-1",
            kind="restore",
            status="failed",
            target_db="acme_prod_copy",
            filename=LISTINGS["acme-prod-backup"].backups[0].name,
            summary="Database acme_prod_copy already exists. Nothing was changed.",
            started_at=NOW - timedelta(minutes=3),
            finished_at=NOW - timedelta(minutes=2),
            log="[09:12:00] filestore check: code 0\n[09:12:03] psql: database exists\n",
        )
        failed.steps = [
            {"name": "Check filestore folder", "status": "done", "at": ""},
            {"name": "Create database and load dump.sql", "status": "failed", "at": ""},
            {"name": "Copy filestore", "status": "todo", "at": ""},
            {"name": "Done", "status": "todo", "at": ""},
        ]
        failed.save()
        self.job(
            instance_slug="wiki-backup",
            container_name="wiki-backup-odoo-backup-1",
            status="done",
            summary="Backup finished.",
            started_at=NOW - timedelta(hours=1),
            finished_at=NOW - timedelta(hours=1) + timedelta(seconds=12),
            log="[08:00:00] finished\n",
        )
        self.job(
            kind="download",
            status="done",
            filename=LISTINGS["acme-staging-backup"].backups[1].name,
            started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=2),
            log="6740230 bytes sent.",
        )

        errors = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for scheme in SCHEMES:
                for width in WIDTHS:
                    ctx = browser.new_context(
                        viewport={"width": width, "height": 900}, color_scheme=scheme, device_scale_factor=1
                    )
                    page = ctx.new_page()
                    where = f"{scheme} {width}"
                    page.on("pageerror", lambda e, where=where: errors.append(f"{where}: {e}"))
                    page.on(
                        "console",
                        lambda m, where=where: (
                            errors.append(f"{where} console.{m.type}: {m.text} @ {m.location.get('url', '')}")
                            if m.type == "error" and "nope.zip" not in m.location.get("url", "")
                            else None
                        ),
                    )
                    self.walk(page, width, scheme)
                    ctx.close()
            browser.close()
        self.problems.extend(errors)
        print("\n".join(self.problems) if self.problems else "no problems found")
        print(f"{len(list(OUT.glob('*.png')))} screenshots in {OUT}")
        self.assertEqual(self.problems, [])

    def walk(self, page, width, scheme):
        base = self.live_server_url
        page.goto(base + "/login/")
        self.shot(page, "01-login", width, scheme)
        page.fill("#id_username", "admin")
        page.fill("#id_password", "wrong")
        page.click("button[type=submit]")
        page.wait_for_selector("#login-error:not(:empty)")
        self.shot(page, "01b-login-error", width, scheme)
        self.login(page)

        # instance list: wait until every lazy row is replaced
        page.wait_for_function(
            "document.querySelectorAll('[data-row][data-level=loading]').length === 0", timeout=15000
        )
        page.wait_for_function(
            "(t => t.includes('fresh') && !t.includes('loading'))(document.getElementById('health-count').textContent)"
        )
        tally = page.inner_text("#health-count")
        expected = ("2 fresh", "1 stale", "1 no backups", "1 stopped")
        for part in expected:
            if part not in tally:
                self.problems.append(f"list @ {width} {scheme}: tally '{tally}' lacks '{part}'")
        self.shot(page, "02-instances", width, scheme)
        page.fill("[data-search]", "wiki")
        page.wait_for_function("document.getElementById('result-count').textContent.startsWith('1 instance')")
        self.shot(page, "02b-instances-search", width, scheme)
        page.fill("[data-search]", "")

        # detail with a running job + loaded table + status card swapped in
        page.goto(base + "/e/vps-1/acme-staging-backup/")
        page.wait_for_selector("#backups table")
        page.wait_for_function("document.querySelector('#side-status .status')?.textContent.includes('Fresh')")
        page.wait_for_function(
            "(document.querySelector('[data-elapsed]')?.textContent ?? '41 dtk') !== '41 dtk'", timeout=5000
        )
        self.shot(page, "03-detail-running", width, scheme)
        page.click("[data-download]")
        page.wait_for_selector("#toast:not([hidden])")
        self.shot(page, "03b-detail-toast", width, scheme)

        page.goto(base + "/e/vps-1/acme-prod-backup/")
        page.wait_for_selector("#backups table")
        page.wait_for_function("document.querySelector('#side-status .status')?.textContent.includes('Stale')")
        self.shot(page, "03c-detail-failed-stale", width, scheme)
        page.click("[data-dismiss]")
        if page.query_selector("#job-panel"):
            self.problems.append(f"detail @ {width} {scheme}: Tutup did not remove the job panel")

        page.goto(base + "/e/vps-1/demo-shop-backup/")
        page.wait_for_selector("#backups .emptystate")
        self.shot(page, "03d-detail-empty", width, scheme)
        page.goto(base + "/e/vps-2/old-erp-backup/")
        self.shot(page, "03e-detail-stopped", width, scheme)
        page.goto(base + "/e/vps-3/nope/")
        self.shot(page, "03f-detail-unreachable", width, scheme)

        # wizard: the stack with a running job only shows the warning; the real walk is on another stack
        page.goto(base + "/e/vps-1/acme-staging-backup/restore/")
        page.wait_for_selector("#wizard[data-js]")
        self.shot(page, "04-wizard-busy", width, scheme)
        chosen = LISTINGS["acme-prod-backup"].backups[1].name
        page.goto(base + "/e/vps-1/acme-prod-backup/restore/?file=" + chosen)
        page.wait_for_selector("#wizard[data-js]")
        if not page.is_checked(f"input[value='{chosen}']"):
            self.problems.append("wizard: ?file= was not preselected")
        self.shot(page, "04-wizard-1", width, scheme)
        page.click("[data-go='2']")
        page.wait_for_selector("#checks [data-all-ok='true']")
        if page.is_disabled("[data-needs-checks]"):
            self.problems.append(f"wizard @ {width} {scheme}: Lanjut still disabled after checks passed")
        self.shot(page, "04-wizard-2", width, scheme)
        # a name that exists -> a failing check must block the next step
        page.fill("#new-db", "acme_prod")
        page.wait_for_selector("#checks [data-all-ok='false']")
        if not page.is_disabled("[data-needs-checks]"):
            self.problems.append(f"wizard @ {width} {scheme}: Lanjut enabled although a check failed")
        self.shot(page, "04-wizard-2-fail", width, scheme)
        page.fill("#new-db", "acme_prod_copy2")
        page.check("input[name=neutralize]")
        page.wait_for_function(
            "document.querySelector('#checks [data-all-ok]') && document.querySelector('#checks').textContent.includes('Odoo container')"
        )
        page.click("[data-go='3']")
        page.wait_for_selector(".step[data-step='3'][data-open='true']")
        if (
            page.inner_text("[data-sum=new_db]") != "acme_prod_copy2"
            or page.inner_text("[data-sum=neutralize]") != "yes"
        ):
            self.problems.append(f"wizard @ {width} {scheme}: summary wrong")
        self.shot(page, "04-wizard-3", width, scheme)
        page.fill("#confirm-db", "typo")
        page.click("button[type=submit].btn--danger")
        page.wait_for_selector("#confirm-error:not(:empty)")
        self.shot(page, "04-wizard-3-error", width, scheme)
        page.fill("#confirm-db", "acme_prod_copy2")
        with page.expect_navigation():
            page.click("button[type=submit].btn--danger")
        if "/jobs/" not in page.url:
            self.problems.append(f"wizard @ {width} {scheme}: submit did not reach a job page ({page.url})")
        self.shot(page, "05-job-queued", width, scheme)
        job = Job.objects.filter(target_db="acme_prod_copy2").latest("pk")
        if not (job.neutralize and job.filename == chosen):
            self.problems.append("wizard: the queued job lacks neutralize or the chosen file")
        job.delete()  # so the next viewport can queue again (one active job per stack)

        page.goto(base + f"/jobs/{Job.objects.get(status='failed').pk}/")
        self.shot(page, "05b-job-failed", width, scheme)
        page.goto(base + f"/jobs/{Job.objects.get(instance_slug='wiki-backup').pk}/")
        self.shot(page, "05c-job-done", width, scheme)
        page.goto(base + "/jobs/")
        self.shot(page, "06-aktivitas", width, scheme)
        page.goto(base + "/e/vps-1/acme-staging-backup/backups/nope.zip/download")
        self.shot(page, "07-error-page", width, scheme)
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        page.goto(base + "/instances/")
        page.keyboard.press("Tab")
        self.shot(page, "08-focus-skiplink", width, scheme)


if __name__ == "__main__":
    settings.DEBUG = False
    override = override_settings(
        PORTAINER_URL="https://p.example.com", PORTAINER_API_KEY="k", DOCKER_ENDPOINTS=[], JOB_POLL_INTERVAL=0
    )
    override.enable()
    runner = get_runner(settings)(verbosity=1)
    runner.setup_test_environment()
    old = runner.setup_databases()
    try:
        result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(Shots))
    finally:
        runner.teardown_databases(old)
        runner.teardown_test_environment()
    sys.exit(0 if result.wasSuccessful() else 1)
