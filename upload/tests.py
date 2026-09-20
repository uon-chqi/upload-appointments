from contextlib import contextmanager
from datetime import date, timedelta
from io import StringIO
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import (Client, SimpleTestCase, TestCase, TransactionTestCase,
                         override_settings)
from django.urls import reverse
from django.utils import timezone

from upload import crypto, openmrs, schedule, services, tenants
from upload.models import AppSettings, Facility, TenantServer, UploadLog, UploadRun


def make_response(status_code=200, json_data=None, headers=None, text=''):
    """Build a stand-in for a requests.Response with the bits the code touches."""
    resp = mock.Mock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    resp.headers = headers or {}
    resp.text = text
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


def make_tokens(*tokens):
    """A TokenProvider that hands out canned tokens instead of calling the API."""
    return services.TokenProvider(fetch=mock.Mock(side_effect=list(tokens) or ['t']))


@override_settings(CHQI_API_BASE_URL='https://api.test')
class PostBatchWithRetryTests(SimpleTestCase):
    def setUp(self):
        self.url = 'https://api.test/api/patients/upload-json'
        self.payload = '{"patients": []}'

    def call(self, tokens=None, **kwargs):
        return services._post_batch_with_retry(
            self.url, self.payload, tokens or make_tokens('t'), 1, 1, **kwargs
        )

    @mock.patch('upload.services.time.sleep')
    @mock.patch('upload.services.requests.post')
    def test_retries_on_5xx_then_succeeds(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            make_response(503, text='busy'),
            make_response(200, json_data={'ok': True}),
        ]
        resp = self.call()
        self.assertTrue(resp.ok)
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @mock.patch('upload.services.time.sleep')
    @mock.patch('upload.services.requests.post')
    def test_honors_retry_after_header_on_429(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            make_response(429, headers={'Retry-After': '7'}),
            make_response(200, json_data={'ok': True}),
        ]
        self.call()
        # The server-provided Retry-After must win over the jittered backoff.
        mock_sleep.assert_called_once_with(7)

    @mock.patch('upload.services.time.sleep')
    @mock.patch('upload.services.requests.post')
    def test_retries_on_network_errors_then_succeeds(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            requests.ConnectionError('boom'),
            requests.Timeout('slow'),
            make_response(200, json_data={'ok': True}),
        ]
        resp = self.call()
        self.assertTrue(resp.ok)
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @mock.patch('upload.services.time.sleep')
    @mock.patch('upload.services.requests.post')
    def test_fails_fast_on_4xx_without_retry(self, mock_post, mock_sleep):
        mock_post.return_value = make_response(400, json_data={'error': 'bad'})
        with self.assertRaises(requests.HTTPError):
            self.call()
        self.assertEqual(mock_post.call_count, 1)
        mock_sleep.assert_not_called()

    @mock.patch('upload.services.time.sleep')
    @mock.patch('upload.services.requests.post')
    def test_raises_after_exhausting_retries_on_5xx(self, mock_post, mock_sleep):
        mock_post.return_value = make_response(500, text='down')
        with self.assertRaises(requests.HTTPError):
            self.call(max_retries=3)
        self.assertEqual(mock_post.call_count, 3)
        # Sleeps happen between attempts, never after the final failure.
        self.assertEqual(mock_sleep.call_count, 2)

    @mock.patch('upload.services.time.sleep')
    @mock.patch('upload.services.requests.post')
    def test_raises_after_exhausting_retries_on_network_error(self, mock_post, mock_sleep):
        mock_post.side_effect = requests.ConnectionError('boom')
        with self.assertRaises(requests.ConnectionError):
            self.call(max_retries=2)
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(mock_sleep.call_count, 1)

    @mock.patch('upload.services.time.sleep')
    @mock.patch('upload.services.requests.post')
    def test_expired_token_is_refreshed_and_batch_retried(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            make_response(401, json_data={'error': 'expired'}),
            make_response(200, json_data={'ok': True}),
        ]
        tokens = make_tokens('stale', 'fresh')
        resp = self.call(tokens=tokens)

        self.assertTrue(resp.ok)
        self.assertEqual(mock_post.call_count, 2)
        # A token refresh is not a transient failure: no backoff, no attempt spent.
        mock_sleep.assert_not_called()
        self.assertEqual(
            [c.kwargs['headers']['Authorization'] for c in mock_post.call_args_list],
            ['Bearer stale', 'Bearer fresh'],
        )

    @mock.patch('upload.services.time.sleep')
    @mock.patch('upload.services.requests.post')
    def test_401_after_reauth_gives_up(self, mock_post, mock_sleep):
        mock_post.return_value = make_response(401, json_data={'error': 'nope'})
        with self.assertRaises(requests.HTTPError):
            self.call(tokens=make_tokens('a', 'b'))
        # One re-auth, then it stops rather than looping on a bad credential.
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_not_called()


class TokenProviderTests(SimpleTestCase):
    def test_authenticates_once_and_caches(self):
        fetch = mock.Mock(side_effect=['t1'])
        tokens = services.TokenProvider(fetch=fetch)
        self.assertEqual(tokens.get(), 't1')
        self.assertEqual(tokens.get(), 't1')
        fetch.assert_called_once()

    def test_refresh_is_skipped_when_another_worker_already_refreshed(self):
        fetch = mock.Mock(side_effect=['t1', 't2', 't3'])
        tokens = services.TokenProvider(fetch=fetch)
        self.assertEqual(tokens.get(), 't1')

        # Two workers hit a 401 holding the same stale token; only the first
        # should trigger a login, or 100 facilities means 100 logins.
        self.assertEqual(tokens.refresh('t1'), 't2')
        self.assertEqual(tokens.refresh('t1'), 't2')
        self.assertEqual(fetch.call_count, 2)


@override_settings(CHQI_API_BASE_URL='https://api.test', UPLOAD_BATCH_SIZE=10)
class UploadPatientsTests(SimpleTestCase):
    @mock.patch('upload.services.time.sleep')
    @mock.patch('upload.services.requests.post')
    def test_retry_is_per_batch_and_results_preserved(self, mock_post, mock_sleep):
        # 3 patients with batch_size=2 -> 2 batches; first batch fails once.
        mock_post.side_effect = [
            make_response(503),
            make_response(200, json_data={'batch': 1}),
            make_response(200, json_data={'batch': 2}),
        ]
        patients = [{'patient_id': str(i)} for i in range(3)]
        results = services.upload_patients(patients, make_tokens('t'), batch_size=2)
        self.assertEqual(results, [{'batch': 1}, {'batch': 2}])
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 1)

    @mock.patch('upload.services.requests.post')
    def test_progress_is_throttled_but_always_written_for_the_last_batch(self, mock_post):
        mock_post.return_value = make_response(200)
        patients = [{'patient_id': str(i)} for i in range(6)]  # 3 batches of 2
        log = mock.Mock(batches_total=0, batches_completed=0)

        services.upload_patients(patients, make_tokens('t'), log=log, batch_size=2,
                                 progress_interval=10_000)

        # One save to record batches_total, one for the final batch. Saving after
        # each batch would have 100 facilities pounding one SQLite file.
        self.assertEqual(log.save.call_count, 2)
        self.assertEqual(log.batches_completed, 3)

    @mock.patch('upload.services.requests.post')
    def test_progress_is_written_every_batch_when_interval_is_zero(self, mock_post):
        mock_post.return_value = make_response(200)
        patients = [{'patient_id': str(i)} for i in range(6)]
        log = mock.Mock(batches_total=0, batches_completed=0)

        services.upload_patients(patients, make_tokens('t'), log=log, batch_size=2,
                                 progress_interval=0)

        self.assertEqual(log.save.call_count, 4)  # batches_total + 3 batches


@override_settings(CHQI_API_BASE_URL='https://api.test')
class UploadPatientUpdatesTests(SimpleTestCase):
    """The second upload of each facility: where it posts, and how it shares a bar."""

    @mock.patch('upload.services.requests.post')
    def test_updates_go_to_the_update_endpoint(self, mock_post):
        mock_post.return_value = make_response(200)

        services.upload_patient_updates(
            [{'patient_id': '1'}], make_tokens('t'), batch_size=10,
        )

        url = mock_post.call_args[0][0]
        self.assertEqual(url, 'https://api.test/api/patients/update-json')

    @mock.patch('upload.services.requests.post')
    def test_a_second_phase_continues_the_first_phase_batch_numbering(self, mock_post):
        mock_post.return_value = make_response(200)
        log = mock.Mock(batches_total=0, batches_completed=0)

        # Two batches of updates following four batches of appointments.
        services.upload_patient_updates(
            [{'patient_id': str(i)} for i in range(4)], make_tokens('t'), log=log,
            batch_size=2, batch_offset=4, batches_total=6, progress_interval=0,
        )

        self.assertEqual(log.batches_total, 6)
        # 5 then 6 — never back to 1, which would make the bar jump backwards.
        self.assertEqual(log.batches_completed, 6)


class BackoffDelayTests(SimpleTestCase):
    def test_stays_within_jitter_bounds(self):
        for attempt in range(1, 6):
            ceiling = min(120, 5 * (2 ** (attempt - 1)))
            for _ in range(50):
                delay = services._backoff_delay(5, attempt)
                self.assertGreaterEqual(delay, 0)
                self.assertLessEqual(delay, ceiling)

    def test_ceiling_grows_exponentially(self):
        with mock.patch('upload.services.random.uniform', return_value=0) as mock_uniform:
            services._backoff_delay(5, 1)
            services._backoff_delay(5, 2)
            services._backoff_delay(5, 3)
        self.assertEqual(
            [c.args for c in mock_uniform.call_args_list],
            [(0, 5), (0, 10), (0, 20)],
        )

    def test_ceiling_is_capped(self):
        with mock.patch('upload.services.random.uniform', return_value=0) as mock_uniform:
            services._backoff_delay(5, 10)  # 5 * 2**9 = 2560, capped to 120
        mock_uniform.assert_called_once_with(0, 120)


class FakeCursor:
    """Replays canned rows for probe()'s three queries."""

    def __init__(self, version='8.0.35', default_location=(), visit_locations=()):
        self.version = version
        self.default_location = default_location
        self.visit_locations = visit_locations
        self.executed = []
        self._rows = ()

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if 'VERSION()' in sql:
            self._rows = ((self.version,),)
        elif 'kenyaemr.defaultLocation' in sql:
            self._rows = self.default_location
        else:
            self._rows = self.visit_locations

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows

    def close(self):
        pass


def fake_conn(**kwargs):
    cursor = FakeCursor(**kwargs)
    conn = mock.Mock()
    conn.cursor.return_value = cursor
    return conn, cursor


class ProbeTests(SimpleTestCase):
    config = openmrs.FacilityConfig(
        label='F', host='h', port=3306, user='u', password='p', database='openmrs',
    )

    def probe_with(self, conn):
        @contextmanager
        def fake_connect(config):
            yield conn

        with mock.patch.object(openmrs, 'connect', fake_connect):
            return openmrs.probe(self.config)

    def test_identifies_the_container_from_its_default_location(self):
        conn, cursor = fake_conn(default_location=(('Othach Dispensary', '14000'),))
        result = self.probe_with(conn)

        self.assertTrue(result['ok'])
        self.assertEqual(result['mfl'], '14000')
        self.assertEqual(result['facility_name'], 'Othach Dispensary')
        # A KenyaEMR container holds the whole national facility list, so the
        # visit-location fallback must not run when the default location answers.
        self.assertEqual(len(cursor.executed), 2)

    def test_falls_back_to_visit_locations_when_no_default_location_is_set(self):
        conn, cursor = fake_conn(default_location=(), visit_locations=(('Kilifi', '99999'),))
        result = self.probe_with(conn)

        self.assertTrue(result['ok'])
        self.assertEqual(result['mfl'], '99999')
        self.assertEqual(len(cursor.executed), 3)

    def test_ambiguous_identity_is_not_ok(self):
        conn, _ = fake_conn(
            default_location=(),
            visit_locations=(('Kilifi', '111'), ('Malindi', '222')),
        )
        result = self.probe_with(conn)

        self.assertFalse(result['ok'])
        self.assertEqual(result['mfl'], '')
        self.assertIn('defaultLocation', result['message'])

    def test_missing_mfl_is_not_ok(self):
        conn, _ = fake_conn(default_location=(('Othach Dispensary', None),))
        result = self.probe_with(conn)

        self.assertFalse(result['ok'])
        self.assertIn('no MFL code', result['message'])

    def test_no_location_at_all_is_not_ok(self):
        conn, _ = fake_conn(default_location=(), visit_locations=())
        result = self.probe_with(conn)

        self.assertFalse(result['ok'])
        self.assertIn('cannot be identified', result['message'])

    def test_connection_failure_is_reported_not_raised(self):
        @contextmanager
        def boom(config):
            raise OSError('connection refused')
            yield  # pragma: no cover

        with mock.patch.object(openmrs, 'connect', boom):
            result = openmrs.probe(self.config)

        self.assertFalse(result['ok'])
        self.assertIn('Could not connect', result['message'])


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key')
class CryptoTests(SimpleTestCase):
    def test_roundtrip(self):
        self.assertEqual(crypto.decrypt(crypto.encrypt('s3cret')), 's3cret')

    def test_ciphertext_is_not_the_plaintext(self):
        self.assertNotIn('s3cret', crypto.encrypt('s3cret'))

    def test_blank_values_pass_through(self):
        self.assertEqual(crypto.encrypt(''), '')
        self.assertEqual(crypto.decrypt(''), '')

    def test_decrypting_with_a_rotated_key_explains_itself(self):
        ciphertext = crypto.encrypt('s3cret')
        with override_settings(FIELD_ENCRYPTION_KEY='a-different-key'):
            with self.assertRaisesMessage(ValueError, 're-enter the password'):
                crypto.decrypt(ciphertext)


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', OPENMRS_DB_LABEL='Env facility')
class FacilityModelTests(TestCase):
    def test_password_is_encrypted_at_rest_and_recovered_for_the_connection(self):
        facility = Facility(name='Kilifi', host='db-kilifi', username='readonly')
        facility.set_password('hunter2')
        facility.save()

        stored = Facility.objects.values_list('password_encrypted', flat=True).get()
        self.assertNotIn('hunter2', stored)
        self.assertEqual(facility.as_config().password, 'hunter2')

    def test_config_repr_never_leaks_the_password(self):
        facility = Facility(name='Kilifi', host='db-kilifi', username='readonly')
        facility.set_password('hunter2')
        self.assertNotIn('hunter2', repr(facility.as_config()))

    def test_blank_mfl_codes_do_not_collide(self):
        Facility.objects.create(name='A', host='a', username='u', password_encrypted='x')
        Facility.objects.create(name='B', host='b', username='u', password_encrypted='x')
        self.assertEqual(Facility.objects.count(), 2)


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', OPENMRS_DB_LABEL='Env facility')
class CreateRunTests(TestCase):
    def setUp(self):
        self.dates = (date(2026, 1, 1), date(2026, 1, 2))

    def test_single_mode_creates_one_log_for_the_env_facility(self):
        run = services.create_run(*self.dates, triggered_by='cron', mode='single')
        self.assertEqual(run.facilities_total, 1)
        log = run.logs.get()
        self.assertIsNone(log.facility)
        self.assertEqual(log.facility_label, 'Env facility')

    def test_multi_mode_creates_one_log_per_active_facility(self):
        active = [
            Facility.objects.create(name=n, host=n, username='u', password_encrypted='x')
            for n in ('Kilifi', 'Malindi')
        ]
        Facility.objects.create(name='Retired', host='r', username='u',
                                password_encrypted='x', is_active=False)

        run = services.create_run(*self.dates, triggered_by='cron', mode='multi')

        self.assertEqual(run.facilities_total, 2)
        self.assertEqual(
            sorted(run.logs.values_list('facility_label', flat=True)),
            ['Kilifi', 'Malindi'],
        )
        self.assertEqual(
            sorted(log.facility_id for log in run.logs.all()),
            sorted(f.pk for f in active),
        )

    def test_explicit_facility_list_wins_over_the_active_set(self):
        kilifi = Facility.objects.create(name='Kilifi', host='k', username='u', password_encrypted='x')
        Facility.objects.create(name='Malindi', host='m', username='u', password_encrypted='x')

        run = services.create_run(*self.dates, triggered_by='manual', mode='multi',
                                  facilities=[kilifi])
        self.assertEqual(run.facilities_total, 1)
        self.assertEqual(run.logs.get().facility_label, 'Kilifi')


class FinalizeRunTests(TestCase):
    def _run_with(self, statuses):
        run = UploadRun.objects.create(
            date_from=date(2026, 1, 1), date_to=date(2026, 1, 2),
            mode='multi', triggered_by='cron', facilities_total=len(statuses),
        )
        for i, status in enumerate(statuses):
            UploadLog.objects.create(
                run=run, facility_label='F{}'.format(i), date_from=run.date_from,
                date_to=run.date_to, triggered_by='cron', status=status,
                records_uploaded=10,
            )
        return run

    def test_all_success(self):
        run = services._finalize_run(self._run_with(['success', 'success']))
        self.assertEqual(run.status, 'success')
        self.assertEqual(run.records_uploaded, 20)
        self.assertEqual(run.facilities_failed, 0)

    def test_some_failed_is_partial_not_failed(self):
        # 97 good facilities and 3 timeouts is the normal case; calling the whole
        # run "failed" would hide the 97.
        run = services._finalize_run(self._run_with(['success', 'failed', 'success']))
        self.assertEqual(run.status, 'partial')
        self.assertEqual(run.facilities_failed, 1)
        self.assertEqual(run.records_uploaded, 30)

    def test_all_failed(self):
        run = services._finalize_run(self._run_with(['failed', 'failed']))
        self.assertEqual(run.status, 'failed')

    def test_no_facilities_is_a_failure(self):
        run = services._finalize_run(self._run_with([]))
        self.assertEqual(run.status, 'failed')


@override_settings(UPLOAD_STALE_MINUTES=20)
class StaleRunTests(TestCase):
    def _run(self, status, heartbeat_age_minutes=None):
        run = UploadRun.objects.create(
            date_from=date(2026, 1, 1), date_to=date(2026, 1, 2),
            mode='multi', triggered_by='manual', status=status, facilities_total=1,
        )
        if heartbeat_age_minutes is not None:
            run.heartbeat_at = timezone.now() - timedelta(minutes=heartbeat_age_minutes)
            run.save(update_fields=['heartbeat_at'])
        UploadLog.objects.create(
            run=run, facility_label='F', date_from=run.date_from, date_to=run.date_to,
            triggered_by='manual', status='in_progress',
        )
        return run

    def test_run_with_a_cold_heartbeat_is_failed_along_with_its_logs(self):
        run = self._run('in_progress', heartbeat_age_minutes=45)
        services.mark_stale_runs()

        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('stopped unexpectedly', run.message)
        self.assertEqual(run.logs.get().status, 'failed')
        self.assertIsNotNone(run.finished_at)

    def test_live_run_is_left_alone(self):
        run = self._run('in_progress', heartbeat_age_minutes=1)
        services.mark_stale_runs()
        run.refresh_from_db()
        self.assertEqual(run.status, 'in_progress')

    def test_freshly_created_run_awaiting_its_process_is_left_alone(self):
        run = self._run('pending')
        services.mark_stale_runs()
        run.refresh_from_db()
        self.assertEqual(run.status, 'pending')

    def test_active_run_lookup_ignores_stale_runs(self):
        self._run('in_progress', heartbeat_age_minutes=45)
        services.mark_stale_runs()
        self.assertIsNone(services.active_run())


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', OPENMRS_DB_LABEL='Env facility')
class ViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user('admin', password='pw', is_staff=True)
        self.plain = User.objects.create_user('nurse', password='pw')

    def test_multi_facilities_page_is_staff_only(self):
        self.client.force_login(self.plain)
        response = self.client.get(reverse('upload:multi_facilities'))
        self.assertEqual(response.status_code, 302)

        self.client.force_login(self.staff)
        response = self.client.get(reverse('upload:multi_facilities'))
        self.assertEqual(response.status_code, 200)

    @mock.patch('upload.services.spawn_run')
    def test_second_upload_is_refused_while_one_is_running(self, mock_spawn):
        self.client.force_login(self.staff)
        payload = {'date_from': '2026-01-01', 'date_to': '2026-01-02'}

        first = self.client.post(reverse('upload:upload'), payload)
        self.assertEqual(first.status_code, 200)

        second = self.client.post(reverse('upload:upload'), payload)
        self.assertEqual(second.status_code, 409)
        self.assertIn('already running', second.json()['error'])
        self.assertEqual(UploadRun.objects.count(), 1)
        mock_spawn.assert_called_once()

    @mock.patch('upload.services.spawn_run')
    def test_backfill_button_starts_a_backfill_without_asking_for_dates(self, mock_spawn):
        self.client.force_login(self.plain)
        response = self.client.post(reverse('upload:backfill_upload'))

        self.assertEqual(response.status_code, 200)
        run = UploadRun.objects.get(pk=response.json()['run_id'])
        self.assertTrue(run.is_backfill)
        self.assertEqual(run.mode, 'single')
        self.assertEqual(run.triggered_by, 'manual')
        mock_spawn.assert_called_once()

    @mock.patch('upload.services.spawn_run')
    def test_backfill_in_multi_mode_is_staff_only(self, mock_spawn):
        AppSettings.objects.update_or_create(pk=1, defaults={'multi_facility_enabled': True})
        self.client.force_login(self.plain)

        response = self.client.post(reverse('upload:backfill_upload'))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(UploadRun.objects.exists())
        mock_spawn.assert_not_called()

    @mock.patch('upload.services.spawn_run')
    def test_retrying_a_backfill_stays_a_backfill(self, mock_spawn):
        kilifi = Facility.objects.create(name='Kilifi', host='k', username='u',
                                         password_encrypted='x')
        run = services.create_run(date(2026, 8, 5), date(2026, 8, 5), triggered_by='cron',
                                  mode='multi', facilities=[kilifi], is_backfill=True)
        run.logs.update(status='failed')
        UploadRun.objects.filter(pk=run.pk).update(status='partial', facilities_failed=1)

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('upload:run_retry_failed', kwargs={'run_id': run.pk}),
        )

        self.assertEqual(response.status_code, 200)
        retry = UploadRun.objects.get(pk=response.json()['run_id'])
        self.assertTrue(retry.is_backfill)

    @mock.patch('upload.services.spawn_run')
    def test_multi_upload_without_facilities_is_rejected(self, mock_spawn):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('upload:multi_upload'),
                                    {'date_from': '2026-01-01', 'date_to': '2026-01-02'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('No active facilities', response.json()['error'])
        mock_spawn.assert_not_called()

    @mock.patch('upload.views.openmrs.probe')
    def test_test_connection_rejects_an_mfl_another_facility_already_claims(self, mock_probe):
        Facility.objects.create(name='Kilifi', host='k', username='u',
                                password_encrypted='x', mfl_code='12345')
        mock_probe.return_value = {
            'ok': True, 'message': 'Connected', 'mfl': '12345',
            'facility_name': 'Kilifi Clone', 'candidates': [], 'mysql_version': '8.0',
        }
        self.client.force_login(self.staff)

        response = self.client.post(reverse('upload:facility_test'), {
            'name': 'Clone', 'host': 'clone', 'port': '3306',
            'database_name': 'openmrs', 'username': 'u', 'password': 'pw',
        })

        data = response.json()
        self.assertFalse(data['ok'])
        self.assertIn('already used by "Kilifi"', data['message'])

    @mock.patch('upload.views.openmrs.probe')
    def test_test_connection_stores_the_discovered_mfl(self, mock_probe):
        facility = Facility.objects.create(name='Kilifi', host='k', username='u',
                                           password_encrypted='x')
        mock_probe.return_value = {
            'ok': True, 'message': 'Connected', 'mfl': '12345',
            'facility_name': 'Kilifi Sub-District', 'candidates': [], 'mysql_version': '8.0',
        }
        self.client.force_login(self.staff)

        self.client.post(reverse('upload:facility_test'), {
            'pk': facility.pk, 'name': 'Kilifi', 'host': 'k', 'port': '3306',
            'database_name': 'openmrs', 'username': 'u', 'password': 'pw',
        })

        facility.refresh_from_db()
        self.assertEqual(facility.mfl_code, '12345')
        self.assertTrue(facility.last_test_ok)
        self.assertIsNotNone(facility.last_tested_at)

    def test_editing_a_facility_without_retyping_the_password_keeps_it(self):
        facility = Facility.objects.create(name='Kilifi', host='old-host', username='u')
        facility.set_password('hunter2')
        facility.save()
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse('upload:facility_edit', kwargs={'pk': facility.pk}),
            {'name': 'Kilifi', 'host': 'new-host', 'port': '3307',
             'database_name': 'openmrs', 'username': 'u', 'password': '', 'is_active': 'on'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Facility.objects.count(), 1)
        facility.refresh_from_db()
        self.assertEqual(facility.host, 'new-host')
        self.assertEqual(facility.port, 3307)
        self.assertEqual(facility.get_password(), 'hunter2')

    def test_failed_edit_rerenders_posting_back_to_the_edit_url(self):
        Facility.objects.create(name='Taken', host='t', username='u', password_encrypted='x')
        facility = Facility.objects.create(name='Kilifi', host='k', username='u',
                                           password_encrypted='x')
        self.client.force_login(self.staff)

        response = self.client.post(
            reverse('upload:facility_edit', kwargs={'pk': facility.pk}),
            {'name': 'Taken', 'host': 'k', 'port': '3306',
             'database_name': 'openmrs', 'username': 'u', 'password': ''},
        )

        self.assertEqual(response.status_code, 400)
        # Re-rendering with the add URL would turn a rejected edit into a new row.
        edit_url = reverse('upload:facility_edit', kwargs={'pk': facility.pk})
        self.assertContains(response, 'action="{}"'.format(edit_url), status_code=400)
        self.assertEqual(Facility.objects.count(), 2)
        # The errors live inside the modal, so it has to come back open.
        self.assertContains(response, 'modal-backdrop open', status_code=400)

    def test_facility_modal_is_closed_on_a_plain_page_load(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse('upload:multi_facilities'))
        self.assertContains(response, 'id="facility-modal"')
        self.assertNotContains(response, 'modal-backdrop open')

    def test_adding_a_facility_requires_a_password(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('upload:facility_add'), {
            'name': 'Kilifi', 'host': 'k', 'port': '3306',
            'database_name': 'openmrs', 'username': 'u', 'password': '',
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Facility.objects.exists())

    def test_retry_failed_needs_something_to_retry(self):
        run = UploadRun.objects.create(
            date_from=date(2026, 1, 1), date_to=date(2026, 1, 2),
            mode='multi', triggered_by='manual', status='success',
        )
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('upload:run_retry_failed', kwargs={'run_id': run.pk}),
        )
        self.assertEqual(response.status_code, 400)

    @mock.patch('upload.services.spawn_run')
    def test_retry_failed_reruns_only_the_failed_facilities(self, mock_spawn):
        kilifi = Facility.objects.create(name='Kilifi', host='k', username='u', password_encrypted='x')
        malindi = Facility.objects.create(name='Malindi', host='m', username='u', password_encrypted='x')
        run = services.create_run(date(2026, 1, 1), date(2026, 1, 2), triggered_by='manual',
                                  mode='multi', facilities=[kilifi, malindi])
        run.logs.filter(facility=kilifi).update(status='failed')
        run.logs.filter(facility=malindi).update(status='success')
        UploadRun.objects.filter(pk=run.pk).update(status='partial', facilities_failed=1)

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('upload:run_retry_failed', kwargs={'run_id': run.pk}),
        )

        self.assertEqual(response.status_code, 200)
        retry = UploadRun.objects.get(pk=response.json()['run_id'])
        self.assertEqual(retry.retry_of_id, run.pk)
        self.assertEqual(retry.date_from, run.date_from)
        self.assertEqual([log.facility_label for log in retry.logs.all()], ['Kilifi'])
        mock_spawn.assert_called_once()


class SpawnRunTests(SimpleTestCase):
    """How the upload subprocess is detached, per platform."""

    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    CREATE_NEW_PROCESS_GROUP = 0x00000200

    def spawn_as(self, os_name):
        with mock.patch('upload.services.os.name', os_name), \
                mock.patch('upload.services.subprocess.Popen') as popen:
            services.spawn_run(7)
        return popen.call_args

    def test_windows_gets_no_console_window(self):
        flags = self.spawn_as('nt').kwargs['creationflags']

        self.assertTrue(flags & self.CREATE_NO_WINDOW)
        self.assertTrue(flags & self.CREATE_NEW_PROCESS_GROUP)
        # DETACHED_PROCESS reads as though it suppresses the console but in fact
        # allocates a new one — a blank window for the length of the upload — and
        # combining the two makes Windows ignore CREATE_NO_WINDOW entirely.
        self.assertFalse(flags & self.DETACHED_PROCESS)

    def test_posix_gets_its_own_session_instead(self):
        call = self.spawn_as('posix')

        self.assertTrue(call.kwargs['start_new_session'])
        self.assertNotIn('creationflags', call.kwargs)

    def test_the_child_is_told_which_run_to_execute(self):
        command = self.spawn_as('posix').args[0]

        self.assertEqual(command[-3:], ['upload_appointments', '--run-id', '7'])


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', OPENMRS_DB_LABEL='Env facility')
class ExecuteRunTests(TestCase):
    def test_facility_with_an_undecryptable_password_fails_without_blocking_the_rest(self):
        good = Facility.objects.create(name='Good', host='g', username='u')
        good.set_password('pw')
        good.save()
        Facility.objects.create(name='Corrupt', host='c', username='u',
                                password_encrypted='not-a-valid-fernet-token')

        run = services.create_run(date(2026, 1, 1), date(2026, 1, 2),
                                  triggered_by='cron', mode='multi')

        with mock.patch('upload.services.upload_facility') as mock_upload:
            mock_upload.side_effect = lambda log, config, tokens, backfill=False: (
                UploadLog.objects.filter(pk=log.pk).update(status='success')
            )
            services.execute_run(run, workers=1)

        run.refresh_from_db()
        self.assertEqual(run.status, 'partial')
        self.assertEqual(run.facilities_failed, 1)
        corrupt = run.logs.get(facility_label='Corrupt')
        self.assertEqual(corrupt.status, 'failed')
        self.assertIn('could not be decrypted', corrupt.error_message)
        # The healthy facility was still attempted.
        self.assertEqual(mock_upload.call_count, 1)

    def test_run_with_no_facilities_reports_why(self):
        run = services.create_run(date(2026, 1, 1), date(2026, 1, 2),
                                  triggered_by='cron', mode='multi', facilities=[])
        services.execute_run(run)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('No active facilities', run.message)


class FetchAppointmentsQueryTests(SimpleTestCase):
    """Which of the two query variants runs, and with which parameters."""

    class Cursor:
        description = [('patient_id',), ('appointment_type',)]

        def __init__(self):
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append((sql, params))

        def fetchall(self):
            return ()

        def close(self):
            pass

    def _fetch(self, *args):
        cursor = self.Cursor()
        conn = mock.Mock()
        conn.cursor.return_value = cursor
        openmrs.fetch_appointments(conn, *args)
        return cursor.executed[0]

    def test_a_date_range_filters_on_when_the_appointment_was_booked(self):
        sql, params = self._fetch(date(2026, 1, 1), date(2026, 1, 2))
        self.assertIn('date_appointment_scheduled between', sql)
        self.assertEqual(params, ['2026-01-01', '2026-01-02'])

    def test_no_dates_runs_the_unfiltered_backfill_query(self):
        sql, params = self._fetch()
        self.assertNotIn('date_appointment_scheduled between', sql)
        self.assertIsNone(params)

    def test_both_variants_keep_the_consent_and_pending_filters(self):
        for sql in (openmrs.APPOINTMENT_QUERY, openmrs.APPOINTMENT_BACKFILL_QUERY):
            self.assertIn("data.consented='Yes'", sql)
            self.assertIn("x.start_date_time > now()", sql)
            self.assertIn("x.status != 'Cancelled'", sql)


class FetchPatientUpdatesTests(SimpleTestCase):
    """The patient-detail query: what it selects, and what it maps to."""

    COLUMNS = [
        'patient_id', 'ccc_number', 'phone_number', 'facility_mfl', 'facility_name',
        'consented', 'risk_classification_value', 'risk_classification',
        'risk_factors', 'risk_classification_date', 'last_viral_load',
        'last_viral_load_date', 'last_cd4', 'last_cd4_date',
    ]

    class Cursor:
        def __init__(self, columns, rows):
            self.description = [(name,) for name in columns]
            self.rows = rows
            self.executed = []

        def execute(self, sql, params=None):
            self.executed.append((sql, params))

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    def _fetch(self, rows=()):
        cursor = self.Cursor(self.COLUMNS, rows)
        conn = mock.Mock()
        conn.cursor.return_value = cursor
        return openmrs.fetch_patient_updates(conn), cursor

    def test_runs_unfiltered_by_date_so_every_run_refreshes_everybody(self):
        _, cursor = self._fetch()
        sql, params = cursor.executed[0]
        self.assertNotIn('date_appointment_scheduled between', sql)
        self.assertIsNone(params)

    def test_keeps_the_consent_and_pending_filters(self):
        self.assertIn("data.consented='Yes'", openmrs.PATIENT_UPDATE_QUERY)
        self.assertIn('x.start_date_time > now()', openmrs.PATIENT_UPDATE_QUERY)
        self.assertIn("x.status != 'Cancelled'", openmrs.PATIENT_UPDATE_QUERY)

    def test_shares_its_ctes_with_the_appointment_query(self):
        # One copy of the CTEs, so a fix to the viral-load logic reaches both.
        for query in (openmrs.APPOINTMENT_QUERY, openmrs.PATIENT_UPDATE_QUERY):
            self.assertIn(openmrs._COMMON_CTES, query)

    def test_row_is_mapped_to_the_update_payload(self):
        row = ('12', 'CCC-1', '0722000000', '15234', 'Kilifi County Hospital',
               'Yes', '0.42', 'High Risk', 'missed appointments', '2026-09-01',
               '0.00', '2026-08-02', '350', '2026-07-03')
        patients, _ = self._fetch([row])

        self.assertEqual(patients, [{
            'patient_id': '12',
            'ccc_number': 'CCC-1',
            'facility_mfl': '15234',
            'facility_name': 'Kilifi County Hospital',
            'phone_number': '0722000000',
            'risk_classification': 'High Risk',
            'risk_classification_value': 0.42,
            'risk_classification_date': '2026-09-01',
            'risk_factors': 'missed appointments',
            'last_viral_load': '0.00',
            'last_viral_load_date': '2026-08-02',
            'last_cd4': '350',
            'last_cd4_date': '2026-07-03',
        }])

    def test_a_patient_never_scored_still_uploads(self):
        # A blank risk score must not drop the row: the phone number is the point.
        row = ('12', 'CCC-1', '0722000000', '15234', 'Kilifi', 'Yes',
               None, None, None, None, None, None, None, None)
        patients, _ = self._fetch([row])

        self.assertEqual(patients[0]['risk_classification'], 'Unknown')
        self.assertEqual(patients[0]['risk_classification_value'], 0.0)
        self.assertEqual(patients[0]['last_viral_load'], '')


@override_settings(CHQI_API_BASE_URL='https://api.test',
                   FIELD_ENCRYPTION_KEY='unit-test-key',
                   OPENMRS_DB_LABEL='Env facility', UPLOAD_BATCH_SIZE=2)
class UploadFacilityTests(TestCase):
    """Both halves of a facility's work: appointments, then patient updates."""

    def setUp(self):
        self.run = services.create_run(
            date(2026, 9, 19), date(2026, 9, 20), triggered_by='cron', mode='single',
        )
        self.log = self.run.logs.get()
        self.config = openmrs.FacilityConfig(
            label='Env facility', host='db', port=3306, user='u', password='p',
            database='openmrs',
        )

    @contextmanager
    def _openmrs(self, appointments, updates):
        conn = mock.Mock()
        with mock.patch.object(openmrs, 'connect') as connect, \
                mock.patch.object(openmrs, 'fetch_appointments',
                                  return_value=appointments) as fetch_appts, \
                mock.patch.object(openmrs, 'fetch_patient_updates',
                                  return_value=updates) as fetch_updates:
            connect.return_value.__enter__ = mock.Mock(return_value=conn)
            connect.return_value.__exit__ = mock.Mock(return_value=False)
            yield fetch_appts, fetch_updates

    @mock.patch('upload.services.requests.post')
    def test_both_endpoints_are_posted_to_and_both_counts_recorded(self, mock_post):
        mock_post.return_value = make_response(200)
        appointments = [{'patient_id': str(i)} for i in range(3)]
        updates = [{'patient_id': str(i)} for i in range(2)]

        with self._openmrs(appointments, updates):
            services.upload_facility(self.log, self.config, make_tokens('t'))

        urls = [call[0][0] for call in mock_post.call_args_list]
        self.assertEqual(urls, [
            'https://api.test/api/patients/upload-json',   # 2 batches of 2
            'https://api.test/api/patients/upload-json',
            'https://api.test/api/patients/update-json',   # 1 batch of 2
        ])
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'success')
        self.assertEqual(self.log.records_uploaded, 3)
        self.assertEqual(self.log.patient_updates_uploaded, 2)
        # One bar across both phases, finishing full rather than at two-thirds.
        self.assertEqual(self.log.batches_total, 3)
        self.assertEqual(self.log.batches_completed, 3)

    @mock.patch('upload.services.requests.post')
    def test_updates_are_sent_even_when_nothing_was_booked_in_the_window(self, mock_post):
        # The usual quiet night: nothing new booked, but risk scores moved.
        mock_post.return_value = make_response(200)

        with self._openmrs([], [{'patient_id': '1'}]):
            services.upload_facility(self.log, self.config, make_tokens('t'))

        urls = [call[0][0] for call in mock_post.call_args_list]
        self.assertEqual(urls, ['https://api.test/api/patients/update-json'])
        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'success')
        self.assertEqual(self.log.error_message, '')

    @mock.patch('upload.services.requests.post')
    def test_a_failed_update_upload_fails_the_facility(self, mock_post):
        # Appointments through, updates rejected: the facility is not "done", and
        # "Retry failed" is what re-runs it.
        mock_post.side_effect = [make_response(200), make_response(400, text='bad')]

        with self._openmrs([{'patient_id': '1'}], [{'patient_id': '1'}]):
            services.upload_facility(self.log, self.config, make_tokens('t'))

        self.log.refresh_from_db()
        self.assertEqual(self.log.status, 'failed')
        self.assertIn('HTTP 400', self.log.error_message)

    @mock.patch('upload.services.requests.post')
    def test_a_backfill_still_sends_the_unfiltered_patient_updates(self, mock_post):
        mock_post.return_value = make_response(200)

        with self._openmrs([{'patient_id': '1'}], [{'patient_id': '1'}]) as (appts, updates):
            services.upload_facility(self.log, self.config, make_tokens('t'), backfill=True)

        # The appointment half drops its dates for a backfill; the update half
        # has no dates to drop.
        self.assertEqual(appts.call_args[0][1:], ())
        self.assertEqual(updates.call_count, 1)


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', OPENMRS_DB_LABEL='Env facility')
class BackfillRunTests(TestCase):
    """The one-off initial load: when it runs, and when it counts as done."""

    def _facility(self, name):
        facility = Facility.objects.create(name=name, host=name, username='u')
        facility.set_password('pw')
        facility.save()
        return facility

    def _backfill_run(self, facilities, statuses, mode='multi'):
        run = services.create_run(
            date(2026, 8, 5), date(2026, 8, 5), triggered_by='cron', mode=mode,
            facilities=facilities, is_backfill=True,
        )
        for log, status in zip(run.logs.order_by('pk'), statuses):
            UploadLog.objects.filter(pk=log.pk).update(status=status)
        return run

    def test_a_completed_backfill_marks_the_deployment_done(self):
        facilities = [self._facility('Kilifi'), self._facility('Malindi')]
        run = self._backfill_run(facilities, ['success', 'success'])

        services._finalize_run(run)

        app_settings = AppSettings.load()
        self.assertTrue(app_settings.initial_backfill_done)
        self.assertEqual(app_settings.initial_backfill_run_id, run.pk)
        self.assertIsNotNone(app_settings.initial_backfill_at)

    def test_a_partial_backfill_still_counts(self):
        # Three unreachable containers out of a hundred is the normal case;
        # re-uploading full history nightly to chase them would be worse.
        facilities = [self._facility('Kilifi'), self._facility('Malindi')]
        run = self._backfill_run(facilities, ['success', 'failed'])

        services._finalize_run(run)

        self.assertTrue(AppSettings.load().initial_backfill_done)

    def test_a_backfill_of_one_facility_does_not_mark_the_deployment_done(self):
        kilifi = self._facility('Kilifi')
        self._facility('Malindi')
        run = self._backfill_run([kilifi], ['success'])

        services._finalize_run(run)

        self.assertFalse(AppSettings.load().initial_backfill_done)

    def test_a_failed_backfill_does_not_mark_the_deployment_done(self):
        facilities = [self._facility('Kilifi')]
        run = self._backfill_run(facilities, ['failed'])

        services._finalize_run(run)

        self.assertFalse(AppSettings.load().initial_backfill_done)

    def test_a_dated_run_never_touches_the_flag(self):
        facilities = [self._facility('Kilifi')]
        run = services.create_run(date(2026, 1, 1), date(2026, 1, 2),
                                  triggered_by='cron', mode='multi', facilities=facilities)
        run.logs.update(status='success')

        services._finalize_run(run)

        self.assertFalse(AppSettings.load().initial_backfill_done)

    def test_uploading_a_facility_in_backfill_mode_drops_the_dates_and_stamps_it(self):
        facility = self._facility('Kilifi')
        run = self._backfill_run([facility], ['pending'])
        log = run.logs.get()

        @contextmanager
        def fake_connect(config):
            yield mock.Mock()

        with mock.patch.object(openmrs, 'connect', fake_connect), \
                mock.patch.object(openmrs, 'fetch_appointments', return_value=[]) as fetch, \
                mock.patch.object(openmrs, 'fetch_patient_updates', return_value=[]):
            services.upload_facility(log, facility.as_config(), mock.Mock(), backfill=True)

        # No date arguments at all — the backfill query takes none.
        self.assertEqual(fetch.call_args[0][1:], ())
        facility.refresh_from_db()
        self.assertIsNotNone(facility.initial_backfill_at)

    def test_a_dated_upload_leaves_the_facility_stamp_alone(self):
        facility = self._facility('Kilifi')
        run = services.create_run(date(2026, 1, 1), date(2026, 1, 2),
                                  triggered_by='cron', mode='multi', facilities=[facility])
        log = run.logs.get()

        @contextmanager
        def fake_connect(config):
            yield mock.Mock()

        with mock.patch.object(openmrs, 'connect', fake_connect), \
                mock.patch.object(openmrs, 'fetch_appointments', return_value=[]) as fetch, \
                mock.patch.object(openmrs, 'fetch_patient_updates', return_value=[]):
            services.upload_facility(log, facility.as_config(), mock.Mock())

        self.assertEqual(fetch.call_args[0][1:], (log.date_from, log.date_to))
        facility.refresh_from_db()
        self.assertIsNone(facility.initial_backfill_at)


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', OPENMRS_DB_LABEL='Env facility')
class UploadCommandBackfillTests(TestCase):
    """What the nightly cron job decides to upload."""

    def setUp(self):
        patcher = mock.patch('upload.services.execute_run', side_effect=self._succeed)
        self.execute = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _succeed(run, workers=None):
        # Stand in for the real upload, so _report() sees a finished run rather
        # than a pending one and does not exit(1).
        UploadRun.objects.filter(pk=run.pk).update(status='success')
        return run

    def _run(self, **kwargs):
        call_command('upload_appointments', **kwargs)
        return UploadRun.objects.latest('pk')

    def test_the_first_cron_job_uploads_everything_pending(self):
        run = self._run()
        self.assertTrue(run.is_backfill)
        self.assertEqual(run.period_label, 'All pending appointments')

    def test_later_cron_jobs_upload_the_nightly_window(self):
        AppSettings.objects.update_or_create(pk=1, defaults={'initial_backfill_done': True})

        run = self._run()

        self.assertFalse(run.is_backfill)
        self.assertEqual(run.date_to, date.today())
        self.assertEqual(run.date_from, date.today() - timedelta(days=1))

    def test_an_explicit_date_range_is_never_turned_into_a_backfill(self):
        # The flag is unset, but someone asking for specific dates means them.
        run = self._run(date_from='2026-01-01', date_to='2026-01-02')

        self.assertFalse(run.is_backfill)
        self.assertEqual(run.date_from, date(2026, 1, 1))

    def test_backfill_can_be_forced_after_it_has_already_happened(self):
        AppSettings.objects.update_or_create(pk=1, defaults={'initial_backfill_done': True})

        run = self._run(backfill=True)

        self.assertTrue(run.is_backfill)

    def test_backfill_with_a_date_range_is_refused(self):
        with self.assertRaises(CommandError) as ctx:
            call_command('upload_appointments', backfill=True, date_from='2026-01-01')
        self.assertIn('one or the other', str(ctx.exception))
        self.assertFalse(UploadRun.objects.exists())


class AppSettingsTests(TestCase):
    def test_load_is_idempotent_and_always_one_row(self):
        first = AppSettings.load()
        first.multi_facility_enabled = True
        first.save()

        AppSettings.load()
        AppSettings().save()

        self.assertEqual(AppSettings.objects.count(), 1)
        self.assertFalse(AppSettings.load().multi_facility_enabled)

    def test_cron_mode_reflects_the_enabled_flag(self):
        settings_obj = AppSettings.load()
        self.assertEqual(settings_obj.cron_mode(), 'single')

        settings_obj.multi_facility_enabled = True
        self.assertEqual(settings_obj.cron_mode(), 'multi')

        settings_obj.multi_tenant_enabled = True
        # Both set is not reachable through the UI, but it must still resolve to
        # one mode rather than uploading twice or not at all.
        self.assertEqual(settings_obj.cron_mode(), 'tenant')


# ------------------------------------------------------------------ multi-tenant

def probe_result(mfl='', name='', ok=None, message=''):
    """A canned openmrs.probe() return value."""
    ok = bool(mfl) if ok is None else ok
    return {
        'ok': ok,
        'message': message or ('Connected' if ok else 'no MFL code'),
        'mfl': mfl,
        'facility_name': name,
        'candidates': [],
        'mysql_version': '8.0.35',
    }


class ListDatabasesTests(SimpleTestCase):
    config = openmrs.FacilityConfig(
        label='Cloud', host='h', port=3306, user='u', password='p',
    )

    class Cursor:
        def __init__(self, names):
            self.names = names
            self._rows = ()

        def execute(self, sql, params=None):
            self._rows = (('8.0.35',),) if 'VERSION()' in sql else tuple(
                (n,) for n in self.names
            )

        def fetchone(self):
            return self._rows[0]

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    def with_databases(self, names):
        conn = mock.Mock()
        conn.cursor.return_value = self.Cursor(names)

        @contextmanager
        def fake_connect(config):
            yield conn

        return mock.patch.object(openmrs, 'connect', fake_connect)

    def test_the_prefix_selects_tenants_and_never_system_schemas(self):
        with self.with_databases([
            'information_schema', 'mysql', 'performance_schema', 'sys',
            'openmrs_kilifi', 'openmrs_malindi', 'wordpress', 'openmrs',
        ]):
            found = openmrs.list_databases(self.config, 'openmrs_')

        # 'openmrs' itself is short of the prefix; the underscore is matched as a
        # literal, not as the LIKE wildcard it would be in SQL.
        self.assertEqual(found, ['openmrs_kilifi', 'openmrs_malindi'])

    def test_an_empty_prefix_means_everything_except_the_system_schemas(self):
        with self.with_databases(['mysql', 'sys', 'openmrs_kilifi', 'wordpress']):
            self.assertEqual(
                openmrs.list_databases(self.config, ''),
                ['openmrs_kilifi', 'wordpress'],
            )

    def test_prefix_matching_ignores_case(self):
        # MySQL lower-cases schema names on some platforms and not others.
        with self.with_databases(['OpenMRS_Kilifi']):
            self.assertEqual(
                openmrs.list_databases(self.config, 'openmrs_'), ['OpenMRS_Kilifi'],
            )

    def test_probe_server_reports_a_prefix_that_matches_nothing(self):
        with self.with_databases(['mysql', 'wordpress']):
            result = openmrs.probe_server(self.config, 'openmrs_')

        self.assertFalse(result['ok'])
        self.assertIn('none of the 1 database(s)', result['message'])

    def test_probe_server_reports_what_it_found(self):
        with self.with_databases(['openmrs_a', 'openmrs_b', 'wordpress']):
            result = openmrs.probe_server(self.config, 'openmrs_')

        self.assertTrue(result['ok'])
        self.assertEqual(result['databases'], ['openmrs_a', 'openmrs_b'])
        self.assertIn('2 of 3', result['message'])

    def test_a_connection_failure_is_reported_not_raised(self):
        @contextmanager
        def boom(config):
            raise OSError('connection refused')
            yield  # pragma: no cover

        with mock.patch.object(openmrs, 'connect', boom):
            result = openmrs.probe_server(self.config, 'openmrs_')

        self.assertFalse(result['ok'])
        self.assertIn('Could not connect', result['message'])


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', TENANT_PROBE_WORKERS=1)
class TenantSyncTests(TestCase):
    """Reconciling one server's schema list into Facility rows."""

    def setUp(self):
        self.server = TenantServer.objects.create(
            name='Cloud', host='cloud.example', username='readonly',
            database_prefix='openmrs_',
        )
        self.server.set_password('pw')
        self.server.save()

    def sync(self, databases, probes=None, **kwargs):
        probes = probes or {}
        with mock.patch.object(openmrs, 'list_databases', return_value=databases), \
                mock.patch.object(
                    openmrs, 'probe',
                    side_effect=lambda config: probes.get(config.database, probe_result()),
                ):
            return tenants.sync_server(self.server, **kwargs)

    def enable(self, database='openmrs_kilifi'):
        """Stand in for an operator switching a discovered database on."""
        Facility.objects.filter(database_name=database).update(
            is_active=True, disabled_by_sync=False, activated_at=timezone.now(),
        )

    def test_schemas_become_facilities_named_by_what_they_report(self):
        summary = self.sync(
            ['openmrs_kilifi', 'openmrs_malindi'],
            {
                'openmrs_kilifi': probe_result('12345', 'Kilifi Dispensary'),
                'openmrs_malindi': probe_result('67890', 'Malindi Health Centre'),
            },
        )

        self.assertTrue(summary['ok'])
        self.assertEqual(summary['added'], 2)
        kilifi = Facility.objects.get(database_name='openmrs_kilifi')
        self.assertEqual(kilifi.name, 'Kilifi Dispensary')
        self.assertEqual(kilifi.mfl_code, '12345')
        self.assertEqual(kilifi.server, self.server)
        self.assertIsNotNone(kilifi.last_seen_at)

    def test_a_newly_discovered_schema_is_listed_but_left_switched_off(self):
        # A server may hold schemas that are demos, archives, or simply not this
        # deployment's to upload. Sync says what is there; an operator says what
        # uploads.
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})

        facility = Facility.objects.get(database_name='openmrs_kilifi')
        self.assertFalse(facility.is_active)
        self.assertIsNone(facility.activated_at)
        # Nothing is wrong with it — it identified cleanly — so it must not read
        # as auto-disabled either.
        self.assertFalse(facility.disabled_by_sync)
        self.assertEqual(facility.mfl_code, '12345')
        self.assertTrue(facility.last_test_ok)

    def test_a_schema_nobody_enabled_is_never_switched_on_by_a_later_sync(self):
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result(message='down')})
        self.assertTrue(
            Facility.objects.get(database_name='openmrs_kilifi').disabled_by_sync,
        )

        # Recovering clears the problem, but the choice was never made.
        summary = self.sync(
            ['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')},
        )

        self.assertEqual(summary['reappeared'], 0)
        self.assertFalse(Facility.objects.get(database_name='openmrs_kilifi').is_active)

    def test_enabling_one_database_leaves_the_rest_switched_off(self):
        probes = {
            'openmrs_kilifi': probe_result('12345', 'Kilifi'),
            'openmrs_malindi': probe_result('67890', 'Malindi'),
        }
        self.sync(['openmrs_kilifi', 'openmrs_malindi'], probes)
        self.enable('openmrs_kilifi')

        self.sync(['openmrs_kilifi', 'openmrs_malindi'], probes)

        self.assertEqual(
            list(Facility.objects.tenants().filter(is_active=True)
                 .values_list('database_name', flat=True)),
            ['openmrs_kilifi'],
        )

    def test_a_tenant_connects_through_its_server_not_its_own_row(self):
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})

        config = Facility.objects.get(database_name='openmrs_kilifi').as_config()
        self.assertEqual(config.host, 'cloud.example')
        self.assertEqual(config.user, 'readonly')
        self.assertEqual(config.password, 'pw')
        self.assertEqual(config.database, 'openmrs_kilifi')

    def test_editing_the_server_password_reaches_every_tenant_at_once(self):
        self.sync(['openmrs_a', 'openmrs_b'], {
            'openmrs_a': probe_result('1', 'A'), 'openmrs_b': probe_result('2', 'B'),
        })
        self.server.set_password('rotated')
        self.server.save()

        for facility in Facility.objects.tenants().select_related('server'):
            self.assertEqual(facility.as_config().password, 'rotated')

    def test_a_schema_that_cannot_be_identified_is_created_disabled(self):
        summary = self.sync(
            ['openmrs_broken'],
            {'openmrs_broken': probe_result(message='no default location set')},
        )

        self.assertEqual(summary['unidentified'], 1)
        self.assertEqual(len(summary['problems']), 1)
        facility = Facility.objects.get(database_name='openmrs_broken')
        # Uploading under a blank MFL would be rejected upstream, so it is kept
        # visible but out of the run.
        self.assertFalse(facility.is_active)
        self.assertTrue(facility.disabled_by_sync)
        self.assertEqual(facility.mfl_code, '')
        self.assertEqual(facility.name, 'openmrs_broken')

    def test_a_probe_failure_takes_the_mfl_but_leaves_the_reported_name(self):
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})

        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result(message='down')})

        facility = Facility.objects.get(database_name='openmrs_kilifi')
        self.assertEqual(facility.mfl_code, '')
        self.assertFalse(facility.is_active)
        self.assertEqual(facility.mfl_facility_name, 'Kilifi')

    def test_a_recovered_facility_follows_a_rename_upstream(self):
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})
        self.enable()
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result(message='down')})

        self.sync(
            ['openmrs_kilifi'],
            {'openmrs_kilifi': probe_result('12345', 'Kilifi Sub-District')},
        )

        facility = Facility.objects.get(database_name='openmrs_kilifi')
        self.assertEqual(facility.name, 'Kilifi Sub-District')
        self.assertTrue(facility.is_active)

    def test_a_duplicate_mfl_disables_the_second_schema_rather_than_overwriting(self):
        Facility.objects.create(name='Kilifi Container', host='k', username='u',
                                password_encrypted='x', mfl_code='12345')

        summary = self.sync(
            ['openmrs_clone'], {'openmrs_clone': probe_result('12345', 'Kilifi Clone')},
        )

        self.assertEqual(summary['unidentified'], 1)
        self.assertIn('already used by "Kilifi Container"', summary['problems'][0])
        clone = Facility.objects.get(database_name='openmrs_clone')
        self.assertEqual(clone.mfl_code, '')
        self.assertFalse(clone.is_active)

    def test_two_schemas_claiming_one_mfl_do_not_both_upload(self):
        summary = self.sync(['openmrs_a', 'openmrs_b'], {
            'openmrs_a': probe_result('12345', 'Kilifi'),
            'openmrs_b': probe_result('12345', 'Kilifi'),
        })

        self.assertEqual(summary['unidentified'], 1)
        self.assertEqual(Facility.objects.filter(mfl_code='12345').count(), 1)
        # Both start switched off; only the one holding the MFL is enableable,
        # and the other carries the reason it is not.
        loser = Facility.objects.get(mfl_code='')
        self.assertTrue(loser.disabled_by_sync)
        self.assertIn('already used by', loser.last_test_message)

    def test_a_schema_that_disappears_is_deactivated_not_deleted(self):
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})

        summary = self.sync([])

        self.assertEqual(summary['disappeared'], 1)
        facility = Facility.objects.get(database_name='openmrs_kilifi')
        self.assertFalse(facility.is_active)
        self.assertTrue(facility.disabled_by_sync)
        self.assertIn('no longer on Cloud', facility.last_test_message)

    def test_a_schema_that_comes_back_is_re_enabled(self):
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})
        self.enable()
        self.sync([])

        summary = self.sync(
            ['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')},
        )

        self.assertEqual(summary['reappeared'], 1)
        facility = Facility.objects.get(database_name='openmrs_kilifi')
        self.assertTrue(facility.is_active)
        self.assertFalse(facility.disabled_by_sync)

    def test_a_renamed_schema_can_hand_its_mfl_to_its_replacement(self):
        self.sync(['openmrs_old'], {'openmrs_old': probe_result('12345', 'Kilifi')})

        # The same facility, moved to a new schema name. The retired row must
        # release the MFL in the same sync or the new one cannot claim it.
        summary = self.sync(['openmrs_new'], {'openmrs_new': probe_result('12345', 'Kilifi')})

        self.assertEqual(summary['problems'], [])
        self.assertEqual(
            Facility.objects.get(database_name='openmrs_new').mfl_code, '12345',
        )
        self.assertEqual(Facility.objects.get(database_name='openmrs_old').mfl_code, '')

    def test_a_facility_switched_off_by_hand_stays_off(self):
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})
        self.enable()
        Facility.objects.update(is_active=False, disabled_by_sync=False)

        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})

        self.assertFalse(Facility.objects.get(database_name='openmrs_kilifi').is_active)

    def test_a_hand_picked_name_survives_later_syncs(self):
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})
        Facility.objects.update(name='Kilifi — north wing')

        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})

        self.assertEqual(
            Facility.objects.get(database_name='openmrs_kilifi').name,
            'Kilifi — north wing',
        )

    def test_a_failed_probe_does_not_rename_an_identified_facility(self):
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})

        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result(message='down')})

        # Reverting to the schema name because the container was briefly down
        # would be churn, not information.
        self.assertEqual(
            Facility.objects.get(database_name='openmrs_kilifi').name, 'Kilifi',
        )

    def test_two_schemas_reporting_the_same_name_are_told_apart(self):
        self.sync(['openmrs_a', 'openmrs_b'], {
            'openmrs_a': probe_result('1', 'Health Centre'),
            'openmrs_b': probe_result('2', 'Health Centre'),
        })

        self.assertEqual(
            sorted(Facility.objects.values_list('name', flat=True)),
            ['Health Centre', 'Health Centre (openmrs_b)'],
        )

    def test_an_unreachable_server_changes_nothing_and_says_so(self):
        self.sync(['openmrs_kilifi'], {'openmrs_kilifi': probe_result('12345', 'Kilifi')})
        self.enable()

        with mock.patch.object(openmrs, 'list_databases',
                               side_effect=OSError('connection refused')):
            summary = tenants.sync_server(self.server)

        self.assertFalse(summary['ok'])
        self.assertIn('Could not list databases', summary['message'])
        # Last night's list is a better basis than no list at all.
        self.assertTrue(Facility.objects.get(database_name='openmrs_kilifi').is_active)
        self.server.refresh_from_db()
        self.assertFalse(self.server.last_sync_ok)

    def test_no_reprobe_only_identifies_what_is_new_or_still_unknown(self):
        self.sync(['openmrs_known'], {'openmrs_known': probe_result('12345', 'Known')})

        probes = {
            'openmrs_known': probe_result('12345', 'Known'),
            'openmrs_fresh': probe_result('67890', 'Fresh'),
        }
        with mock.patch.object(openmrs, 'list_databases',
                               return_value=['openmrs_known', 'openmrs_fresh']), \
                mock.patch.object(
                    openmrs, 'probe',
                    side_effect=lambda c: probes[c.database],
                ) as probe:
            tenants.sync_server(self.server, reprobe=False)

        self.assertEqual([c.args[0].database for c in probe.call_args_list],
                         ['openmrs_fresh'])
        self.assertEqual(Facility.objects.count(), 2)

    def test_sync_all_covers_active_servers_only(self):
        TenantServer.objects.create(name='Retired', host='r', username='u',
                                    password_encrypted='x', is_active=False)
        with mock.patch.object(tenants, 'sync_server', return_value={'ok': True}) as sync:
            results = tenants.sync_all()

        self.assertEqual([r[0].name for r in results], ['Cloud'])
        self.assertEqual(sync.call_count, 1)


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', OPENMRS_DB_LABEL='Env facility')
class FacilityModeScopingTests(TestCase):
    """Standalone containers and discovered tenants share a table, never a run."""

    def setUp(self):
        self.server = TenantServer.objects.create(
            name='Cloud', host='cloud', username='u', password_encrypted='x',
        )
        self.standalone = Facility.objects.create(
            name='Kilifi Container', host='k', username='u', password_encrypted='x',
        )
        self.tenant = Facility.objects.create(
            name='Malindi Tenant', host='cloud', username='u', server=self.server,
            database_name='openmrs_malindi',
        )

    def test_querysets_split_the_two_setups(self):
        self.assertEqual([f.pk for f in Facility.objects.standalone()], [self.standalone.pk])
        self.assertEqual([f.pk for f in Facility.objects.tenants()], [self.tenant.pk])
        self.assertEqual(list(Facility.objects.for_mode('multi')),
                         list(Facility.objects.standalone()))
        self.assertEqual(list(Facility.objects.for_mode('tenant')),
                         list(Facility.objects.tenants()))

    def test_for_mode_refuses_a_mode_that_has_no_facilities(self):
        with self.assertRaises(ValueError):
            Facility.objects.for_mode('single')

    def test_a_multi_facility_run_does_not_sweep_in_tenants(self):
        run = services.create_run(date(2026, 1, 1), date(2026, 1, 2),
                                  triggered_by='cron', mode='multi')

        self.assertEqual([log.facility_label for log in run.logs.all()],
                         ['Kilifi Container'])

    def test_a_tenant_run_does_not_sweep_in_standalone_facilities(self):
        run = services.create_run(date(2026, 1, 1), date(2026, 1, 2),
                                  triggered_by='cron', mode='tenant')

        self.assertEqual([log.facility_label for log in run.logs.all()],
                         ['Malindi Tenant'])

    def test_a_tenant_backfill_does_not_wait_on_standalone_facilities(self):
        # Deployment-wide "initial load done" is judged against the facilities
        # the run's own mode targets, not every row in the table.
        run = services.create_run(date(2026, 8, 5), date(2026, 8, 5),
                                  triggered_by='cron', mode='tenant', is_backfill=True)
        run.logs.update(status='success')

        services._finalize_run(run)

        self.assertTrue(AppSettings.load().initial_backfill_done)


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', OPENMRS_DB_LABEL='Env facility',
                   TENANT_PROBE_WORKERS=1)
class MultiTenantViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user('admin', password='pw', is_staff=True)
        self.plain = User.objects.create_user('nurse', password='pw')
        self.client.force_login(self.staff)

    def _server(self, name='Cloud'):
        server = TenantServer.objects.create(name=name, host='cloud', username='u')
        server.set_password('pw')
        server.save()
        return server

    def _tenant(self, server, database='openmrs_kilifi', name='Kilifi', mfl='12345'):
        return Facility.objects.create(
            name=name, host='cloud', username='u', server=server,
            database_name=database, mfl_code=mfl,
        )

    def test_the_page_is_staff_only(self):
        self.client.force_login(self.plain)
        self.assertEqual(self.client.get(reverse('upload:multi_tenant')).status_code, 302)

        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse('upload:multi_tenant')).status_code, 200)

    def test_each_page_wires_the_shared_run_panel_to_its_own_endpoint(self):
        # The upload panel and its script are one include shared by both pages;
        # pointing either at the other's endpoint would upload the wrong set.
        pages = {
            'upload:multi_facilities': 'upload:multi_upload',
            'upload:multi_tenant': 'upload:tenant_upload',
        }
        for page, upload_url in pages.items():
            with self.subTest(page=page):
                response = self.client.get(reverse(page))
                html = response.content.decode()
                self.assertIn(
                    'action="{}" id="multi-upload-form"'.format(reverse(upload_url)), html,
                )
                self.assertIn('id="backfill-form"', html)
                self.assertIn('window.runPanel', html)
                # {% include %} without `only`, so the forms inside still get a
                # CSRF token from the request context.
                self.assertIn('name="csrfmiddlewaretoken"', html)
                # The page-specific script leans on the shared one being there.
                self.assertLess(html.index('window.runPanel'), html.index('runPanel.onRetry'))

    def test_the_facility_modal_still_belongs_to_the_multi_facility_page_alone(self):
        response = self.client.get(reverse('upload:multi_facilities'))
        self.assertContains(response, 'id="facility-modal"')
        self.assertNotContains(response, 'id="server-modal"')

        response = self.client.get(reverse('upload:multi_tenant'))
        self.assertContains(response, 'id="server-modal"')
        self.assertNotContains(response, 'id="facility-modal"')

    def test_saving_a_server_discovers_its_databases_straight_away(self):
        with mock.patch('upload.views.tenants.sync_server') as sync:
            sync.return_value = {'ok': True, 'message': '2 database(s) found.'}
            response = self.client.post(reverse('upload:tenant_server_add'), {
                'name': 'Cloud', 'host': 'cloud', 'port': '3306', 'username': 'u',
                'password': 'pw', 'database_prefix': 'openmrs_', 'is_active': 'on',
            })

        self.assertEqual(response.status_code, 302)
        server = TenantServer.objects.get()
        self.assertEqual(server.get_password(), 'pw')
        sync.assert_called_once_with(server)

    def test_adding_a_server_requires_a_password(self):
        response = self.client.post(reverse('upload:tenant_server_add'), {
            'name': 'Cloud', 'host': 'cloud', 'port': '3306', 'username': 'u',
            'password': '', 'database_prefix': 'openmrs_',
        })

        self.assertEqual(response.status_code, 400)
        self.assertFalse(TenantServer.objects.exists())
        # The errors live inside the modal, so it has to come back open.
        self.assertContains(response, 'modal-backdrop open', status_code=400)

    def test_editing_a_server_without_retyping_the_password_keeps_it(self):
        server = self._server()

        with mock.patch('upload.views.tenants.sync_server',
                        return_value={'ok': True, 'message': 'done'}):
            response = self.client.post(
                reverse('upload:tenant_server_edit', kwargs={'pk': server.pk}),
                {'name': 'Cloud', 'host': 'new-host', 'port': '3307', 'username': 'u',
                 'password': '', 'database_prefix': 'openmrs_', 'is_active': 'on'},
            )

        self.assertEqual(response.status_code, 302)
        server.refresh_from_db()
        self.assertEqual(server.host, 'new-host')
        self.assertEqual(server.get_password(), 'pw')

    def test_deleting_a_server_takes_its_databases_but_leaves_the_history(self):
        server = self._server()
        facility = self._tenant(server)
        run = services.create_run(date(2026, 1, 1), date(2026, 1, 2),
                                  triggered_by='manual', mode='tenant',
                                  facilities=[facility])

        self.client.post(reverse('upload:tenant_server_delete', kwargs={'pk': server.pk}))

        self.assertFalse(Facility.objects.exists())
        log = run.logs.get()
        self.assertIsNone(log.facility_id)
        self.assertEqual(log.facility_label, 'Kilifi')

    def test_toggling_a_database_overrides_sync_for_good(self):
        server = self._server()
        facility = self._tenant(server)
        Facility.objects.filter(pk=facility.pk).update(
            is_active=False, disabled_by_sync=True,
        )

        self.client.post(reverse('upload:tenant_toggle', kwargs={'pk': facility.pk}))

        facility.refresh_from_db()
        self.assertTrue(facility.is_active)
        self.assertFalse(facility.disabled_by_sync)

    def test_enabling_a_database_records_that_somebody_chose_it(self):
        facility = self._tenant(self._server())
        Facility.objects.filter(pk=facility.pk).update(is_active=False)
        url = reverse('upload:tenant_toggle', kwargs={'pk': facility.pk})

        self.client.post(url)

        facility.refresh_from_db()
        chosen_at = facility.activated_at
        self.assertIsNotNone(chosen_at)

        # Switching it off again does not un-choose it: sync may put back what
        # sync took away, and only this stamp tells it that it may.
        self.client.post(url)
        facility.refresh_from_db()
        self.assertFalse(facility.is_active)
        self.assertEqual(facility.activated_at, chosen_at)

    def test_the_page_says_how_many_databases_are_waiting_to_be_enabled(self):
        server = self._server()
        self._tenant(server, 'openmrs_kilifi', 'Kilifi', '1')
        self._tenant(server, 'openmrs_malindi', 'Malindi', '2')
        Facility.objects.filter(database_name='openmrs_malindi').update(is_active=False)

        response = self.client.get(reverse('upload:multi_tenant'))

        self.assertEqual(response.context['awaiting_count'], 1)
        self.assertContains(response, 'Not enabled')

    def test_the_facility_form_cannot_reach_a_discovered_database(self):
        facility = self._tenant(self._server())

        response = self.client.post(
            reverse('upload:facility_edit', kwargs={'pk': facility.pk}),
            {'name': 'Hijacked', 'host': 'elsewhere', 'port': '3306',
             'database_name': 'openmrs', 'username': 'u', 'password': 'pw'},
        )

        self.assertEqual(response.status_code, 404)
        facility.refresh_from_db()
        self.assertEqual(facility.host, 'cloud')

    def test_the_multi_facility_page_does_not_list_tenants(self):
        self._tenant(self._server())
        Facility.objects.create(name='Standalone', host='s', username='u',
                                password_encrypted='x')

        response = self.client.get(reverse('upload:multi_facilities'))

        self.assertEqual([f.name for f in response.context['facilities']], ['Standalone'])

    @mock.patch('upload.services.spawn_run')
    def test_a_tenant_upload_covers_the_discovered_databases(self, mock_spawn):
        server = self._server()
        self._tenant(server, 'openmrs_kilifi', 'Kilifi', '1')
        Facility.objects.create(name='Standalone', host='s', username='u',
                                password_encrypted='x')

        response = self.client.post(reverse('upload:tenant_upload'),
                                    {'date_from': '2026-01-01', 'date_to': '2026-01-02'})

        self.assertEqual(response.status_code, 200)
        run = UploadRun.objects.get(pk=response.json()['run_id'])
        self.assertEqual(run.mode, 'tenant')
        self.assertEqual([log.facility_label for log in run.logs.all()], ['Kilifi'])

    @mock.patch('upload.services.spawn_run')
    def test_a_tenant_upload_with_nothing_discovered_says_so(self, mock_spawn):
        response = self.client.post(reverse('upload:tenant_upload'),
                                    {'date_from': '2026-01-01', 'date_to': '2026-01-02'})

        self.assertEqual(response.status_code, 400)
        self.assertIn('No tenant databases are enabled', response.json()['error'])
        mock_spawn.assert_not_called()

    @mock.patch('upload.services.spawn_run')
    def test_retrying_a_tenant_run_stays_a_tenant_run(self, mock_spawn):
        facility = self._tenant(self._server())
        run = services.create_run(date(2026, 1, 1), date(2026, 1, 2), triggered_by='manual',
                                  mode='tenant', facilities=[facility])
        run.logs.update(status='failed')
        UploadRun.objects.filter(pk=run.pk).update(status='failed', facilities_failed=1)

        response = self.client.post(
            reverse('upload:run_retry_failed', kwargs={'run_id': run.pk}),
        )

        retry = UploadRun.objects.get(pk=response.json()['run_id'])
        self.assertEqual(retry.mode, 'tenant')

    def test_enabling_one_mode_disables_the_other(self):
        self.client.post(reverse('upload:multi_settings'), {'multi_facility_enabled': 'on'})
        self.assertEqual(AppSettings.load().cron_mode(), 'multi')

        self.client.post(reverse('upload:tenant_settings'), {'multi_tenant_enabled': 'on'})

        app_settings = AppSettings.load()
        self.assertFalse(app_settings.multi_facility_enabled)
        self.assertEqual(app_settings.cron_mode(), 'tenant')

    def test_server_test_reports_the_matching_databases_without_saving(self):
        with mock.patch('upload.views.openmrs.probe_server') as probe:
            probe.return_value = {
                'ok': True, 'message': 'Connected', 'mysql_version': '8.0',
                'databases': ['openmrs_{}'.format(i) for i in range(30)],
            }
            response = self.client.post(reverse('upload:tenant_server_test'), {
                'name': 'Cloud', 'host': 'cloud', 'port': '3306', 'username': 'u',
                'password': 'pw', 'database_prefix': 'openmrs_',
            })

        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['count'], 30)
        # A hundred names is not something to paste into an alert box.
        self.assertEqual(len(data['sample']), 20)
        self.assertFalse(TenantServer.objects.exists())


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', OPENMRS_DB_LABEL='Env facility')
class UploadCommandTenantTests(TestCase):
    """What the nightly cron job does when multi-tenant mode is on."""

    def setUp(self):
        patcher = mock.patch('upload.services.execute_run', side_effect=self._succeed)
        self.execute = patcher.start()
        self.addCleanup(patcher.stop)

        AppSettings.objects.update_or_create(pk=1, defaults={
            'multi_tenant_enabled': True, 'initial_backfill_done': True,
        })
        self.server = TenantServer.objects.create(
            name='Cloud', host='cloud', username='u', password_encrypted='x',
        )

    @staticmethod
    def _succeed(run, workers=None):
        UploadRun.objects.filter(pk=run.pk).update(status='success')
        return run

    def _tenant(self, name='Kilifi', database='openmrs_kilifi'):
        return Facility.objects.create(
            name=name, host='cloud', username='u', server=self.server,
            database_name=database, mfl_code=name,
        )

    def test_the_nightly_run_refreshes_the_database_list_first(self):
        self._tenant()
        with mock.patch('upload.management.commands.upload_appointments.tenants.sync_all',
                        return_value=[]) as sync:
            call_command('upload_appointments')

        # reprobe=False: a container that already answered is not going to
        # change its MFL, and re-asking a hundred of them wastes the window.
        sync.assert_called_once_with(workers=None, reprobe=False)
        self.assertEqual(UploadRun.objects.get().mode, 'tenant')

    def test_no_sync_uploads_what_is_already_known(self):
        self._tenant()
        with mock.patch('upload.management.commands.upload_appointments.tenants.sync_all') as sync:
            call_command('upload_appointments', no_sync=True)

        sync.assert_not_called()
        self.assertEqual(UploadRun.objects.get().mode, 'tenant')

    def test_an_unreachable_server_does_not_stop_the_upload(self):
        self._tenant()
        summary = {'ok': False, 'message': 'Could not list databases: refused'}
        with mock.patch('upload.management.commands.upload_appointments.tenants.sync_all',
                        return_value=[(self.server, summary)]):
            call_command('upload_appointments')

        self.assertEqual(UploadRun.objects.get().mode, 'tenant')

    def test_a_tenant_mode_deployment_with_no_servers_refuses_to_run(self):
        TenantServer.objects.all().delete()
        with self.assertRaises(CommandError) as ctx:
            call_command('upload_appointments')
        self.assertIn('no active tenant servers', str(ctx.exception))

    def test_a_tenant_mode_deployment_with_nothing_discovered_refuses_to_run(self):
        with mock.patch('upload.management.commands.upload_appointments.tenants.sync_all',
                        return_value=[]):
            with self.assertRaises(CommandError) as ctx:
                call_command('upload_appointments')
        self.assertIn('no tenant database is switched on', str(ctx.exception))
        self.assertFalse(UploadRun.objects.exists())

    def test_a_single_facility_rerun_uses_the_mode_that_facility_belongs_to(self):
        facility = self._tenant()
        with mock.patch('upload.management.commands.upload_appointments.tenants.sync_all',
                        return_value=[]):
            call_command('upload_appointments', facility=facility.pk)

        self.assertEqual(UploadRun.objects.get().mode, 'tenant')


# ------------------------------------------------------- catch-up scheduling

@override_settings(UPLOAD_CATCH_UP_DAYS=30, UPLOAD_RETRY_BASE_MINUTES=30,
                   UPLOAD_RETRY_MAX_MINUTES=120)
class CatchUpWindowTests(TestCase):
    """What period a facility is asked for, given how far behind it is."""

    def _facility(self, name='Kilifi', **kwargs):
        return Facility.objects.create(
            name=name, host='h', username='u', password_encrypted='x', **kwargs,
        )

    def test_a_facility_never_uploaded_gets_everything_pending(self):
        self.assertIsNone(schedule.window_for(self._facility()))

    def test_a_facility_uploaded_yesterday_gets_the_same_window_as_before(self):
        # The nightly job's yesterday-to-today window, reproduced exactly: a
        # healthy facility must see no change at all from the catch-up machinery.
        today = date(2026, 9, 20)
        facility = self._facility(appointments_synced_through=today - timedelta(days=1))

        self.assertEqual(
            schedule.window_for(facility, today=today),
            (date(2026, 9, 19), date(2026, 9, 20)),
        )

    def test_three_nights_switched_off_asks_for_three_nights(self):
        today = date(2026, 9, 20)
        facility = self._facility(appointments_synced_through=today - timedelta(days=3))

        self.assertEqual(
            schedule.window_for(facility, today=today),
            (date(2026, 9, 17), date(2026, 9, 20)),
        )

    def test_a_facility_far_enough_behind_falls_back_to_the_backfill(self):
        today = date(2026, 9, 20)
        facility = self._facility(appointments_synced_through=today - timedelta(days=31))

        self.assertIsNone(schedule.window_for(facility, today=today))

    def test_the_cut_off_day_itself_is_still_a_window(self):
        today = date(2026, 9, 20)
        facility = self._facility(appointments_synced_through=today - timedelta(days=30))

        self.assertEqual(schedule.window_for(facility, today=today)[0],
                         date(2026, 8, 21))

    def test_a_watermark_ahead_of_today_never_makes_a_backwards_window(self):
        # A clock that has gone backwards must not produce date_from > date_to,
        # which would match nothing at all and look like a quiet success.
        today = date(2026, 9, 20)
        facility = self._facility(appointments_synced_through=date(2026, 9, 25))

        window = schedule.window_for(facility, today=today)

        self.assertLessEqual(window[0], window[1])

    def test_the_env_facility_keeps_its_watermark_on_the_settings_row(self):
        # Single-facility mode has no Facility row at all; None is the env one.
        AppSettings.objects.update_or_create(
            pk=1, defaults={'appointments_synced_through': date(2026, 9, 18)},
        )

        self.assertEqual(
            schedule.window_for(None, today=date(2026, 9, 20)),
            (date(2026, 9, 18), date(2026, 9, 20)),
        )


@override_settings(UPLOAD_RETRY_BASE_MINUTES=30, UPLOAD_RETRY_MAX_MINUTES=120)
class DueCheckTests(TestCase):
    """Which facilities a tick picks up, and how hard it retries the broken ones."""

    def setUp(self):
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)

    def _facility(self, name='Kilifi', **kwargs):
        return Facility.objects.create(
            name=name, host='h', username='u', password_encrypted='x', **kwargs,
        )

    def test_a_facility_already_uploaded_today_is_not_due_again(self):
        facility = self._facility(appointments_synced_through=self.today)

        self.assertFalse(schedule.is_due(facility, now=self.now))

    def test_a_facility_uploaded_yesterday_is_due(self):
        facility = self._facility(
            appointments_synced_through=self.today - timedelta(days=1),
        )

        self.assertTrue(schedule.is_due(facility, now=self.now))

    def test_a_facility_nobody_has_uploaded_is_due(self):
        self.assertTrue(schedule.is_due(self._facility(), now=self.now))

    def test_it_makes_no_difference_what_time_of_day_the_machine_came_on(self):
        # The whole point: a container switched on at 14:20 uploads at 14:20.
        facility = self._facility(
            appointments_synced_through=self.today - timedelta(days=1),
        )
        afternoon = self.now.replace(hour=14, minute=20)

        self.assertTrue(schedule.is_due(facility, now=afternoon))

    def test_a_facility_that_just_failed_is_left_alone_for_one_interval(self):
        facility = self._facility(
            appointments_synced_through=self.today - timedelta(days=1),
            last_attempt_at=self.now - timedelta(minutes=10),
            consecutive_failures=1,
        )

        self.assertFalse(schedule.is_due(facility, now=self.now))

    def test_it_is_tried_again_once_the_interval_has_passed(self):
        facility = self._facility(
            appointments_synced_through=self.today - timedelta(days=1),
            last_attempt_at=self.now - timedelta(minutes=31),
            consecutive_failures=1,
        )

        self.assertTrue(schedule.is_due(facility, now=self.now))

    def test_each_failure_doubles_the_wait_up_to_the_ceiling(self):
        self.assertEqual(schedule.retry_delay(1), 30)
        self.assertEqual(schedule.retry_delay(2), 60)
        self.assertEqual(schedule.retry_delay(3), 120)
        # Capped, so a machine coming online at lunchtime still uploads today.
        self.assertEqual(schedule.retry_delay(9), 120)

    def test_a_facility_whose_last_attempt_worked_is_not_held_back(self):
        # Zero failures means no wait: a successful upload this morning followed
        # by a new day must not be delayed by the morning's timestamp.
        facility = self._facility(
            appointments_synced_through=self.today - timedelta(days=1),
            last_attempt_at=self.now - timedelta(minutes=1),
            consecutive_failures=0,
        )

        self.assertTrue(schedule.is_due(facility, now=self.now))

    def test_due_targets_skips_inactive_facilities(self):
        self._facility('Kilifi')
        self._facility('Malindi', is_active=False)

        targets = schedule.due_targets('multi', now=self.now)

        self.assertEqual([f.name for f in targets], ['Kilifi'])

    def test_due_targets_in_single_mode_is_the_env_facility(self):
        self.assertEqual(schedule.due_targets('single', now=self.now), [None])

        AppSettings.objects.update_or_create(
            pk=1, defaults={'appointments_synced_through': self.today},
        )
        self.assertEqual(schedule.due_targets('single', now=self.now), [])

    def test_due_targets_does_not_mix_tenants_into_a_multi_facility_tick(self):
        server = TenantServer.objects.create(
            name='Cloud', host='h', username='u', password_encrypted='x',
        )
        self._facility('Standalone')
        self._facility('Tenant', server=server, database_name='openmrs_kilifi')

        self.assertEqual([f.name for f in schedule.due_targets('multi', now=self.now)],
                         ['Standalone'])
        self.assertEqual([f.name for f in schedule.due_targets('tenant', now=self.now)],
                         ['Tenant'])


class RecordAttemptTests(TestCase):
    """How an attempt's outcome moves the catch-up state."""

    def _facility(self):
        return Facility.objects.create(
            name='Kilifi', host='h', username='u', password_encrypted='x',
        )

    def test_success_advances_the_watermark_and_clears_the_failures(self):
        facility = self._facility()
        Facility.objects.filter(pk=facility.pk).update(consecutive_failures=3)

        schedule.record_attempt(facility.pk, ok=True, synced_through=date(2026, 9, 20))

        facility.refresh_from_db()
        self.assertEqual(facility.appointments_synced_through, date(2026, 9, 20))
        self.assertEqual(facility.consecutive_failures, 0)
        self.assertIsNotNone(facility.last_attempt_at)

    def test_failure_leaves_the_watermark_owing_and_counts_up(self):
        facility = self._facility()
        Facility.objects.filter(pk=facility.pk).update(
            appointments_synced_through=date(2026, 9, 17),
        )

        schedule.record_attempt(facility.pk, ok=False)
        schedule.record_attempt(facility.pk, ok=False)

        facility.refresh_from_db()
        # Still owed: the next successful run collects these days too.
        self.assertEqual(facility.appointments_synced_through, date(2026, 9, 17))
        self.assertEqual(facility.consecutive_failures, 2)

    def test_the_watermark_never_moves_backwards(self):
        # Re-uploading some week in January is a legitimate thing to ask for and
        # must not convince the nightly job that January is where we have got to.
        facility = self._facility()
        Facility.objects.filter(pk=facility.pk).update(
            appointments_synced_through=date(2026, 9, 20),
        )

        schedule.record_attempt(facility.pk, ok=True, synced_through=date(2026, 1, 5))

        facility.refresh_from_db()
        self.assertEqual(facility.appointments_synced_through, date(2026, 9, 20))

    def test_the_env_facility_records_against_the_settings_row(self):
        schedule.record_attempt(None, ok=True, synced_through=date(2026, 9, 20))

        app_settings = AppSettings.load()
        self.assertEqual(app_settings.appointments_synced_through, date(2026, 9, 20))
        self.assertEqual(app_settings.consecutive_failures, 0)

        schedule.record_attempt(None, ok=False)

        app_settings.refresh_from_db()
        self.assertEqual(app_settings.consecutive_failures, 1)
        self.assertEqual(app_settings.appointments_synced_through, date(2026, 9, 20))


class PlatformReachableTests(SimpleTestCase):
    """The cheap link check that decides whether a tick is worth attempting."""

    @override_settings(CHQI_API_BASE_URL='https://api.test/v1')
    def test_a_successful_connect_uses_the_scheme_default_port(self):
        with mock.patch.object(services.socket, 'create_connection') as connect:
            ok, why = services.platform_reachable()

        self.assertTrue(ok)
        self.assertEqual(why, '')
        self.assertEqual(connect.call_args[0][0], ('api.test', 443))
        connect.return_value.close.assert_called_once()

    @override_settings(CHQI_API_BASE_URL='http://10.0.0.5:8080')
    def test_an_explicit_port_wins_over_the_scheme(self):
        with mock.patch.object(services.socket, 'create_connection') as connect:
            services.platform_reachable()

        self.assertEqual(connect.call_args[0][0], ('10.0.0.5', 8080))

    @override_settings(CHQI_API_BASE_URL='https://api.test')
    def test_an_unreachable_platform_is_reported_not_raised(self):
        with mock.patch.object(services.socket, 'create_connection',
                               side_effect=OSError('Network is unreachable')):
            ok, why = services.platform_reachable()

        self.assertFalse(ok)
        self.assertIn('Network is unreachable', why)

    @override_settings(CHQI_API_BASE_URL='')
    def test_an_unconfigured_platform_is_not_reachable(self):
        ok, why = services.platform_reachable()

        self.assertFalse(ok)
        self.assertIn('not configured', why)

    @override_settings(CHQI_API_BASE_URL='api.test')
    def test_a_url_with_no_host_is_reported_rather_than_dialled(self):
        # urlparse puts a bare hostname in `path`, leaving `hostname` None.
        with mock.patch.object(services.socket, 'create_connection') as connect:
            ok, why = services.platform_reachable()

        self.assertFalse(ok)
        self.assertIn('not a usable URL', why)
        connect.assert_not_called()

    @override_settings(CHQI_API_BASE_URL='https://api.test',
                       UPLOAD_CONNECT_CHECK_SECONDS=5)
    def test_the_check_is_kept_short(self):
        # It runs every tick; a tick that finds no internet must cost nothing.
        with mock.patch.object(services.socket, 'create_connection') as connect:
            services.platform_reachable()

        self.assertEqual(connect.call_args[1]['timeout'], 5)


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key')
class PerFacilityWindowTests(TestCase):
    """One run, facilities at different distances behind."""

    def _facility(self, name, **kwargs):
        facility = Facility.objects.create(name=name, host='h', username='u', **kwargs)
        facility.set_password('pw')
        facility.save()
        return facility

    def test_each_log_gets_its_own_period_and_the_run_gets_the_envelope(self):
        behind_a_day = self._facility('Kilifi')
        behind_a_week = self._facility('Malindi')
        windows = {
            'Kilifi': (date(2026, 9, 19), date(2026, 9, 20)),
            'Malindi': (date(2026, 9, 13), date(2026, 9, 20)),
        }

        run = services.create_run(
            date(2026, 9, 20), date(2026, 9, 20), triggered_by='cron', mode='multi',
            facilities=[behind_a_day, behind_a_week],
            windows=lambda facility: windows[facility.name],
        )

        logs = {log.facility_label: log for log in run.logs.all()}
        self.assertEqual(logs['Kilifi'].date_from, date(2026, 9, 19))
        self.assertEqual(logs['Malindi'].date_from, date(2026, 9, 13))
        # The run reads as the widest of them, so the history page does not claim
        # a facility was covered for a period it never was.
        self.assertEqual((run.date_from, run.date_to),
                         (date(2026, 9, 13), date(2026, 9, 20)))
        self.assertFalse(run.is_backfill)

    def test_a_facility_with_no_window_is_a_backfill_on_its_own(self):
        never_uploaded = self._facility('Kilifi')
        behind_a_day = self._facility('Malindi')
        windows = {'Kilifi': None, 'Malindi': (date(2026, 9, 19), date(2026, 9, 20))}

        run = services.create_run(
            date(2026, 9, 20), date(2026, 9, 20), triggered_by='cron', mode='multi',
            facilities=[never_uploaded, behind_a_day],
            windows=lambda facility: windows[facility.name],
        )

        logs = {log.facility_label: log for log in run.logs.all()}
        self.assertTrue(logs['Kilifi'].is_backfill)
        self.assertFalse(logs['Malindi'].is_backfill)
        # One container out of a hundred needing a full load is not the
        # deployment's initial load, so the run-level flag stays off.
        self.assertFalse(run.is_backfill)

    def test_a_run_where_every_facility_needs_everything_is_a_backfill(self):
        facilities = [self._facility('Kilifi'), self._facility('Malindi')]

        run = services.create_run(
            date(2026, 9, 20), date(2026, 9, 20), triggered_by='cron', mode='multi',
            facilities=facilities, windows=lambda facility: None,
        )

        self.assertTrue(run.is_backfill)
        self.assertEqual(run.period_label, 'All pending appointments')
        self.assertTrue(all(log.is_backfill for log in run.logs.all()))

    def test_without_windows_every_log_keeps_the_runs_own_dates(self):
        # The nightly job and every manual upload go through this path unchanged.
        facilities = [self._facility('Kilifi'), self._facility('Malindi')]

        run = services.create_run(
            date(2026, 1, 1), date(2026, 1, 2), triggered_by='manual',
            mode='multi', facilities=facilities,
        )

        self.assertEqual((run.date_from, run.date_to), (date(2026, 1, 1), date(2026, 1, 2)))
        for log in run.logs.all():
            self.assertEqual((log.date_from, log.date_to),
                             (date(2026, 1, 1), date(2026, 1, 2)))
            self.assertFalse(log.is_backfill)

    def test_the_worker_backfills_the_facility_its_log_says_to(self):
        never_uploaded = self._facility('Kilifi')
        behind_a_day = self._facility('Malindi')
        windows = {'Kilifi': None, 'Malindi': (date(2026, 9, 19), date(2026, 9, 20))}
        run = services.create_run(
            date(2026, 9, 20), date(2026, 9, 20), triggered_by='cron', mode='multi',
            facilities=[never_uploaded, behind_a_day],
            windows=lambda facility: windows[facility.name],
        )

        @contextmanager
        def fake_connect(config):
            yield mock.Mock()

        calls = {}

        def fetch(conn, *dates):
            calls[len(calls)] = dates
            return []

        with mock.patch.object(openmrs, 'connect', fake_connect), \
                mock.patch.object(openmrs, 'fetch_appointments', side_effect=fetch), \
                mock.patch.object(openmrs, 'fetch_patient_updates', return_value=[]):
            services.execute_run(run, workers=1)

        # Ordered by facility_label: Kilifi (no dates at all), then Malindi.
        self.assertEqual(calls[0], ())
        self.assertEqual(calls[1], (date(2026, 9, 19), date(2026, 9, 20)))


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key', OPENMRS_DB_LABEL='Env facility',
                   UPLOAD_RETRY_BASE_MINUTES=30, UPLOAD_RETRY_MAX_MINUTES=120)
class DueOnlyCommandTests(TestCase):
    """The frequent tick: what it uploads, and when it does nothing at all."""

    def setUp(self):
        patcher = mock.patch('upload.services.execute_run', side_effect=self._succeed)
        self.execute = patcher.start()
        self.addCleanup(patcher.stop)
        reachable = mock.patch('upload.services.platform_reachable',
                               return_value=(True, ''))
        self.reachable = reachable.start()
        self.addCleanup(reachable.stop)

    @staticmethod
    def _succeed(run, workers=None):
        UploadRun.objects.filter(pk=run.pk).update(status='success')
        return run

    def _tick(self, **kwargs):
        out = StringIO()
        call_command('upload_appointments', due_only=True, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def _facility(self, name, **kwargs):
        return Facility.objects.create(
            name=name, host='h', username='u', password_encrypted='x', **kwargs,
        )

    def test_a_facility_already_uploaded_today_produces_no_run_at_all(self):
        AppSettings.objects.update_or_create(
            pk=1, defaults={'appointments_synced_through': timezone.localdate()},
        )

        output = self._tick()

        self.assertIn('up to date', output)
        self.assertFalse(UploadRun.objects.exists())
        self.execute.assert_not_called()

    def test_no_link_to_the_platform_leaves_no_trace(self):
        # Forty-eight ticks through an afternoon with no internet must not write
        # forty-eight failed runs; the facility is not behind, the link is down.
        self.reachable.return_value = (False, 'Cannot reach api.test:443 — down.')

        output = self._tick()

        self.assertIn('No link to the platform', output)
        self.assertFalse(UploadRun.objects.exists())

    def test_a_tick_landing_inside_a_long_run_is_not_an_error(self):
        # Cron mails anything that exits non-zero, and at this cadence overlapping
        # a big run is routine rather than exceptional.
        running = UploadRun.objects.create(
            date_from=date(2026, 9, 20), date_to=date(2026, 9, 20),
            mode='multi', triggered_by='cron', status='in_progress',
            heartbeat_at=timezone.now(),
        )

        output = self._tick()

        self.assertIn('still in progress', output)
        self.assertEqual(UploadRun.objects.count(), 1)
        self.assertEqual(UploadRun.objects.get().pk, running.pk)

    def test_a_machine_switched_on_after_three_nights_off_uploads_those_nights(self):
        today = timezone.localdate()
        AppSettings.objects.update_or_create(
            pk=1,
            defaults={'appointments_synced_through': today - timedelta(days=3)},
        )

        self._tick()

        run = UploadRun.objects.latest('pk')
        self.assertEqual(run.mode, 'single')
        self.assertEqual(run.date_from, today - timedelta(days=3))
        self.assertEqual(run.date_to, today)
        self.assertFalse(run.is_backfill)

    def test_the_first_tick_on_a_fresh_install_is_a_backfill(self):
        output = self._tick()

        run = UploadRun.objects.latest('pk')
        self.assertTrue(run.is_backfill)
        self.assertIn('all pending', output)

    def test_only_the_facilities_that_are_behind_are_uploaded(self):
        today = timezone.localdate()
        AppSettings.objects.update_or_create(
            pk=1, defaults={'multi_facility_enabled': True},
        )
        self._facility('Kilifi', appointments_synced_through=today)
        self._facility('Malindi', appointments_synced_through=today - timedelta(days=2))

        self._tick()

        run = UploadRun.objects.latest('pk')
        self.assertEqual([log.facility_label for log in run.logs.all()], ['Malindi'])
        self.assertEqual(run.facilities_total, 1)

    def test_a_facility_still_inside_its_backoff_is_left_out(self):
        today = timezone.localdate()
        AppSettings.objects.update_or_create(
            pk=1, defaults={'multi_facility_enabled': True},
        )
        self._facility(
            'Kilifi',
            appointments_synced_through=today - timedelta(days=1),
            last_attempt_at=timezone.now() - timedelta(minutes=5),
            consecutive_failures=2,
        )

        output = self._tick()

        self.assertIn('up to date', output)
        self.assertFalse(UploadRun.objects.exists())

    def test_it_refuses_to_be_combined_with_a_date_range(self):
        with self.assertRaises(CommandError) as ctx:
            call_command('upload_appointments', due_only=True, date_from='2026-01-01')

        self.assertIn('neither --backfill nor a date range', str(ctx.exception))
        self.assertFalse(UploadRun.objects.exists())

    def test_it_refuses_to_be_combined_with_backfill(self):
        with self.assertRaises(CommandError):
            call_command('upload_appointments', due_only=True, backfill=True)

    def test_it_refuses_to_be_combined_with_a_single_facility(self):
        facility = self._facility('Kilifi')

        with self.assertRaises(CommandError) as ctx:
            call_command('upload_appointments', due_only=True, facility=facility.pk)

        self.assertIn('--facility on its own', str(ctx.exception))

    def test_the_nightly_window_still_works_untouched(self):
        # --due-only is an addition, not a replacement: a deployment that keeps
        # its 6am cron entry must behave exactly as it did before.
        AppSettings.objects.update_or_create(
            pk=1, defaults={'initial_backfill_done': True},
        )

        call_command('upload_appointments')

        run = UploadRun.objects.latest('pk')
        self.assertEqual(run.date_from, date.today() - timedelta(days=1))
        self.assertEqual(run.date_to, date.today())


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key')
class WatermarkStampingTests(TestCase):
    """What an upload does to the state the next tick reads."""

    def _facility(self):
        facility = Facility.objects.create(name='Kilifi', host='h', username='u')
        facility.set_password('pw')
        facility.save()
        return facility

    @contextmanager
    def _openmrs(self, fails=False):
        @contextmanager
        def fake_connect(config):
            yield mock.Mock()

        fetch = mock.Mock(side_effect=RuntimeError('boom')) if fails \
            else mock.Mock(return_value=[])
        with mock.patch.object(openmrs, 'connect', fake_connect), \
                mock.patch.object(openmrs, 'fetch_appointments', fetch), \
                mock.patch.object(openmrs, 'fetch_patient_updates', return_value=[]):
            yield

    def _log(self, facility, date_from, date_to, is_backfill=False):
        run = services.create_run(
            date_from, date_to, triggered_by='cron', mode='multi',
            facilities=[facility], is_backfill=is_backfill,
        )
        return run.logs.get()

    def test_a_successful_upload_moves_the_watermark_to_its_end_date(self):
        facility = self._facility()
        log = self._log(facility, date(2026, 9, 19), date(2026, 9, 20))

        with self._openmrs():
            services.upload_facility(log, facility.as_config(), mock.Mock())

        facility.refresh_from_db()
        self.assertEqual(facility.appointments_synced_through, date(2026, 9, 20))
        self.assertEqual(facility.consecutive_failures, 0)

    def test_a_failed_upload_leaves_the_period_owed_for_next_time(self):
        facility = self._facility()
        Facility.objects.filter(pk=facility.pk).update(
            appointments_synced_through=date(2026, 9, 17),
        )
        log = self._log(facility, date(2026, 9, 17), date(2026, 9, 20))

        with self._openmrs(fails=True):
            services.upload_facility(log, facility.as_config(), mock.Mock())

        facility.refresh_from_db()
        self.assertEqual(log.status, 'failed')
        self.assertEqual(facility.appointments_synced_through, date(2026, 9, 17))
        self.assertEqual(facility.consecutive_failures, 1)

    def test_a_backfill_leaves_the_watermark_at_today(self):
        # It covered everything outstanding, so only today's bookings are new.
        facility = self._facility()
        log = self._log(facility, date(2026, 9, 20), date(2026, 9, 20), is_backfill=True)

        with self._openmrs():
            services.upload_facility(log, facility.as_config(), mock.Mock(), backfill=True)

        facility.refresh_from_db()
        self.assertEqual(facility.appointments_synced_through, timezone.localdate())

    def test_a_manual_upload_of_an_old_range_does_not_rewind_the_watermark(self):
        facility = self._facility()
        Facility.objects.filter(pk=facility.pk).update(
            appointments_synced_through=date(2026, 9, 20),
        )
        log = self._log(facility, date(2026, 1, 1), date(2026, 1, 5))

        with self._openmrs():
            services.upload_facility(log, facility.as_config(), mock.Mock())

        facility.refresh_from_db()
        self.assertEqual(facility.appointments_synced_through, date(2026, 9, 20))

    def test_a_facility_whose_password_cannot_be_read_still_counts_as_an_attempt(self):
        # Otherwise it never leaves the due list and is retried every tick.
        facility = Facility.objects.create(
            name='Kilifi', host='h', username='u', password_encrypted='not-decryptable',
        )
        run = services.create_run(
            date(2026, 9, 19), date(2026, 9, 20), triggered_by='cron',
            mode='multi', facilities=[facility],
        )

        services.execute_run(run, workers=1)

        facility.refresh_from_db()
        self.assertEqual(facility.consecutive_failures, 1)
        self.assertIsNotNone(facility.last_attempt_at)
        self.assertIsNone(facility.appointments_synced_through)


class RetryWindowTests(TestCase):
    """Retrying a catch-up run gives each facility back its own period."""

    def setUp(self):
        self.staff = User.objects.create_user('staff', password='pw', is_staff=True)
        self.client = Client()

    def _facility(self, name):
        return Facility.objects.create(
            name=name, host='h', username='u', password_encrypted='x',
        )

    @mock.patch('upload.services.spawn_run')
    def test_a_retry_reuses_each_log_period_not_the_runs_envelope(self, mock_spawn):
        never_uploaded = self._facility('Kilifi')
        behind_a_day = self._facility('Malindi')
        windows = {'Kilifi': None, 'Malindi': (date(2026, 9, 19), date(2026, 9, 20))}
        run = services.create_run(
            date(2026, 9, 20), date(2026, 9, 20), triggered_by='cron', mode='multi',
            facilities=[never_uploaded, behind_a_day],
            windows=lambda facility: windows[facility.name],
        )
        run.logs.update(status='failed')
        UploadRun.objects.filter(pk=run.pk).update(status='failed', facilities_failed=2)

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse('upload:run_retry_failed', kwargs={'run_id': run.pk}),
        )

        retry = UploadRun.objects.get(pk=response.json()['run_id'])
        logs = {log.facility_label: log for log in retry.logs.all()}
        # The one that needed everything pending must not be handed a date range
        # just because its neighbour only needed a day.
        self.assertTrue(logs['Kilifi'].is_backfill)
        self.assertFalse(logs['Malindi'].is_backfill)
        self.assertEqual(logs['Malindi'].date_from, date(2026, 9, 19))
        mock_spawn.assert_called_once()


@override_settings(FIELD_ENCRYPTION_KEY='unit-test-key')
class DueOnlyTenantTests(TestCase):
    """The tick in multi-tenant mode, where the facility list is discovered."""

    def setUp(self):
        patcher = mock.patch('upload.services.execute_run', side_effect=self._succeed)
        self.execute = patcher.start()
        self.addCleanup(patcher.stop)
        reachable = mock.patch('upload.services.platform_reachable',
                               return_value=(True, ''))
        reachable.start()
        self.addCleanup(reachable.stop)
        AppSettings.objects.update_or_create(
            pk=1, defaults={'multi_tenant_enabled': True},
        )

    @staticmethod
    def _succeed(run, workers=None):
        UploadRun.objects.filter(pk=run.pk).update(status='success')
        return run

    def _tick(self, **kwargs):
        out = StringIO()
        call_command('upload_appointments', due_only=True, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def _server(self):
        server = TenantServer.objects.create(
            name='Cloud', host='h', username='u', password_encrypted='x',
        )
        return server

    def test_the_tick_refreshes_the_database_list_before_deciding(self):
        server = self._server()
        Facility.objects.create(
            name='Kilifi', server=server, database_name='openmrs_kilifi',
            host='h', username='u', password_encrypted='x',
        )

        with mock.patch('upload.management.commands.upload_appointments.tenants.sync_all',
                        return_value=[(server, {'ok': True, 'message': 'synced'})]) as sync:
            self._tick()

        sync.assert_called_once()
        self.assertEqual(sync.call_args[1]['reprobe'], False)
        self.assertEqual(UploadRun.objects.latest('pk').mode, 'tenant')

    def test_a_deployment_with_no_servers_declines_quietly_rather_than_failing(self):
        # Raising here would mail the operator every half hour about a
        # misconfiguration one message an hour would not fix any faster.
        output = self._tick()

        self.assertIn('no active tenant servers', output)
        self.assertFalse(UploadRun.objects.exists())

    def test_no_sync_skips_the_servers_and_uploads_what_is_known(self):
        server = self._server()
        Facility.objects.create(
            name='Kilifi', server=server, database_name='openmrs_kilifi',
            host='h', username='u', password_encrypted='x',
        )

        with mock.patch('upload.management.commands.upload_appointments.tenants.sync_all') as sync:
            self._tick(no_sync=True)

        sync.assert_not_called()
        self.assertEqual(
            [log.facility_label for log in UploadRun.objects.latest('pk').logs.all()],
            ['Kilifi'],
        )

class CatchUpSeedMigrationTests(TransactionTestCase):
    """0008 reads the watermarks out of the history a deployment already has.

    Without this, the first tick on a deployment that has been uploading for
    months would find every watermark empty, read that as "never uploaded", and
    re-send every pending appointment for every facility.
    """

    migrate_from = ('upload', '0007_uploadlog_patient_updates_uploaded_and_more')
    migrate_to = ('upload', '0008_appsettings_appointments_synced_through_and_more')

    available_apps = ['upload', 'django.contrib.auth', 'django.contrib.contenttypes']

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([target])
        return executor

    def setUp(self):
        self._migrate(self.migrate_from)
        self.old_apps = MigrationExecutor(connection).loader.project_state(
            [self.migrate_from],
        ).apps

    def tearDown(self):
        # Leave the database on the latest migration for whatever runs next.
        self._migrate(self.migrate_to)

    def _log(self, apps, **kwargs):
        UploadLog = apps.get_model('upload', 'UploadLog')
        defaults = dict(
            date_from=date(2026, 9, 1), date_to=date(2026, 9, 1),
            triggered_by='cron', status='success',
        )
        defaults.update(kwargs)
        return UploadLog.objects.create(**defaults)

    def test_a_facility_watermark_comes_from_its_latest_successful_upload(self):
        old = self.old_apps
        facility = old.get_model('upload', 'Facility').objects.create(
            name='Kilifi', host='h', username='u', password_encrypted='x',
        )
        self._log(old, facility=facility, date_to=date(2026, 9, 18))
        self._log(old, facility=facility, date_to=date(2026, 9, 20))
        # A later failure says nothing about how far the facility actually got.
        self._log(old, facility=facility, date_to=date(2026, 9, 21), status='failed')

        self._migrate(self.migrate_to)

        self.assertEqual(
            Facility.objects.get(pk=facility.pk).appointments_synced_through,
            date(2026, 9, 20),
        )

    def test_the_env_facility_watermark_comes_from_its_own_logs(self):
        old = self.old_apps
        old.get_model('upload', 'AppSettings').objects.create(pk=1)
        # facility NULL is the environment-configured one, including for logs old
        # enough to predate runs entirely.
        self._log(old, facility=None, date_to=date(2026, 9, 19))

        self._migrate(self.migrate_to)

        self.assertEqual(AppSettings.objects.get(pk=1).appointments_synced_through,
                         date(2026, 9, 19))

    def test_a_facility_that_never_succeeded_is_left_needing_a_backfill(self):
        old = self.old_apps
        facility = old.get_model('upload', 'Facility').objects.create(
            name='Kilifi', host='h', username='u', password_encrypted='x',
        )
        self._log(old, facility=facility, status='failed')

        self._migrate(self.migrate_to)

        self.assertIsNone(
            Facility.objects.get(pk=facility.pk).appointments_synced_through,
        )

    def test_logs_of_a_backfill_run_are_marked_as_backfills_themselves(self):
        old = self.old_apps
        run = old.get_model('upload', 'UploadRun').objects.create(
            date_from=date(2026, 9, 1), date_to=date(2026, 9, 1),
            mode='single', triggered_by='cron', status='success', is_backfill=True,
        )
        log = self._log(old, run=run)

        self._migrate(self.migrate_to)

        self.assertTrue(UploadLog.objects.get(pk=log.pk).is_backfill)
