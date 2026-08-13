from django.db import models
from django.db.models import Q, UniqueConstraint

from . import crypto
from .openmrs import DEFAULT_DATABASE_PREFIX, FacilityConfig


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

    # A third deployment shape: one MySQL server hosting a schema per facility.
    # Kept as its own flag rather than folded into multi_facility_enabled so an
    # existing deployment's setting keeps meaning what it meant. The two are
    # mutually exclusive in the UI; cron_mode() is the tie-breaker if a stray
    # admin edit ever sets both.
    multi_tenant_enabled = models.BooleanField(
        default=False,
        help_text='When enabled, the cron job uploads every schema discovered on '
                  'the configured multi-tenant servers. Takes precedence over '
                  'multi-facility mode if both are somehow set.',
    )

    # A fresh install has no history upstream, so the first run uploads every
    # pending appointment rather than one night's window. Recorded here so it
    # happens exactly once: whichever comes first, the operator pressing the
    # button or the next cron firing, and never again afterwards.
    initial_backfill_done = models.BooleanField(
        default=False,
        help_text='Whether the one-off upload of all pending appointments has run.',
    )
    initial_backfill_at = models.DateTimeField(null=True, blank=True)
    initial_backfill_run = models.ForeignKey(
        'UploadRun', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
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

    def cron_mode(self):
        """Which mode the nightly job runs in: 'single', 'multi' or 'tenant'."""
        if self.multi_tenant_enabled:
            return 'tenant'
        if self.multi_facility_enabled:
            return 'multi'
        return 'single'

    def __str__(self):
        return 'Cron mode: {}'.format(self.cron_mode())


class TenantServer(models.Model):
    """One MySQL instance hosting many facilities' OpenMRS schemas.

    The multi-facility setup is one KenyaEMR container per facility, each with
    its own MySQL and its own hand-entered connection. A multi-tenant cloud
    server inverts that: a single MySQL holds one schema per facility, named
    with a shared prefix, so the only thing that differs per facility is the
    schema name — and the server itself can be asked for the list. Nothing here
    is per-facility; the facilities are discovered (see `upload.tenants`).
    """

    name = models.CharField(
        max_length=200, unique=True,
        help_text='Label for this server in the UI.',
    )
    host = models.CharField(
        max_length=255,
        help_text='Hostname or IP address of the MySQL server holding the tenant schemas.',
    )
    port = models.PositiveIntegerField(default=3306)
    username = models.CharField(max_length=100)
    password_encrypted = models.TextField(blank=True, default='')
    database_prefix = models.CharField(
        max_length=100, default=DEFAULT_DATABASE_PREFIX,
        help_text='Databases whose names start with this are treated as facilities. '
                  'Matched case-insensitively.',
    )

    is_active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_ok = models.BooleanField(null=True, blank=True)
    last_sync_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def set_password(self, raw_password):
        self.password_encrypted = crypto.encrypt(raw_password)

    def get_password(self):
        return crypto.decrypt(self.password_encrypted)

    def as_config(self, database='', label=None):
        """A connection to this server, optionally scoped to one of its schemas.

        Without a database it reaches the server itself, which is what listing
        the tenant schemas needs.
        """
        return FacilityConfig(
            label=label or self.name,
            host=self.host,
            port=self.port,
            user=self.username,
            password=self.get_password(),
            database=database,
        )


class FacilityQuerySet(models.QuerySet):
    def standalone(self):
        """Facilities with their own MySQL — the multi-facility setup."""
        return self.filter(server__isnull=True)

    def tenants(self):
        """Facilities discovered as schemas on a multi-tenant server."""
        return self.filter(server__isnull=False)

    def for_mode(self, mode):
        """The facilities a run in `mode` targets.

        The two setups share one table but never one run: a multi-facility
        upload must not sweep in a hundred discovered tenants, and vice versa.
        """
        if mode == 'tenant':
            return self.tenants()
        if mode == 'multi':
            return self.standalone()
        raise ValueError('mode {!r} does not upload Facility rows.'.format(mode))


class Facility(models.Model):
    """One facility's OpenMRS database.

    Either a KenyaEMR container of its own (`server` is NULL, connection details
    typed in) or a schema discovered on a multi-tenant server (`server` set,
    connection details taken from it). Everything downstream — runs, logs,
    retries, backfill stamps — treats the two identically.
    """

    name = models.CharField(
        max_length=200, unique=True,
        help_text='Label for this facility in the UI. The name and MFL code sent '
                  'upstream come from the OpenMRS data itself, not from this field.',
    )
    # NULL for a standalone container. Set for a schema on a multi-tenant server,
    # in which case host/port/username below are refreshed copies kept only so
    # the row reads sensibly in the UI — as_config() always goes to the server.
    server = models.ForeignKey(
        'TenantServer', null=True, blank=True, on_delete=models.CASCADE,
        related_name='facilities',
        help_text='The multi-tenant server this schema was discovered on, if any.',
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
    # Set when a sync turned this facility off — because its schema vanished, or
    # because it could not be identified. Distinguishes that from an operator
    # switching it off, which a later sync must not quietly undo.
    disabled_by_sync = models.BooleanField(default=False)
    # Last time a sync saw this schema on its server. NULL for standalone rows.
    last_seen_at = models.DateTimeField(null=True, blank=True)
    # When this facility's pending appointments were last fully loaded. Per
    # facility as well as deployment-wide, because a backfill across a hundred
    # containers will normally leave a few behind for the operator to retry.
    initial_backfill_at = models.DateTimeField(null=True, blank=True)
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_ok = models.BooleanField(null=True, blank=True)
    last_test_message = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = FacilityQuerySet.as_manager()

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Facilities'
        constraints = [
            UniqueConstraint(
                fields=['mfl_code'],
                condition=~Q(mfl_code=''),
                name='unique_non_blank_mfl_code',
            ),
            # One row per schema per server. Conditional because a standalone
            # facility has no server, and several of them may legitimately use
            # the default database name 'openmrs'.
            UniqueConstraint(
                fields=['server', 'database_name'],
                condition=Q(server__isnull=False),
                name='unique_database_per_tenant_server',
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_tenant(self):
        return self.server_id is not None

    def set_password(self, raw_password):
        self.password_encrypted = crypto.encrypt(raw_password)

    def get_password(self):
        return crypto.decrypt(self.password_encrypted)

    def as_config(self):
        # A discovered schema has no credentials of its own: they belong to the
        # server, so changing them there reaches every tenant at once and a
        # stale denormalised copy can never point a run at the wrong host.
        if self.server_id:
            return FacilityConfig(
                label=self.name,
                host=self.server.host,
                port=self.server.port,
                user=self.server.username,
                password=self.server.get_password(),
                database=self.database_name,
                pk=self.pk,
            )
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
        ('tenant', 'Multi Tenant'),
    ]
    # The modes whose runs target Facility rows rather than the environment.
    MULTI_MODES = ('multi', 'tenant')
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
    # A backfill ignores date_from/date_to and uploads every pending appointment.
    # The dates are still stored — they record the day it ran — but the UI shows
    # "all pending" for these, since a range would be a lie.
    is_backfill = models.BooleanField(default=False)
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
        return '{} — {} ({})'.format(self.period_label, self.status, self.mode)

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    @property
    def period_label(self):
        if self.is_backfill:
            return 'All pending appointments'
        return '{} to {}'.format(self.date_from, self.date_to)


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
