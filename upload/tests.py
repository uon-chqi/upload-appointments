from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from upload import services


def make_response(status_code=200, json_data=None, headers=None, text=''):
    """Build a stand-in for a requests.Response with the bits the code touches."""
    resp = mock.Mock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    resp.headers = headers or {}
    resp.text = text
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


@override_settings(CHQI_API_BASE_URL='https://api.test')
class PostBatchWithRetryTests(SimpleTestCase):
    def setUp(self):
        self.url = 'https://api.test/api/patients/upload-json'
        self.headers = {'Authorization': 'Bearer x'}
        self.payload = '{"patients": []}'

    def call(self, **kwargs):
        return services._post_batch_with_retry(
            self.url, self.payload, self.headers, 1, 1, **kwargs
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


@override_settings(CHQI_API_BASE_URL='https://api.test')
class UploadPatientsRetryTests(SimpleTestCase):
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
        results = services.upload_patients(patients, token='t', batch_size=2)
        self.assertEqual(results, [{'batch': 1}, {'batch': 2}])
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 1)


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
