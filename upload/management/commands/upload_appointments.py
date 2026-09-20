import sys
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from upload import openmrs, schedule, services, tenants
from upload.models import AppSettings, Facility, TenantServer, UploadRun


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
        parser.add_argument(
            '--backfill',
            action='store_true',
            help='Upload every pending appointment, ignoring the date range. '
                 'Runs automatically on the first cron job after install; this '
                 'forces another one.',
        )
        parser.add_argument(
            '--no-sync',
            action='store_true',
            help='In multi-tenant mode, upload the schemas already known instead '
                 'of asking the servers what they hold first.',
        )
        parser.add_argument(
            '--due-only',
            action='store_true',
            help='Upload only the facilities that are behind, each for the period '
                 'it actually missed. Exits quietly when there is nothing to do, '
                 'so it can be run every half hour from cron.',
        )

    def handle(self, *args, **options):
        services.mark_stale_runs()

        if options['run_id']:
            run = self._load_run(options['run_id'])
        elif options['due_only']:
            run = self._create_due_run(options)
            if run is None:
                return
        else:
            run = self._create_run(options)

        self.stdout.write(
            'Uploading {} facility(ies) — {}...'.format(
                run.facilities_total, run.period_label.lower(),
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

        app_settings = AppSettings.load()
        # A deployment that has never done its initial load uploads everything
        # outstanding instead of one night's window. That is a superset of the
        # window, so nothing is skipped by not also running the daily upload.
        # An explicitly requested range always wins — someone asking for specific
        # dates means those dates.
        explicit_range = bool(options['date_from'] or options['date_to'])
        if options['backfill'] and explicit_range:
            raise CommandError(
                '--backfill uploads every pending appointment and ignores the '
                'date range. Pass one or the other, not both.'
            )
        is_backfill = options['backfill'] or (
            not explicit_range and not app_settings.initial_backfill_done
        )
        if is_backfill and not options['backfill']:
            self.stdout.write(self.style.WARNING(
                'No initial upload has been recorded for this deployment; '
                'uploading all pending appointments instead of the nightly window.'
            ))

        if options['facility']:
            try:
                facility = Facility.objects.get(pk=options['facility'])
            except Facility.DoesNotExist:
                raise CommandError('Facility {} does not exist.'.format(options['facility']))
            return services.create_run(
                date_from, date_to, triggered_by='cron',
                # A discovered tenant is uploaded as a tenant run, so its history
                # lands on the page the operator manages it from.
                mode='tenant' if facility.is_tenant else 'multi',
                facilities=[facility], is_backfill=is_backfill,
            )

        mode = app_settings.cron_mode()

        if mode == 'tenant':
            if not options['no_sync']:
                self._sync_tenants(options.get('workers'))
            if not Facility.objects.tenants().filter(is_active=True).exists():
                raise CommandError(
                    'Multi-tenant mode is enabled but no tenant database is '
                    'switched on. Discovered databases start disabled; enable '
                    'the ones that should upload on the Multi-Tenant page.'
                )
        elif mode == 'multi':
            if not Facility.objects.standalone().filter(is_active=True).exists():
                raise CommandError(
                    'Multi-facility mode is enabled but no active facilities are '
                    'configured.'
                )

        return services.create_run(
            date_from, date_to, triggered_by='cron', mode=mode,
            is_backfill=is_backfill,
        )

    def _create_due_run(self, options):
        """The frequent tick: upload whatever is behind, or nothing at all.

        Returns None when there is nothing to do, which is the common case. Every
        way of declining exits 0 and writes only to stdout, never stderr, so a
        crontab entry redirecting stdout to /dev/null mails on real failures and
        stays silent the rest of the day. Run every half hour, anything else is
        forty-eight mails a day and an operator who reads none of them.

        The order matters. Connectivity is checked before a run is created, so an
        afternoon with no internet leaves no trace rather than a failed run every
        thirty minutes; and the facilities are picked by what they owe rather
        than by the clock, so a machine switched on at two in the afternoon
        uploads then, for the nights it was off.
        """
        if options['backfill'] or options['date_from'] or options['date_to']:
            raise CommandError(
                "--due-only works out each facility's period from what it last "
                'uploaded, so it takes neither --backfill nor a date range.'
            )
        if options['facility']:
            raise CommandError(
                '--due-only covers every facility that is behind. To re-run one, '
                'use --facility on its own.'
            )

        # Not an error, unlike the nightly path: at this cadence a tick landing
        # inside a long run is routine, and cron mails anything that exits 1.
        running = services.active_run()
        if running:
            self.stdout.write(
                'Upload run {} is still in progress; nothing to do.'.format(running.pk)
            )
            return None

        reachable, why = services.platform_reachable()
        if not reachable:
            self.stdout.write(self.style.WARNING(
                'No link to the platform: {} Nothing attempted — the facilities '
                'are not behind, the internet is.'.format(why)
            ))
            return None

        mode = AppSettings.load().cron_mode()
        if mode == 'tenant' and not options['no_sync']:
            # Not _sync_tenants' own guard, which raises: a deployment configured
            # wrongly should say so once a tick and let the tick end quietly, not
            # exit 1 and mail the operator every half hour about it.
            if TenantServer.objects.filter(is_active=True).exists():
                self._sync_tenants(options.get('workers'))
            else:
                self.stdout.write(self.style.WARNING(
                    'Multi-tenant mode is enabled but no active tenant servers '
                    'are configured; there is nothing to discover or upload.'
                ))
                return None

        targets = schedule.due_targets(mode)
        if not targets:
            self.stdout.write('Every facility is up to date; nothing to upload.')
            return None

        for facility in targets:
            window = schedule.window_for(facility)
            self.stdout.write('  {}: {}'.format(
                facility.name if facility else openmrs.env_config().label,
                'all pending' if window is None
                else '{} to {}'.format(window[0], window[1]),
            ))

        # The dates passed here are only the fallback for a facility whose window
        # is "everything pending", where they record the day the run happened;
        # every other log gets its own period from window_for.
        today = timezone.localdate()
        return services.create_run(
            today, today, triggered_by='cron', mode=mode,
            facilities=targets if mode in UploadRun.MULTI_MODES else None,
            windows=schedule.window_for,
        )

    def _sync_tenants(self, workers):
        """Refresh the schema list before uploading it.

        A tenant server gains and loses facilities without anyone touching this
        deployment, so the nightly run asks rather than assumes. A server that is
        unreachable is reported and skipped: yesterday's list is a far better
        basis for tonight's upload than no upload at all.
        """
        if not TenantServer.objects.filter(is_active=True).exists():
            raise CommandError(
                'Multi-tenant mode is enabled but no active tenant servers are '
                'configured.'
            )
        for server, summary in tenants.sync_all(workers=workers, reprobe=False):
            if summary['ok']:
                self.stdout.write('{}: {}'.format(server.name, summary['message']))
            else:
                self.stderr.write(self.style.WARNING(
                    '{}: {} Uploading the databases already known.'.format(
                        server.name, summary['message'],
                    )
                ))

    def _report(self, run):
        run.refresh_from_db()
        summary = '{} of {} facilities uploaded, {} records, {} patient updates'.format(
            run.facilities_total - run.facilities_failed,
            run.facilities_total,
            run.records_uploaded,
            run.patient_updates_uploaded,
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
