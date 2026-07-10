import sys
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError

from upload import services
from upload.models import AppSettings, Facility, UploadRun


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
        parser.add_argument(
            '--run-id',
            type=int,
            help='Execute an existing pending run. Used by the web UI, which '
                 'creates the run and then detaches this command to do the work.',
        )
        parser.add_argument(
            '--facility',
            type=int,
            help='Upload only this facility (by ID). Useful for re-running one '
                 'that failed without repeating the whole batch.',
        )
        parser.add_argument(
            '--workers',
            type=int,
            help='Override the number of facilities uploaded concurrently.',
        )

    def handle(self, *args, **options):
        services.mark_stale_runs()

        if options['run_id']:
            run = self._load_run(options['run_id'])
        else:
            run = self._create_run(options)

        self.stdout.write(
            'Uploading {} facility(ies) from {} to {}...'.format(
                run.facilities_total, run.date_from, run.date_to,
            )
        )
        services.execute_run(run, workers=options.get('workers'))
        self._report(run)

    def _load_run(self, run_id):
        try:
            run = UploadRun.objects.get(pk=run_id)
        except UploadRun.DoesNotExist:
            raise CommandError('Run {} does not exist.'.format(run_id))
        if not run.is_active:
            raise CommandError(
                'Run {} is already {}.'.format(run_id, run.status)
            )
        return run

    def _create_run(self, options):
        # A cron firing while the previous night's run is still going would upload
        # everything twice. mark_stale_runs() has already cleared dead runs, so
        # anything still active is genuinely in flight.
        running = services.active_run()
        if running:
            raise CommandError(
                'Upload run {} is still in progress (started {}). Refusing to '
                'start another.'.format(running.pk, running.created_at)
            )

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
        if date_from > date_to:
            raise CommandError("--date-from must be on or before --date-to.")

        if options['facility']:
            try:
                facility = Facility.objects.get(pk=options['facility'])
            except Facility.DoesNotExist:
                raise CommandError('Facility {} does not exist.'.format(options['facility']))
            return services.create_run(
                date_from, date_to, triggered_by='cron',
                mode='multi', facilities=[facility],
            )

        if AppSettings.load().multi_facility_enabled:
            if not Facility.objects.filter(is_active=True).exists():
                raise CommandError(
                    'Multi-facility mode is enabled but no active facilities are '
                    'configured.'
                )
            return services.create_run(
                date_from, date_to, triggered_by='cron', mode='multi',
            )

        return services.create_run(
            date_from, date_to, triggered_by='cron', mode='single',
        )

    def _report(self, run):
        run.refresh_from_db()
        summary = '{} of {} facilities uploaded, {} records'.format(
            run.facilities_total - run.facilities_failed,
            run.facilities_total,
            run.records_uploaded,
        )
        if run.status == 'success':
            self.stdout.write(self.style.SUCCESS('Success: ' + summary))
            return

        for log in run.logs.filter(status='failed').order_by('facility_label'):
            last_line = (log.error_message or '').strip().splitlines()
            self.stderr.write('  {}: {}'.format(
                log.facility_label, last_line[-1] if last_line else 'failed',
            ))

        if run.status == 'partial':
            # Exit 0: with a hundred facilities a couple will be down on any given
            # night, and a cron mail every morning is a cron mail nobody reads.
            # The run shows as partial in the UI, with a retry button.
            self.stderr.write(self.style.WARNING('Partial: ' + summary))
            return

        self.stderr.write(self.style.ERROR('Failed: ' + summary))
        sys.exit(1)
