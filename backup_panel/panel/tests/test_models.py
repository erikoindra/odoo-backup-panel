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
