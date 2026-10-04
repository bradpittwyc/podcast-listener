import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock

from fastapi.testclient import TestClient

import app
import transcription as stt


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        keys = patch.dict(os.environ, {"DASHSCOPE_API_KEY": "", "DASHSCOPE_API_KEY_1": "", "DASHSCOPE_API_KEY_2": ""})
        keys.start()
        self.addCleanup(keys.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.url = "https://example.com/episode.mp3"


    def test_missing_key_is_valid_sse_error_without_download(self):
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY_1": ""}), patch.object(app, "CACHE_DIR", str(self.cache)), patch.object(stt.requests, "get") as download:
            response = TestClient(app.app).get("/api/transcribe_stream", params={"audio_url": self.url})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-1]["status"], "error")
        self.assertFalse(events[-1]["retryable"])
        download.assert_not_called()
        self.assertEqual(response.headers["x-accel-buffering"], "no")

    def test_failed_download_does_not_leave_audio_cache(self):
        _, audio, _ = stt.cache_paths(self.cache, self.url)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        def incomplete(**kwargs):
            yield b"partial audio"
            raise stt.requests.ConnectionError("download interrupted")
        response.iter_content = incomplete
        with patch.object(stt.requests, "get", return_value=response):
            with self.assertRaises(stt.requests.ConnectionError):
                stt.download_audio(self.url, audio, None, threading.Event())
        self.assertFalse(audio.exists())
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_official_transcript_without_keys(self):
        response = Mock(ok=True, text="WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello\n")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY_1": ""}), patch.object(stt.requests, "get", return_value=response):
            events = list(stt.transcript_events(self.url, "https://example.com/cc.vtt", False, self.cache))
        self.assertEqual(events[0]["status"], "official")

    def test_empty_vtt_cache_is_not_accepted(self):
        _, audio, vtt = stt.cache_paths(self.cache, self.url)
        audio.write_bytes(b"audio")
        vtt.write_text("WEBVTT\n\n", encoding="utf-8")
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY_1": ""}):
            events = list(stt.transcript_events(self.url, "", False, self.cache))
        self.assertEqual(events[-1]["status"], "error")


    def test_disconnect_stops_before_next_api_request(self):
        stopped = threading.Event()
        stopped.set()
        with patch.object(stt.aliyun, "transcript_events") as provider:
            self.assertEqual(list(stt.transcript_events(self.url, "", False, self.cache, stopped=stopped)), [])
        provider.assert_not_called()


    def test_audio_proxy_streams_and_forwards_range(self):
        response = Mock(status_code=206, headers={"Content-Type": "audio/mpeg", "Content-Range": "bytes 10-12/50", "Accept-Ranges": "bytes"})
        response.iter_content.return_value = iter([b"abc"])
        with patch.object(app, "CACHE_DIR", str(self.cache)), patch.object(app.requests, "get", return_value=response) as get:
            result = TestClient(app.app).get("/api/audio", params={"url": self.url}, headers={"Range": "bytes=10-12"})
        self.assertEqual(result.status_code, 206)
        self.assertEqual(result.content, b"abc")
        self.assertEqual(result.headers["content-range"], "bytes 10-12/50")
        self.assertEqual(get.call_args.kwargs["headers"]["Range"], "bytes=10-12")
        response.close.assert_called()

    def test_audio_proxy_uses_complete_cache_for_range_requests(self):
        _, audio, _ = stt.cache_paths(self.cache, self.url)
        audio.write_bytes(b"0123456789")
        with patch.object(app, "CACHE_DIR", str(self.cache)), patch.object(app.requests, "get") as get:
            result = TestClient(app.app).get("/api/audio", params={"url": self.url}, headers={"Range": "bytes=2-4"})
        self.assertEqual(result.status_code, 206)
        self.assertEqual(result.content, b"234")
        get.assert_not_called()


    def test_corrupt_checkpoint_is_ignored(self):
        path = self.cache / 'broken.progress.json'
        path.write_text('{"version":1,"until":30,"next_chunk":1,"cues":[{"start":0,"end":99,"text":"bad"}]}')
        self.assertEqual(stt.read_checkpoint(path)['until'], 0)

    def test_qa_requires_complete_transcript_and_awaits_full_context(self):
        client = Mock()
        client.aio.models.generate_content = AsyncMock(return_value=Mock(text='Answer'))
        payload = {'question':'Explain', 'audio_url':self.url, 'full_transcript':'x' * 30000 + ' END_OF_EPISODE'}
        with patch.object(app,'gemini_client',client), patch.object(app,'CACHE_DIR',str(self.cache)):
            api = TestClient(app.app)
            rejected = api.post('/api/ask', json=payload)
            self.assertEqual(rejected.status_code,409)
            client.aio.models.generate_content.assert_not_called()
            accepted = api.post('/api/ask', json={**payload,'transcript_complete':True})
        self.assertEqual(accepted.json()['status'],'success')
        client.aio.models.generate_content.assert_awaited_once()
        self.assertIn(payload['full_transcript'],client.aio.models.generate_content.call_args.kwargs['contents'])


if __name__ == "__main__":
    unittest.main()
