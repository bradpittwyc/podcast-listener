import threading
import unittest
from unittest.mock import Mock
from transcription_jobs import TranscriptionJobs
from transcription import stream_with_heartbeat


class JobTests(unittest.TestCase):
    def test_cancel_closes_resources_and_does_not_touch_next_episode(self):
        jobs = TranscriptionJobs()
        old, new = jobs.register('old'), jobs.register('new')
        close = Mock()
        old.on_cancel(close)
        jobs.cancel('old')
        self.assertTrue(old.is_set())
        self.assertFalse(new.is_set())
        close.assert_called_once()
        jobs.cancel('old')
        close.assert_called_once()

    def test_cancel_before_request_start_prevents_cloud_start(self):
        jobs = TranscriptionJobs()
        jobs.cancel('late')
        signal = jobs.register('late')
        close = Mock()
        signal.on_cancel(close)
        self.assertTrue(signal.is_set())
        close.assert_called_once()

    def test_completion_does_not_drop_queued_terminal_event(self):
        jobs = TranscriptionJobs()
        signal = jobs.register('done')
        started = threading.Event()
        def events(stopped):
            yield {'status':'cue', 'cue': {'text':'Last sentence.'}}
            yield {'status':'done'}
            started.set()
            yield {'status':'progress'}
        output = ''.join(stream_with_heartbeat(events, signal))
        self.assertIn('Last sentence.', output)
        self.assertIn('"status": "done"', output)
        self.assertNotIn('"status": "progress"', output)
        jobs.release('done', signal)
        self.assertNotIn('done', jobs.active)

    def test_previous_finish_cannot_release_replacement(self):
        jobs = TranscriptionJobs()
        old, new = jobs.register('same'), jobs.register('same')
        jobs.release('same', old)
        self.assertIs(jobs.active['same'], new)
        self.assertFalse(new.is_set())

    def test_cancel_endpoint_race_stops_before_provider_request(self):
        import app
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        client = TestClient(app.app)
        client.post('/api/cancel_transcription', json={'request_id':'before-start-test'})
        with patch('transcription.gemini_transcription.transcript_events') as provider:
            response = client.get('/api/transcribe_stream', params={'audio_url':'https://example.com/audio.mp3','request_id':'before-start-test'})
        provider.assert_not_called()
        self.assertNotIn('"status": "progress"', response.text)
        self.assertNotIn('before-start-test', app.transcription_jobs.active)
