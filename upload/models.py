from django.db import models
from django.db.models import Q, UniqueConstraint

from . import crypto
from .openmrs import FacilityConfig


class AppSettings(models.Model):
    """Single-row deployment settings.

    The same codebase is deployed per client; most run one facility from the
    OPENMRS_DB_* environment variables. Enabling multi-facility mode switches
    what the nightly cron job uploads, and nothing else.
    """

    multi_facility_enabled = models.BooleanField(
        default=False,
        help_text='When enabled, the cron job uploads every active facility '
                  'instead of the environment-configured one.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'App settings'
        verbose_name_plural = 'App settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return 'Multi-facility mode: {}'.format(
            'on' if self.multi_facility_enabled else 'off'
        )


class Facility(models.Model):
    """One KenyaEMR container's OpenMRS database."""

    name = models.CharField(
        max_length=200, unique=True,
        help_text='Label for this facility in the UI. The name and MFL code sent '
                  'upstream come from the OpenMRS data itself, not from this field.',
    )
    host = models.CharField(
        max_length=255,
        help_text='Hostname, container/service name, or IP address of the MySQL server.',
    )
    port = models.PositiveIntegerField(default=3306)
    database_name = models.CharField(max_length=100, default='openmrs')
    username = models.CharField(max_length=100)
    password_encrypted = models.TextField(blank=True, default='')

    # Discovered by the test-connection probe, not typed in. Unique because the
    # DIFF platform identifies facilities solely by MFL: two containers sharing
    # one would silently overwrite each other upstream.
    mfl_code = models.CharField(max_length=20, blank=True, default='')
    mfl_facility_name = models.CharField(max_length=200, blank=True, default='')

    is_active = models.BooleanField(default=True)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_ok = models.BooleanField(null=True, blank=True)
    last_test_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Facilities'
        constraints = [
            UniqueConstraint(
                fields=['mfl_code'],
                condition=~Q(mfl_code=''),
                name='unique_non_blank_mfl_code',
            ),
        ]

    def __str__(self):
        return self.name

    def set_password(self, raw_password):
        self.password_encrypted = crypto.encrypt(raw_password)

    def get_password(self):
        return crypto.decrypt(self.password_encrypted)

    def as_config(self):
        return FacilityConfig(
            label=self.name,
            host=self.host,
            port=self.port,
            user=self.username,
            password=self.get_password(),
            database=self.database_name,
            pk=self.pk,
        )


class UploadRun(models.Model):
    """One upload, covering one facility in single mode or many in multi mode."""

    MODE_CHOICES = [
        ('single', 'Single Facility'),
        ('multi', 'Multi Facility'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('partial', 'Partial'),
        ('failed', 'Failed'),
    ]
    TRIGGER_CHOICES = [
        ('cron', 'Cron Job'),
        ('manual', 'Manual'),
    ]
    ACTIVE_STATUSES = ('pending', 'in_progress')

    date_from = models.DateField()
    date_to = models.DateField()
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default='single')
    triggered_by = models.CharField(max_length=10, choices=TRIGGER_CHOICES)
    triggered_by_user = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
    )
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')

    facilities_total = models.IntegerField(default=0)
    facilities_completed = models.IntegerField(default=0)
    facilities_failed = models.IntegerField(default=0)
    records_uploaded = models.IntegerField(default=0)
    message = models.TextField(blank=True, default='')

    retry_of = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='retries',
    )

    # Bumped as facilities finish. A run whose heartbeat goes cold is assumed
    # dead — its process was killed mid-flight — and gets marked failed.
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return '{} to {} — {} ({})'.format(
            self.date_from, self.date_to, self.status, self.mode,
        )

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES


class UploadLog(models.Model):
    """The result of uploading one facility within a run."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]
    TRIGGER_CHOICES = [
        ('cron', 'Cron Job'),
        ('manual', 'Manual'),
    ]

    run = models.ForeignKey(
        UploadRun, null=True, blank=True, on_delete=models.CASCADE, related_name='logs',
    )
    # NULL means the environment-configured facility, which is also how every
    # log predating multi-facility support reads.
    facility = models.ForeignKey(
        Facility, null=True, blank=True, on_delete=models.SET_NULL, related_name='logs',
    )
    # Snapshot, so history survives the facility being renamed or deleted.
    facility_label = models.CharField(max_length=200, blank=True, default='')

    date_from = models.DateField(help_text='Start date of the query period')
    date_to = models.DateField(help_text='End date of the query period')
    triggered_by = models.CharField(max_length=10, choices=TRIGGER_CHOICES)
    triggered_by_user = models.ForeignKey(
        'auth.User', null=True, blank=True, on_delete=models.SET_NULL,
        help_text='User who triggered a manual upload',
    )
    status = models.CharField(max_length=15, choices=STATUS_CHOICES)
    records_uploaded = models.IntegerField(default=0)
    batches_total = models.IntegerField(default=0)
    batches_completed = models.IntegerField(default=0)
    error_message = models.TextField(blank=True, default='')
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.date_from} to {self.date_to} — {self.status} ({self.triggered_by})"
