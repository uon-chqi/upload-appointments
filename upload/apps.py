from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.dispatch import receiver


@receiver(connection_created)
def configure_sqlite(sender, connection, **kwargs):
    """Make SQLite tolerate the concurrent writers a multi-facility run creates.

    WAL lets the upload workers write progress while the web process reads it,
    and busy_timeout makes a contended write wait rather than raise "database is
    locked" immediately.
    """
    if connection.vendor != 'sqlite':
        return
    with connection.cursor() as cursor:
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA busy_timeout=15000;')
        cursor.execute('PRAGMA synchronous=NORMAL;')


class UploadConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'upload'
