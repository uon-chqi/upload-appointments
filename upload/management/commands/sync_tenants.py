import sys

from django.core.management.base import BaseCommand, CommandError

from upload import tenants
from upload.models import TenantServer


class Command(BaseCommand):
    help = ('Discover the OpenMRS schemas on each multi-tenant server and '
            'reconcile them into facilities.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--server',
            type=int,
            help='Sync only this server (by ID), active or not.',
        )
        parser.add_argument(
            '--no-reprobe',
            action='store_true',
            help='Identify only schemas that are new or still unidentified, '
                 'instead of asking every schema for its MFL again.',
        )
        parser.add_argument(
            '--workers',
            type=int,
            help='Override how many schemas are identified concurrently.',
        )

    def handle(self, *args, **options):
        if options['server']:
            try:
                servers = [TenantServer.objects.get(pk=options['server'])]
            except TenantServer.DoesNotExist:
                raise CommandError(
                    'Tenant server {} does not exist.'.format(options['server'])
                )
        else:
            servers = list(TenantServer.objects.filter(is_active=True))
            if not servers:
                raise CommandError('No active multi-tenant servers are configured.')

        failed = 0
        for server in servers:
            summary = tenants.sync_server(
                server,
                workers=options.get('workers'),
                reprobe=not options['no_reprobe'],
            )
            line = '{}: {}'.format(server.name, summary['message'])
            if not summary['ok']:
                failed += 1
                self.stderr.write(self.style.ERROR(line))
                continue

            for problem in summary['problems']:
                self.stderr.write('  ' + problem)
            style = self.style.WARNING if summary['problems'] else self.style.SUCCESS
            self.stdout.write(style(line))

        # Exit 1 only when nothing could be synced at all. A server that answered
        # but has a couple of unidentifiable schemas is the normal case at scale,
        # and a cron mail every morning is a cron mail nobody reads.
        if failed == len(servers):
            sys.exit(1)
