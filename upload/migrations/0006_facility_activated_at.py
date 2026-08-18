from django.db import migrations, models
from django.db.models import F


def stamp_existing_tenants(apps, schema_editor):
    """Existing active tenants were enabled by the old discover-and-switch-on rule.

    Without this they would read as "never chosen", and a sync that had to
    disable one would then refuse to switch it back on when it recovered.
    """
    Facility = apps.get_model('upload', 'Facility')
    Facility.objects.filter(
        server__isnull=False, is_active=True, activated_at__isnull=True,
    ).update(activated_at=F('created_at'))


class Migration(migrations.Migration):

    dependencies = [
        ('upload', '0005_tenantserver_appsettings_multi_tenant_enabled_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='facility',
            name='activated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(stamp_existing_tenants, migrations.RunPython.noop),
    ]
