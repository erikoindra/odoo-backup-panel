from django.conf import settings
from django.db import models


class Job(models.Model):
    """One download, backup or restore, kept as the audit trail (docs/spec.md 6)."""

    class Kind(models.TextChoices):
        DOWNLOAD = "download", "Download"
        BACKUP = "backup", "Backup"
        RESTORE = "restore", "Restore"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    endpoint = models.CharField(max_length=100)
    instance_slug = models.CharField(max_length=200)
    container_name = models.CharField(max_length=255)
    filename = models.CharField(max_length=255, blank=True)  # download / restore
    target_db = models.CharField(max_length=63, blank=True)  # restore
    neutralize = models.BooleanField(default=False)  # restore: run `odoo neutralize` on the new database
    steps = models.JSONField(default=list, blank=True)  # [{"name", "status", "at"}]
    summary = models.CharField(max_length=500, blank=True)  # the outcome in one sentence, or why it failed
    log = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="jobs")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    @property
    def is_active(self):
        return self.status in (self.Status.QUEUED, self.Status.RUNNING)

    @property
    def progress(self):
        """0-100 from the steps: a finished step counts fully, the running one half."""
        if self.status == self.Status.DONE:
            return 100
        if not self.steps:
            return 0
        done = sum(1 for s in self.steps if s["status"] == "done")
        running = sum(1 for s in self.steps if s["status"] == "running")
        return round((done + running / 2) / len(self.steps) * 100)

    @property
    def duration(self):
        """How long the job ran (or has been running); None while it is only queued."""
        from django.utils import timezone

        if not self.started_at:
            return None
        return (self.finished_at or timezone.now()) - self.started_at

    @property
    def title(self):
        if self.kind == self.Kind.RESTORE:
            return f"Restore to {self.target_db}"
        return {"backup": "Back up now", "download": f"Download {self.filename}"}[self.kind]

    @classmethod
    def active_for(cls, endpoint, slug):
        """The backup or restore of this instance that is waiting or running, if any."""
        return cls.objects.filter(
            endpoint=endpoint,
            instance_slug=slug,
            kind__in=(cls.Kind.BACKUP, cls.Kind.RESTORE),
            status__in=(cls.Status.QUEUED, cls.Status.RUNNING),
        ).first()

    @classmethod
    def latest_for(cls, endpoint, slug):
        """The newest backup or restore of this instance, whatever its state."""
        return cls.objects.filter(
            endpoint=endpoint, instance_slug=slug, kind__in=(cls.Kind.BACKUP, cls.Kind.RESTORE)
        ).first()

    def __str__(self):
        return f"#{self.pk} {self.kind} {self.endpoint}/{self.instance_slug} ({self.status})"
