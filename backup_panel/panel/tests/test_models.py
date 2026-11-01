from django.contrib.auth import get_user_model
from django.test import TestCase

from panel.models import Job


class JobModelTests(TestCase):
    def test_new_job_is_queued_with_no_steps(self):
        user = get_user_model().objects.create_user("admin")
        job = Job.objects.create(
            kind=Job.Kind.BACKUP,
            endpoint="vps-1",
            instance_slug="acme-staging-backup",
            container_name="acme-staging-backup-odoo-backup-1",
            created_by=user,
        )
        self.assertEqual(job.status, Job.Status.QUEUED)
        self.assertEqual(job.steps, [])
        self.assertEqual(str(job), f"#{job.pk} backup vps-1/acme-staging-backup (queued)")


class DeleteKindTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("admin")

    def job(self, kind, status=Job.Status.QUEUED, slug="acme-staging-backup", **fields):
        return Job.objects.create(
            kind=kind,
            status=status,
            endpoint="vps-1",
            instance_slug=slug,
            container_name="c",
            created_by=self.user,
            **fields,
        )

    def test_the_title_names_the_file_or_counts_them(self):
        one = Job(kind=Job.Kind.DELETE, files=["a_20261029_020000.zip"])
        many = Job(
            kind=Job.Kind.DELETE, files=["a_20261029_020000.zip", "a_20261028_020000.zip", "b_20261028_020000.zip"]
        )
        self.assertEqual(one.title, "Delete a_20261029_020000.zip")
        self.assertEqual(many.title, "Delete 3 backup files")
        self.assertEqual(Job(kind=Job.Kind.DELETE).title, "Delete 0 backup files")

    def test_a_delete_blocks_backups_and_restores_of_the_same_instance_and_the_other_way_round(self):
        for kind in (Job.Kind.DELETE, Job.Kind.BACKUP, Job.Kind.RESTORE):
            for status in (Job.Status.QUEUED, Job.Status.RUNNING):
                with self.subTest(kind=kind, status=status):
                    Job.objects.all().delete()
                    active = self.job(kind, status)
                    self.assertEqual(Job.active_for("vps-1", "acme-staging-backup"), active)
                    self.assertIsNone(Job.active_for("vps-1", "other-stack"))

    def test_finished_jobs_and_downloads_do_not_block(self):
        self.job(Job.Kind.DELETE, Job.Status.DONE)
        self.job(Job.Kind.DELETE, Job.Status.FAILED)
        self.job(Job.Kind.DOWNLOAD, Job.Status.RUNNING)
        self.assertIsNone(Job.active_for("vps-1", "acme-staging-backup"))

    def test_the_latest_job_includes_deletes(self):
        self.job(Job.Kind.BACKUP, Job.Status.DONE)
        newest = self.job(Job.Kind.DELETE, Job.Status.DONE, files=["x.zip"])
        self.assertEqual(Job.latest_for("vps-1", "acme-staging-backup"), newest)
