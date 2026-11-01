"""The delete script, run in a real shell with /backups pointed at a temporary folder.

The unit tests of the job use a stand-in for the container, and the contract tests use the real backup container
(they need Docker and a disposable host). This sits between them: the shell logic itself (name checks,
all-or-nothing, the lock, the exit codes), with nothing but `sh` and `flock`.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

from django.test import SimpleTestCase

from panel import jobs

START = {
    "shop_20200101_000000.zip",
    "shop_20200102_000000.zip",
    "shop_20200103_000000.zip",
    ".hidden.zip",
    "notes.txt",
    ".odoo-backup.lock",
}


@unittest.skipUnless(shutil.which("sh") and shutil.which("flock"), "needs sh and flock")
class DeleteScriptTests(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.backups = os.path.join(self.root, "backups")
        os.mkdir(self.backups)
        for name in START:
            with open(os.path.join(self.backups, name), "w") as f:
                f.write("x")

    def run_script(self, files):
        script = jobs.DELETE_SCRIPT.replace("/backups", self.backups)
        return subprocess.run(
            ["sh", "-c", script], check=False, env={**os.environ, "FILES": files}, capture_output=True, text=True
        )

    def left(self):
        return set(os.listdir(self.backups))

    def test_valid_names_are_removed_and_nothing_else(self):
        result = self.run_script("shop_20200101_000000.zip shop_20200102_000000.zip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split("\n")[:2], ["deleted shop_20200101_000000.zip", "deleted shop_20200102_000000.zip"]
        )
        self.assertEqual(self.left(), START - {"shop_20200101_000000.zip", "shop_20200102_000000.zip"})

    def test_a_bad_name_is_refused_with_exit_6_and_nothing_is_removed(self):
        for files in (
            "../etc/hostname",  # a path
            ".hidden.zip",  # a leading dot
            ".odoo-backup.lock",  # the lock file can never be named
            "notes.txt",  # not a zip
            "shop_*.zip",  # a pattern, not a name
            "shop_20200101_000000.zip ../x.zip",  # a good name next to a bad one
            "a;b.zip",
            "a$(id).zip",
        ):
            with self.subTest(files):
                result = self.run_script(files)
                self.assertEqual(result.returncode, 6, result.stderr)
                self.assertIn("invalid name", result.stderr)
                self.assertEqual(self.left(), START)

    def test_a_missing_file_stops_everything_with_exit_5(self):
        result = self.run_script("shop_20200101_000000.zip missing_20200101_000000.zip")
        self.assertEqual(result.returncode, 5)
        self.assertIn("missing: missing_20200101_000000.zip", result.stderr)
        self.assertEqual(self.left(), START)  # the good name was not removed either

    def test_a_held_lock_exits_4_and_removes_nothing(self):
        holder = subprocess.Popen(["flock", "-n", os.path.join(self.backups, ".odoo-backup.lock"), "sleep", "5"])
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        result = self.run_script("shop_20200101_000000.zip")
        self.assertEqual(result.returncode, 4)
        self.assertEqual(self.left(), START)

    def test_the_script_text_holds_no_file_name_only_the_variable(self):
        self.assertNotIn("shop", jobs.DELETE_SCRIPT)
        self.assertIn("$FILES", jobs.DELETE_SCRIPT)
