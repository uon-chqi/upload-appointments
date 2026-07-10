from contextlib import contextmanager
from datetime import date, timedelta
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from upload import crypto, openmrs, services
from upload.models import AppSettings, Facility, UploadLog, UploadRun


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
            mock_upload.side_effect = lambda log, config, tokens: (
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


class AppSettingsTests(TestCase):
    def test_load_is_idempotent_and_always_one_row(self):
        first = AppSettings.load()
        first.multi_facility_enabled = True
        first.save()

        AppSettings.load()
        AppSettings().save()

        self.assertEqual(AppSettings.objects.count(), 1)
        self.assertFalse(AppSettings.load().multi_facility_enabled)
