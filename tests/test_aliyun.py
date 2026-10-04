import json
import os
import queue
import tempfile
import threading
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

import aliyun
from transcription import read_checkpoint


class FakeTask:
    calls = []
    fail_index = None

    def __init__(self, key, offset, index, region, stopped=None):
        self.offset, self.index, self.duration = offset, index, 0
        self.messages = queue.Queue()
        self.calls.append((key, offset, index))

    def send(self, frame):
        self.duration += len(frame) / 32000
        if self.duration == 1:
            self.messages.put({'status': 'cue', 'cue': {'start': self.offset, 'end': self.offset + 1, 'text': 'A sentence.'}})
            if self.index == self.fail_index:
                self.messages.put({'status': 'error', 'error': RuntimeError('Disconnected')})

    def finish(self):
        self.messages.put({'status': 'finished'})

    def close(self):
        pass


class AliyunTests(unittest.TestCase):
    def setUp(self):
        FakeTask.calls = []; FakeTask.fail_index = None

    def test_word_timestamps_split_sentences_and_keep_episode_offset(self):
        sentence = {'begin_time': 600, 'end_time': 5900, 'text': 'Hello world. How are you?', 'words': [
            {'begin_time': 600, 'end_time': 900, 'text': 'Hello', 'punctuation': ''},
            {'begin_time': 1000, 'end_time': 1700, 'text': ' world', 'punctuation': '.'},
            {'begin_time': 4500, 'end_time': 4900, 'text': 'How'},
            {'begin_time': 5000, 'end_time': 5200, 'text': 'are'},
            {'begin_time': 5400, 'end_time': 5900, 'text': 'you', 'punctuation': '?'}]}
        self.assertEqual(aliyun.sentence_cues(sentence, 30), [
            {'start': 30.6, 'end': 31.7, 'text': 'Hello world.'},
            {'start': 34.5, 'end': 35.9, 'text': 'How are you?'}])

    def test_subword_fragments_follow_canonical_sentence_spacing(self):
        tokens=['The','failures','and','cru','elt','ies','of','Trump']
        sentence={'begin_time':0,'end_time':3900,'text':'The failures and cruelties of Trump.',
                  'words':[{'begin_time':i*500,'end_time':i*500+400,'text':token,
                            'punctuation':'.' if i==7 else ''} for i,token in enumerate(tokens)]}
        result=aliyun.sentence_cues(sentence,30)
        self.assertEqual(result,[{'start':30,'end':33.9,'text':'The failures and cruelties of Trump.'}])

    def test_contractions_decimal_and_abbreviation_do_not_gain_spaces_or_split(self):
        tokens=['It','is','n',"'t",'3','.','5','in','the','U','.','S','.']
        sentence={'begin_time':0,'end_time':6400,'text':"It isn't 3.5 in the U.S.",
                  'words':[{'begin_time':i*500,'end_time':i*500+400,'text':token} for i,token in enumerate(tokens)]}
        result=aliyun.sentence_cues(sentence,0)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['text'],sentence['text'])

    def test_single_sentence_filter_aligns_before_removing_context(self):
        sentence={'begin_time':0,'end_time':4400,'text':'Previous. One sentence. Next.',
                  'words':[{'begin_time':i*1000,'end_time':i*1000+400,'text':token}
                           for i,token in enumerate(['Previous.','One','sentence.','Next.'])]}
        self.assertEqual(aliyun.sentence_cues(sentence,0,(1,2.5)),[{'start':1,'end':2.4,'text':'One sentence.'}])

    def test_long_sentence_is_not_cut_by_word_count_or_duration(self):
        words = [{'begin_time': i * 500, 'end_time': i * 500 + 400, 'text': 'word'} for i in range(60)]
        result = aliyun.sentence_cues({'words': words}, 0)
        self.assertEqual(len(result), 1)
        self.assertEqual(' '.join(c['text'] for c in result), ' '.join(['word'] * 60))
        self.assertEqual(result[-1]['end'], 29.9)

    def test_fragments_join_across_tasks_without_splitting_contractions(self):
        pending = aliyun.append_fragment({'start': 1, 'end': 29, 'text': "It isn"},
                                         {'start': 30, 'end': 33, 'text': "'t finished"})
        self.assertFalse(aliyun.ends_sentence(pending['text']))
        combined = aliyun.append_fragment(pending, {'start': 34, 'end': 36, 'text': 'until this point.'})
        self.assertEqual(combined, {'start': 1, 'end': 36, 'text': "It isn't finished until this point."})
        self.assertTrue(aliyun.ends_sentence(combined['text']))

    def test_missing_alignment_keeps_sentence_time_instead_of_inventing_words(self):
        result = aliyun.sentence_cues({'begin_time': 1000, 'end_time': 9500, 'text': 'Speech without word alignment.'}, 60)
        self.assertEqual(result, [{'start': 61, 'end': 69.5, 'text': 'Speech without word alignment.'}])

    def run_events(self, folder, seconds):
        frames = (bytes(32000) for _ in range(seconds))
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY_1': 'first', 'DASHSCOPE_API_KEY_2': 'second'}), \
             patch.object(aliyun, 'StreamTask', FakeTask), patch.object(aliyun, 'pcm_frames', return_value=frames):
            return list(aliyun.transcript_events('https://example.com/a.mp3', folder / 'a.mp3',
                        folder / 'a.vtt', folder / 'a.progress.json', None, threading.Event()))

    def test_alternates_and_preserves_order_until_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            events = self.run_events(folder, 61)
            self.assertEqual([call[0] for call in FakeTask.calls], ['first', 'second', 'first'])
            self.assertEqual([event['cue']['start'] for event in events if event['status'] == 'cue'], [0, 30, 60])
            self.assertEqual(events[-1]['status'], 'done')
            self.assertTrue((folder / 'a.vtt').exists())
            self.assertFalse((folder / 'a.progress.json').exists())

    def test_disconnect_saves_stable_sentence_then_resumes_same_key(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); FakeTask.fail_index = 0
            with self.assertRaisesRegex(RuntimeError, 'Disconnected'):
                self.run_events(folder, 31)
            checkpoint = read_checkpoint(folder / 'a.progress.json')
            self.assertEqual(checkpoint['until'], 1)
            self.assertEqual(checkpoint['next_chunk'], 0)
            FakeTask.fail_index = None; FakeTask.calls = []
            events = self.run_events(folder, 1)
            self.assertEqual(FakeTask.calls[0], ('first', 1, 0))
            self.assertEqual(events[0]['status'], 'resumed')
            self.assertEqual(events[0]['cues'], checkpoint['cues'])
            self.assertEqual(events[-1]['status'], 'done')

    def test_finish_timeout_is_not_kept_alive_by_heartbeats(self):
        task = aliyun.StreamTask.__new__(aliyun.StreamTask)
        task.closed = threading.Event(); task.messages = queue.Queue()
        task.finish_started = 0; task.ws = Mock()
        with patch.object(aliyun.time, 'monotonic', return_value=46):
            task.receive()
        message = task.messages.get_nowait()
        self.assertEqual(message['status'], 'error')
        self.assertTrue(message['error'].retryable)
        task.ws.recv.assert_not_called()

    def test_upload_deadline_is_not_kept_alive_by_heartbeats(self):
        task = aliyun.StreamTask.__new__(aliyun.StreamTask)
        task.closed = threading.Event(); task.messages = queue.Queue()
        task.finish_started = None; task.deadline = 180; task.ws = Mock()
        with patch.object(aliyun.time, 'monotonic', return_value=181):
            task.receive()
        self.assertEqual(task.messages.get_nowait()['status'],'error')
        task.ws.recv.assert_not_called()

    def test_authentication_errors_are_permanent_and_do_not_expose_provider_message(self):
        error = aliyun.task_error({'error_code': 'InvalidApiKey', 'error_message': 'secret-key'})
        self.assertFalse(error.retryable)
        self.assertNotIn('secret-key', str(error))
        self.assertTrue(aliyun.task_error({'error_code': 'Throttling'}).retryable)

    def test_duration_is_available_before_immediate_server_response(self):
        task = aliyun.StreamTask.__new__(aliyun.StreamTask)
        task.duration = 0; task.ws = Mock()
        task.closed = threading.Event()
        task.ws.send_binary.side_effect = lambda frame: self.assertEqual(task.duration, .1)
        task.send(bytes(3200))

    def test_second_key_starts_before_first_task_finishes(self):
        second_started = threading.Event()
        class ConcurrentTask(FakeTask):
            def __init__(inner, *args):
                super().__init__(*args)
                if inner.index == 1: second_started.set()
            def finish(inner):
                if inner.index == 0:
                    def delayed():
                        if second_started.wait(3): FakeTask.finish(inner)
                        else: inner.messages.put({'status': 'error', 'error': RuntimeError('Second key never started')})
                    threading.Thread(target=delayed, daemon=True).start()
                else: super().finish()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.dict(os.environ, {'DASHSCOPE_API_KEY_1': 'first', 'DASHSCOPE_API_KEY_2': 'second'}), \
                 patch.object(aliyun, 'StreamTask', ConcurrentTask), patch.object(aliyun, 'pcm_frames', return_value=(bytes(32000) for _ in range(31))):
                events = list(aliyun.transcript_events('https://example.com/a.mp3', folder/'a.mp3', folder/'a.vtt', folder/'a.progress.json', None, threading.Event()))
            self.assertTrue(second_started.is_set())
            self.assertEqual(events[-1]['status'], 'done')

    def test_partial_sentence_survives_disconnect_and_joins_on_resume(self):
        class FragmentTask(FakeTask):
            def send(inner, frame):
                inner.duration += len(frame) / 32000
                if inner.duration == 1:
                    inner.messages.put({'status': 'cue', 'cue': {'start': inner.offset, 'end': inner.offset+1,
                        'text': 'This sentence' if inner.offset == 0 else 'continues here.'}})
                    if inner.offset == 0: inner.messages.put({'status': 'error', 'error': RuntimeError('Disconnected')})
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); progress = folder/'a.progress.json'
            def attempt():
                with patch.dict(os.environ, {'DASHSCOPE_API_KEY_1': 'first'}), patch.object(aliyun, 'StreamTask', FragmentTask), \
                     patch.object(aliyun, 'pcm_frames', return_value=iter([bytes(32000)])):
                    return list(aliyun.transcript_events('x', folder/'a.mp3', folder/'a.vtt', progress, None, threading.Event()))
            with self.assertRaisesRegex(RuntimeError, 'Disconnected'): attempt()
            self.assertEqual(read_checkpoint(progress)['pending_sentence']['text'], 'This sentence')
            events = attempt()
            self.assertEqual(events[0]['until'], 0)
            cues = [e['cue'] for e in events if e['status'] == 'cue']
            self.assertEqual(cues, [{'start': 0, 'end': 2, 'text': 'This sentence continues here.'}])

    def test_resumed_fractional_offset_does_not_cross_five_minute_boundary(self):
        class QuietTask(FakeTask):
            def send(inner, frame): inner.duration += len(frame)/32000
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); progress = folder/'a.progress.json'
            progress.write_text(json.dumps({'version': 1, 'until': 299.95, 'next_chunk': 9,
                'cues': [{'start': 1, 'end': 2, 'text': 'Earlier.'}]}))
            with patch.dict(os.environ, {'DASHSCOPE_API_KEY_1': 'first', 'DASHSCOPE_API_KEY_2': 'second'}), \
                 patch.object(aliyun, 'StreamTask', QuietTask), patch.object(aliyun, 'pcm_frames', return_value=iter([bytes(3200)])):
                events = list(aliyun.transcript_events('x', folder/'a.mp3', folder/'a.vtt', progress, None, threading.Event()))
            self.assertEqual(FakeTask.calls, [('second', 299.95, 9), ('first', 300, 10)])
            self.assertEqual(events[-1]['status'], 'done')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg not installed')
    def test_real_decoder_yields_pcm_before_the_download_finishes(self):
        release = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); source = folder/'source.mp3'; cache = folder/'cache.mp3'
            subprocess.run(['ffmpeg', '-nostdin', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=80',
                            '-ar', '16000', '-ac', '1', str(source)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            contents = source.read_bytes()
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    self.send_response(200); self.send_header('Content-Length', str(len(contents))); self.end_headers()
                    self.wfile.write(contents[:len(contents)//2]); self.wfile.flush()
                    if release.wait(5):
                        try: self.wfile.write(contents[len(contents)//2:]); self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError): pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            iterator = aliyun.pcm_frames(f'http://127.0.0.1:{server.server_port}/audio', cache, None, threading.Event(), 0)
            try:
                self.assertEqual(len(next(iterator)), 3200)
                self.assertFalse(cache.exists(), 'decoder waited for complete download')
                release.set()
                self.assertGreater(sum(len(frame) for frame in iterator), 32000*70)
                self.assertEqual(cache.read_bytes(), contents)
            finally:
                release.set(); iterator.close(); server.shutdown(); server.server_close()
