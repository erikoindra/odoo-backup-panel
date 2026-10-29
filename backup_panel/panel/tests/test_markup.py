"""Every page and fragment, rendered, checked for the mistakes that only show up in a browser."""

import pathlib
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import Resolver404, resolve, reverse

from panel import checks, dockerops
from panel.dockerops import Backup, BackupListing, Endpoint, Server
from panel.models import Job
from panel.tests.fakes import make_instance

VOID = {"meta", "link", "br", "hr", "img", "input", "path", "ellipse", "rect", "line", "circle", "source", "base"}
ENDPOINT = Endpoint("vps-1", "portainer", "https://p.example.com:443/api/endpoints/4/docker", "k")
SLUG = "acme-staging-backup"
ZIP = "acme_staging_20261028_072000.zip"
LISTING = BackupListing([Backup(ZIP, 6740230, datetime(2026, 10, 28, 0, 20, tzinfo=UTC))], datetime.now(UTC))


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.problems, self.stack, self.ids, self.refs, self.links, self.labels = [], [], [], [], [], []
        self.text_in, self.controls = [], []
        self.feed(html)
        self.close()
        if self.stack:
            self.problems.append(f"unclosed: {self.stack}")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in VOID:
            self.stack.append(tag)
        if "id" in a:
            self.ids.append(a["id"])
        for key in ("aria-describedby", "aria-labelledby", "for"):
            for ref in (a.get(key) or "").split():
                self.refs.append((key, ref))
        if tag == "a" and (a.get("href") or "").startswith("#") and len(a["href"]) > 1:
            self.refs.append(("href", a["href"][1:]))
        for key in ("hx-get", "href", "action"):
            if a.get(key, "").startswith("/") and not a[key].startswith("/static/"):
                self.links.append(a[key])
        if tag == "input" and a.get("type") not in ("hidden", "submit"):
            self.controls.append(("input", a))
        if tag in ("a", "button"):
            self.text_in.append([tag, a, ""])
        elif tag == "span" and "sr-only" in (a.get("class") or ""):
            pass

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> closes {self.stack[-1:] or 'nothing'}")
            if tag in self.stack:
                while self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()
        if tag in ("a", "button") and self.text_in:
            t, a, text = self.text_in.pop()
            if not text.strip() and not a.get("aria-label"):
                self.problems.append(f"<{t}> without text or aria-label: {a}")

    def handle_data(self, data):
        for item in self.text_in:
            item[2] += data


def check(case, html, name):
    page = Page(html)
    case.assertEqual(page.problems, [], name)
    dupes = {i for i in page.ids if page.ids.count(i) > 1}
    case.assertEqual(dupes, set(), f"{name}: duplicate ids")
    missing = [r for r in page.refs if r[1] not in page.ids]
    case.assertEqual(missing, [], f"{name}: references to ids that do not exist")
    for link in page.links:
        try:
            resolve(link.split("?")[0])
        except Resolver404:
            case.fail(f"{name}: {link} matches no URL")
    for _, attrs in page.controls:
        labelled = attrs.get("id") in {r[1] for r in page.refs if r[0] == "for"} or attrs.get("aria-label")
        case.assertTrue(labelled or "label" in html, f"{name}: input without a label: {attrs}")
    return page


@override_settings(PORTAINER_URL="", PORTAINER_API_KEY="", DOCKER_ENDPOINTS=[])
class MarkupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("admin", password="x" * 14)

    def setUp(self):
        self.server = Server("vps-1", "portainer", [make_instance()])
        for target, kwargs in (
            ("find_endpoint", {"side_effect": lambda n: ENDPOINT if n == "vps-1" else None}),
            ("look_at", {"side_effect": lambda *a, **k: self.server}),
            ("discover_all", {"side_effect": lambda *a, **k: [self.server]}),
            ("list_backups_cached", {"return_value": LISTING}),
        ):
            patcher = patch.object(dockerops, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_login(self.user)

    def job(self, **fields):
        base = {
            "kind": "backup",
            "endpoint": "vps-1",
            "instance_slug": SLUG,
            "container_name": "c",
            "created_by": self.user,
        }
        return Job.objects.create(**{**base, **fields})

    def test_every_page_and_fragment_is_well_formed(self):
        running = self.job(status="running", started_at=datetime.now(UTC), log="10:00:00 mulai\n")
        running.steps = [
            {"name": "A", "status": "done", "at": ""},
            {"name": "B", "status": "running", "at": ""},
            {"name": "C", "status": "todo", "at": ""},
        ]
        running.save()
        failed = self.job(
            kind="restore",
            status="failed",
            target_db="acme_copy",
            filename=ZIP,
            summary="Database acme_copy already exists.",
        )
        failed.steps = [{"name": "A", "status": "failed", "at": ""}]
        failed.save()
        done = self.job(status="done", summary="Backup finished.")
        queued = self.job()
        download = self.job(kind="download", status="done", filename=ZIP)
        pages = {
            "instance list": reverse("instance_list"),
            "detail": reverse("instance_detail", args=["vps-1", SLUG]),
            "backups fragment": reverse("backups_partial", args=["vps-1", SLUG]),
            "summary fragment": reverse("summary_partial", args=["vps-1", SLUG]),
            "restore wizard": reverse("restore", args=["vps-1", SLUG]),
            "job list": reverse("job_list"),
        }
        for label, job in (
            ("running", running),
            ("failed", failed),
            ("done", done),
            ("queued", queued),
            ("download", download),
        ):
            pages[f"job {label}"] = reverse("job_status", args=[job.pk])
        for name, url in pages.items():
            with self.subTest(name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, name)
                check(self, response.content.decode(), name)
                if name.startswith("job "):  # the polled fragment is the same panel without the page
                    fragment = self.client.get(url, headers={"HX-Request": "true"})
                    check(self, fragment.content.decode(), f"{name} fragment")

    def test_the_restore_check_fragment_is_well_formed(self):
        items = [checks.Check(True, "Present"), checks.Check(False, "Missing", "Because <b>")]
        with patch.object(checks, "run_checks", return_value=items):
            response = self.client.post(reverse("restore_check", args=["vps-1", SLUG]), {"new_db": "acme_copy"})
        check(self, response.content.decode(), "restore checks")
        self.assertIn("Because &lt;b&gt;", response.content.decode())

    def test_the_stopped_empty_and_error_states_are_well_formed(self):
        self.server = Server("vps-1", "portainer", [make_instance(status="exited")])
        check(
            self, self.client.get(reverse("instance_detail", args=["vps-1", SLUG])).content.decode(), "stopped detail"
        )
        self.server = Server("vps-1", "portainer", [], error="timeout")
        check(self, self.client.get(reverse("instance_list")).content.decode(), "unreachable list")
        check(
            self,
            self.client.get(reverse("instance_detail", args=["vps-1", SLUG])).content.decode(),
            "unreachable detail",
        )
        self.server = Server("vps-1", "portainer", [make_instance()])
        with patch.object(dockerops, "list_backups_cached", return_value=BackupListing([], datetime.now(UTC))):
            check(
                self, self.client.get(reverse("backups_partial", args=["vps-1", SLUG])).content.decode(), "empty table"
            )
        bad = self.client.post(
            reverse("restore", args=["vps-1", SLUG]), {"filename": ZIP, "new_db": "Bad Name", "confirm": "x"}
        )
        self.assertEqual(bad.status_code, 400)
        check(self, bad.content.decode(), "restore wizard with errors")
        check(
            self,
            self.client.post(reverse("backup_now", args=["vps-1", SLUG])).content.decode() or "<p></p>",
            "redirect body",
        )

    def test_login_and_lockout_pages_are_well_formed(self):
        anonymous = Client()
        check(self, anonymous.get(reverse("login")).content.decode(), "login")
        check(
            self,
            anonymous.post(reverse("login"), {"username": "admin", "password": "wrong"}).content.decode(),
            "login error",
        )
        with override_settings(AXES_FAILURE_LIMIT=1):
            anonymous.post(reverse("login"), {"username": "admin", "password": "wrong"})
            locked = anonymous.post(reverse("login"), {"username": "admin", "password": "wrong"})
        self.assertEqual(locked.status_code, 429)
        check(self, locked.content.decode(), "locked")

    def test_everything_the_script_looks_up_exists_in_the_pages(self):
        pages = "".join(
            self.client.get(url).content.decode()
            for url in (
                reverse("instance_list"),
                reverse("instance_detail", args=["vps-1", SLUG]),
                reverse("restore", args=["vps-1", SLUG]),
            )
        )
        js = (pathlib.Path(dockerops.__file__).parent / "static/panel/app.js").read_text()
        ids = set(re.findall(r"getElementById\('([^']+)'\)", js)) | set(re.findall(r"setError\('([^']+)'", js))
        self.assertTrue({"wizard", "checks", "run-checks", "toast", "result-count"} <= ids)
        for needle in sorted(ids):
            self.assertIn(f'id="{needle}"', pages, f"app.js looks for #{needle}")
        for attribute in (
            "data-search",
            "data-group",
            "data-row",
            "data-level",
            "data-go",
            "data-sum",
            "data-start",
            "data-needs-checks",
            "data-suggest",
            "data-download",
            "data-dismiss",
            "data-since",
        ):
            self.assertIn(attribute, js + pages, attribute)
        for field in ("new_db", "confirm", "neutralize", "filename"):
            self.assertIn(f'name="{field}"', pages)

    def test_every_template_include_exists(self):
        # {% include %} of a missing file only fails when that branch renders: check them all, statically.
        root = pathlib.Path(dockerops.__file__).parent / "templates"
        for template in root.rglob("*.html"):
            for name in re.findall(r'{%\s*(?:include|extends)\s+"([^"]+)"', template.read_text()):
                self.assertTrue((root / name).is_file(), f"{template.name} refers to missing {name}")
