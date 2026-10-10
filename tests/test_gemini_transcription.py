import base64
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import wave
from unittest.mock import Mock, patch

import gemini_transcription as gemini
import transcription
from transcription_jobs import StopSignal


def response(words, text=None):
    return {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [{
        'type': 'text', 'text': text if text is not None else ' '.join(w[0] for w in words),
        'annotations': [{'type': 'word_info', 'text': word, 'start_offset': f'{start}s', 'end_offset': f'{end}s'}
                        for word, start, end in words]}]}]}


class GeminiTranscriptionTests(unittest.TestCase):
    def test_payload_is_wav_and_verbatim_with_real_word_timing(self):
        body = gemini.payload(bytes(32000))
        self.assertEqual(body['model'], 'gemini-3.5-transcribe')
        self.assertEqual(body['generation_config']['transcription_config']['mode'],
                         {'type': 'verbatim', 'timestamp_granularities': ['word']})
        self.assertFalse(body['store'])
        with wave.open(io.BytesIO(base64.b64decode(body['input'][0]['data']))) as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()), (1, 2, 16000, 16000))

    def test_offsets_are_absolute_and_sentences_span_chunks(self):
        words = gemini.words_from_response(response([('Hello', .1, .5)]), 15, 2)
        ready, pending = gemini.sentences(words)
        self.assertEqual(ready, [])
        more = gemini.words_from_response(response([('world!', .05, .5)]), 16, 2)
        ready, pending = gemini.sentences(more, pending)
        self.assertIsNone(pending)
        self.assertEqual(ready, [{'start': 15.1, 'end': 16.5, 'text': 'Hello world!'}])

    def test_missing_or_invalid_timestamps_never_create_guessed_cues(self):
        for data in (response([], 'Hello.'), response([('Hello.', 3, 4)])):
            with self.assertRaises(gemini.ASRError):
                gemini.words_from_response(data, 0, 2)

    def test_punctuation_and_chinese_words_join_naturally(self):
        self.assertEqual(gemini.join_text('Hello', ','), 'Hello,')
        self.assertEqual(gemini.join_text('Hello,', 'world.'), 'Hello, world.')
        self.assertEqual(gemini.join_text('你', '好。'), '你好。')

    def test_quantized_short_words_keep_text_and_real_sentence_endpoints(self):
        data = response([('I', .1, .1), ('cover', .1, .4), ('the', .4, .4),
                         ('White', .4, .7), ('House', .7, 1), ('.', 1, 1)])
        words = gemini.words_from_response(data, 15, 2)
        ready, pending = gemini.sentences(words)
        self.assertIsNone(pending)
        self.assertEqual(ready, [{'start': 15.1, 'end': 16, 'text': 'I cover the White House.'}])
        self.assertTrue(all(word['end'] > word['start'] for word in words))

    def test_reversed_timestamps_are_still_rejected(self):
        with self.assertRaises(gemini.ASRError):
            gemini.words_from_response(response([('bad', .7, .4)]), 0, 2)

    def test_overlapping_speaker_annotations_are_ordered_by_real_time(self):
        words = gemini.words_from_response(response([('Times.', 72.2, 72.4),
                   ('Mhm.', 70.9, 71.2), ('Right,', 72.3, 72.4)]), 135, 120)
        self.assertEqual([word['text'] for word in words], ['Mhm.', 'Times.', 'Right,'])
        ready, pending = gemini.sentences([{'start': 0, 'end': 2, 'text': 'Hello'},
                                         {'start': 1, 'end': 1.5, 'text': 'there.'}])
        self.assertIsNone(pending)
        self.assertEqual(ready[0]['end'], 2)

    def test_failed_chunk_preserves_pending_sentence_and_resumes_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            audio = folder / 'a.mp3'
            audio.write_bytes(b'audio')
            checkpoint = folder / 'progress.json'
            def ranges(resume, duration):
                return [(0, 0, 15), (1, 15, 17)] if resume == 0 else [(1, 15, 17)]
            with patch.object(gemini, 'api_keys', return_value=['fake-key']), \
                 patch.object(gemini, 'audio_duration', return_value=17), \
                 patch.object(gemini, 'segment_ranges', side_effect=ranges), \
                 patch.object(gemini, 'decode_segment', side_effect=lambda path, start, end, stopped: bytes(round((end-start)*32000))), \
                 patch.object(gemini, 'request_transcription', side_effect=[response([('Hello', .1, 14.9)]), gemini.ASRError('Lost')]):
                with self.assertRaises(gemini.ASRError):
                    list(gemini.transcript_events('https://example.com/audio', audio, folder/'a.vtt', checkpoint, None, threading.Event()))
            state = transcription.read_checkpoint(checkpoint)
            self.assertEqual(state['until'], 15)
            self.assertEqual(state['cues'], [])
            self.assertEqual(state['pending_sentence']['text'], 'Hello')
            with patch.object(gemini, 'api_keys', return_value=['fake-key']), \
                 patch.object(gemini, 'audio_duration', return_value=17), \
                 patch.object(gemini, 'segment_ranges', side_effect=ranges), \
                 patch.object(gemini, 'decode_segment', return_value=bytes(32000*2)) as decode, \
                 patch.object(gemini, 'request_transcription', return_value=response([('world!', .05, .5)])):
                events = list(gemini.transcript_events('https://example.com/audio', audio, folder/'a.vtt', checkpoint, None, threading.Event()))
            self.assertEqual(decode.call_args.args[1], 15)
            self.assertEqual(events[0]['until'], 0)
            self.assertEqual([e['cue']['text'] for e in events if e['status'] == 'cue'], ['Hello world!'])
            self.assertEqual(events[-1]['status'], 'done')
            self.assertFalse(checkpoint.exists())

    def test_twenty_minute_ranges_and_resume_keep_original_boundaries(self):
        self.assertEqual(list(gemini.segment_ranges(0, 3900)), [(0,0,1200),(1,1200,2400),(2,2400,3600),(3,3600,3900)])
        self.assertEqual(list(gemini.segment_ranges(495, 2500)), [(0,495,1200),(1,1200,2400),(2,2400,2500)])

    def test_quota_key_is_skipped_until_reset_and_next_key_takes_over(self):
        limited=gemini.ASRError('Quota',False)
        limited.rate_limited=True;limited.retry_after=86400
        success=response([('Hello.',.1,.5)])
        with patch.dict(gemini._quota_deadlines,{},clear=True), \
             patch.object(gemini,'request_transcription',side_effect=[limited,success,success]) as cloud:
            for _ in range(2):
                result=gemini.transcribe_with_keys(bytes(32000),None,threading.Event(),['quota-key','available-key'],0)
                self.assertEqual(result,success)
            self.assertEqual([call.kwargs['key'] for call in cloud.call_args_list],['quota-key','available-key','available-key'])

    def test_large_segments_upload_a_file_instead_of_inline_base64(self):
        session=Mock()
        def http(body,headers=None):
            item=Mock(ok=True,headers=headers or {})
            item.json.return_value=body
            item.__enter__=Mock(return_value=item)
            item.__exit__=Mock(return_value=False)
            return item
        session.post.side_effect=[http({}, {'X-Goog-Upload-URL':'https://generativelanguage.googleapis.com/upload/test'}),
            http({'file':{'name':'files/test','uri':'https://generativelanguage.googleapis.com/v1beta/files/test'}}),
            http(response([('Hello.',.1,.5)]))]
        with patch.object(gemini.requests,'Session',return_value=session), patch.object(gemini,'wav_audio',return_value=b'wav-data'):
            result=gemini.request_transcription(bytes(19*1024*1024),None,threading.Event(),key='fake-key')
        self.assertEqual(result['status'],'completed')
        body=session.post.call_args_list[-1].kwargs['json']
        self.assertNotIn('data',body['input'][0])
        self.assertEqual(body['input'][0]['uri'],'https://generativelanguage.googleapis.com/v1beta/files/test')
        self.assertEqual(session.post.call_count,3)
        # Cleanup happens in the request worker after the result is delivered.
        for _ in range(20):
            if session.delete.called:break
            threading.Event().wait(.01)
        session.delete.assert_called_once()

    def test_five_keys_start_together_and_commit_in_order_even_when_first_fails(self):
        barrier = threading.Barrier(5)
        keys = ['fake-1','fake-2','fake-3','fake-4','fake-5']
        calls = []
        def cloud(pcm, proxies, stopped, key=None):
            calls.append(key)
            barrier.wait(timeout=2)
            if key == keys[0]:
                raise gemini.ASRError('Quota', False)
            return response([(key+'.', .1, 1.5)])
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory); audio=folder/'a.mp3'; audio.write_bytes(b'audio')
            checkpoint=folder/'progress.json'
            with patch.object(gemini, 'SEGMENT_SECONDS', 2), patch.object(gemini,'api_keys',return_value=keys), \
                 patch.object(gemini,'audio_duration',return_value=10), patch.object(gemini,'decode_segment',return_value=bytes(64000)), \
                 patch.object(gemini,'request_transcription',side_effect=cloud):
                with self.assertRaises(gemini.ASRError):
                    list(gemini.transcript_events('https://example.com/audio',audio,folder/'a.vtt',checkpoint,None,threading.Event()))
            self.assertEqual(set(calls),set(keys))
            self.assertEqual(len(list(folder.glob('*.segment-*.json'))),4)
            with patch.object(gemini,'SEGMENT_SECONDS',2), patch.object(gemini,'api_keys',return_value=keys), \
                 patch.object(gemini,'audio_duration',return_value=10), patch.object(gemini,'decode_segment',return_value=bytes(64000)), \
                 patch.object(gemini,'request_transcription',return_value=response([('fake-1.',.1,1.5)])) as retry:
                events=list(gemini.transcript_events('https://example.com/audio',audio,folder/'a.vtt',checkpoint,None,threading.Event()))
            self.assertEqual(retry.call_count,1)
            self.assertEqual([e['cue']['text'] for e in events if e['status']=='cue'],[key+'.' for key in keys])
            self.assertEqual([e['cue']['start'] for e in events if e['status']=='cue'],[.1,2.1,4.1,6.1,8.1])
            self.assertEqual(events[-1]['status'],'done')
            self.assertEqual(list(folder.glob('*.segment-*.json')),[])

    def test_cancelled_request_returns_without_waiting_for_http_or_second_call(self):
        started, release = threading.Event(), threading.Event()
        signal = StopSignal()
        session = Mock()
        def post(*args, **kwargs):
            started.set()
            release.wait(3)
            raise requests_error()
        def requests_error():
            return gemini.requests.ConnectionError('private-key')
        session.post.side_effect = post
        errors = []
        def run():
            try:
                gemini.request_transcription(bytes(32000), None, signal)
            except gemini.ASRError as error:
                errors.append(error)
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'private-key'}), patch.object(gemini.requests, 'Session', return_value=session):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(started.wait(1))
            signal.set()
            worker.join(1)
            release.set()
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors[0].retryable)
        self.assertNotIn('private-key', str(errors[0]))
        self.assertEqual(session.post.call_count, 1)

    def test_free_tier_rate_limit_is_clear_and_does_not_loop(self):
        session = Mock()
        http = Mock(ok=False, status_code=429)
        http.__enter__ = Mock(return_value=http)
        http.__exit__ = Mock(return_value=False)
        session.post.return_value = http
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'private-key'}), patch.object(gemini.requests, 'Session', return_value=session):
            with self.assertRaises(gemini.ASRError) as result:
                gemini.request_transcription(bytes(32000), None, threading.Event())
        self.assertFalse(result.exception.retryable)
        self.assertIn('额度', str(result.exception))
        self.assertNotIn('private-key', str(result.exception))

    def test_daily_quota_displays_limit_and_retry_after_without_echoing_raw_error(self):
        http = Mock()
        http.json.return_value = {'error': {'message': 'Rate limit exceeded (limit: 25 requests per day on Free Tier). private-key'}}
        http.headers = {'Retry-After': '86241'}
        error = gemini.rate_limit_error(http)
        self.assertFalse(error.retryable)
        self.assertIn('25 次/天', str(error))
        self.assertIn('23 小时 58 分钟', str(error))
        self.assertIn('断点已保留', str(error))
        self.assertNotIn('private-key', str(error))

    def test_cache_and_corrections_are_separate_from_aliyun(self):
        import corrections
        _, _, vtt = transcription.cache_paths(Path('.'), 'https://example.com/audio')
        self.assertIn(gemini.CACHE_SUFFIX, vtt.name)
        self.assertIn(gemini.CACHE_SUFFIX, corrections.path_for('.', 'https://example.com/audio').name)


if __name__ == '__main__':
    unittest.main()
