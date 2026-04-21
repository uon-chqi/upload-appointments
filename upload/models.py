from django.db import models


class UploadLog(models.Model):
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.date_from} to {self.date_to} — {self.status} ({self.triggered_by})"
