from datetime import date, timedelta

from django.core.management.base import BaseCommand

from upload.services import run_upload


class Command(BaseCommand):
    help = 'Fetch appointments from OpenMRS and upload to Ushauri DIFF platform'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date-from',
            type=str,
            help='Start date (YYYY-MM-DD). Defaults to yesterday.',
        )
        parser.add_argument(
            '--date-to',
            type=str,
            help='End date (YYYY-MM-DD). Defaults to today.',
        )

    def handle(self, *args, **options):
        today = date.today()
        yesterday = today - timedelta(days=1)

        date_from = (
            date.fromisoformat(options['date_from'])
            if options['date_from']
            else yesterday
        )
        date_to = (
            date.fromisoformat(options['date_to'])
            if options['date_to']
            else today
        )

        self.stdout.write(f"Uploading appointments from {date_from} to {date_to}...")
        log = run_upload(date_from, date_to, triggered_by='cron')

        if log.status == 'success':
            self.stdout.write(self.style.SUCCESS(
                f"Success: {log.records_uploaded} records uploaded."
            ))
        else:
            self.stderr.write(self.style.ERROR(
                f"Failed: {log.error_message}"
            ))
