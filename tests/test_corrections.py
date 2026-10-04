import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient
import app
import corrections
import transcription


class CorrectionTests(unittest.TestCase):
    def test_sentence_override_survives_checkpoint_and_cached_vtt(self):
        with tempfile.TemporaryDirectory() as directory:
            original = {'start': 10, 'end': 15, 'text': 'Old.'}
            replacement = {'start': 10.2, 'end': 14.8, 'text': 'Corrected.', 'source_start': 10, 'source_end': 15}
            path = corrections.path_for(directory, 'https://example.com/audio')
            corrections.save(path, replacement)
            events = [{'status': 'resumed', 'until': 30, 'cues': [original]}, {'status': 'cue', 'cue': original},
                      {'status': 'cached', 'vtt': transcription.to_vtt([original])}]
            with patch.object(transcription, '_transcript_events', return_value=iter(events)):
                result = list(transcription.transcript_events('https://example.com/audio', '', False, directory))
            self.assertEqual(result[0]['cues'], [replacement])
            self.assertEqual(result[1]['cue'], replacement)
            self.assertIn('Corrected.', result[2]['vtt'])
            self.assertIn('00:00:10.200', result[2]['vtt'])

    def test_repeated_retry_after_cached_playback_updates_same_original_sentence(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'CACHE_DIR', directory):
            url = 'https://example.com/audio'; path = corrections.path_for(directory, url)
            corrections.save(path, {'start': 10.2, 'end': 14.8, 'text': 'First.', 'source_start': 10, 'source_end': 15})
            updated = {'start': 10.1, 'end': 14.9, 'text': 'Second.', 'source_start': 10, 'source_end': 15}
            with patch.object(app.aliyun, 'regenerate', return_value=updated) as regenerate:
                result = TestClient(app.app).post('/api/retranscribe_sentence', json={'audio_url': url, 'start': 10.2, 'end': 14.8})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(regenerate.call_args.args[2:], (10, 15))
            self.assertEqual(corrections.load(path), [updated])

    def test_failure_keeps_previous_sentence_and_rejects_invalid_ranges(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, 'CACHE_DIR', directory):
            url='https://example.com/audio'; path=corrections.path_for(directory,url)
            original={'start':10,'end':15,'source_start':10,'source_end':15,'text':'Preserved.'}
            corrections.save(path,original)
            client=TestClient(app.app)
            with patch.object(app.aliyun,'regenerate',side_effect=app.aliyun.ASRError('Failed')) as regenerate:
                self.assertEqual(client.post('/api/retranscribe_sentence',json={'audio_url':url,'start':10,'end':15}).status_code,502)
                self.assertEqual(corrections.load(path),[original])
                self.assertEqual(client.post('/api/retranscribe_sentence',json={'audio_url':url,'start':10,'end':400}).status_code,400)
                self.assertEqual(regenerate.call_count,1)
