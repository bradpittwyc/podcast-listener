import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import app
import transcription as stt


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.url = "https://example.com/episode.mp3"

    def test_staged_schedule(self):
        offset, sizes = 0, []
        while offset < 1200:
            size = stt.chunk_seconds(offset)
            sizes.append(size)
            offset += size
        self.assertEqual(sizes, [30] * 10 + [120] * 5 + [300])
        self.assertEqual(stt.chunk_seconds(299.999), 300 - 299.999)

    def test_missing_key_is_valid_sse_error_without_download(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": ""}), patch.object(app, "CACHE_DIR", str(self.cache)), patch.object(stt.requests, "get") as download:
            response = TestClient(app.app).get("/api/transcribe_stream", params={"audio_url": self.url})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-1]["status"], "error")
        self.assertIn("GROQ_API_KEY", events[-1]["detail"])
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

    def test_official_transcript_without_groq(self):
        response = Mock(ok=True, text="WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello\n")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"GROQ_API_KEY": ""}), patch.object(stt.requests, "get", return_value=response):
            events = list(stt.transcript_events(self.url, "https://example.com/cc.vtt", False, self.cache))
        self.assertEqual(events[0]["status"], "official")

    def test_empty_vtt_cache_is_not_accepted(self):
        _, audio, vtt = stt.cache_paths(self.cache, self.url)
        audio.write_bytes(b"audio")
        vtt.write_text("WEBVTT\n\n", encoding="utf-8")
        with patch.dict(os.environ, {"GROQ_API_KEY": ""}):
            events = list(stt.transcript_events(self.url, "", False, self.cache))
        self.assertEqual(events[-1]["status"], "error")

    def test_chunk_coverage_offsets_and_cache_only_after_completion(self):
        _, audio, vtt = stt.cache_paths(self.cache, self.url)
        audio.write_bytes(b"audio")
        durations = iter([30, 30, 4])
        def extract(command, **kwargs):
            duration = next(durations)
            with wave.open(command[-1], "wb") as f:
                f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                f.writeframes(b"\0\0" * (16000 * duration))
            return Mock(returncode=0)
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), patch.object(stt.shutil, "which", return_value="ffmpeg"), patch.object(stt.subprocess, "run", side_effect=extract), patch.object(stt, "groq_segments", return_value=[{"start": 1, "end": 3, "text": "Repeated phrase"}]):
            iterator = stt.transcript_events(self.url, "", False, self.cache)
            events = []
            for event in iterator:
                events.append(event)
                if event["status"] == "chunk_ready":
                    self.assertFalse(vtt.exists())
        self.assertEqual([e["until"] for e in events if e["status"] == "chunk_ready"], [30, 60, 64])
        self.assertEqual([e["cue"]["start"] for e in events if e["status"] == "cue"], [1, 31, 61])
        self.assertEqual(events[-1]["status"], "done")
        self.assertIn("00:01:01.000", vtt.read_text(encoding="utf-8"))
        self.assertFalse(list(self.cache.glob("groq-*")))

    def test_ffmpeg_failure_is_error_not_empty_success(self):
        _, audio, vtt = stt.cache_paths(self.cache, self.url)
        audio.write_bytes(b"audio")
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), patch.object(stt.shutil, "which", return_value="ffmpeg"), patch.object(stt.subprocess, "run", return_value=Mock(returncode=1)):
            events = list(stt.transcript_events(self.url, "", False, self.cache))
        self.assertEqual(events[-1]["status"], "error")
        self.assertFalse(vtt.exists())

    def test_groq_429_retry_and_timestamp_request(self):
        chunk = self.cache / "chunk.wav"
        chunk.write_bytes(b"wave")
        limited = Mock(status_code=429, headers={"Retry-After": "0"})
        success = Mock(status_code=200, ok=True)
        success.json.return_value = {"segments": [{"start": 0, "end": 1, "text": "Hello"}]}
        stopped = Mock()
        stopped.is_set.return_value = False
        stopped.wait.return_value = False
        with patch.object(stt.requests, "post", side_effect=[limited, success]) as post:
            segments = stt.groq_segments(chunk, "test-key", None, stopped)
        self.assertEqual(len(segments), 1)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["data"]["timestamp_granularities[]"], "segment")

    def test_disconnect_stops_before_next_api_request(self):
        stopped = threading.Event()
        stopped.set()
        with patch.object(stt, "groq_segments") as groq:
            self.assertEqual(list(stt.transcript_events(self.url, "", False, self.cache, stopped=stopped)), [])
        groq.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_real_ffmpeg_staged_slices(self):
        _, audio, vtt = stt.cache_paths(self.cache, self.url)
        with wave.open(str(audio), "wb") as f:
            f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            f.writeframes(b"\0\0" * (16000 * 921))
        durations = []
        def transcribe(chunk, *args):
            with wave.open(str(chunk), "rb") as f:
                durations.append(f.getnframes() / f.getframerate())
                self.assertEqual(f.getframerate(), 16000)
                self.assertEqual(f.getnchannels(), 1)
            self.assertLess(chunk.stat().st_size, 25000000)
            return [{"start": 0, "end": 1, "text": "Test speech"}]
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), patch.object(stt, "groq_segments", side_effect=transcribe):
            events = list(stt.transcript_events(self.url, "", False, self.cache))
        self.assertEqual(durations, [30] * 10 + [120] * 5 + [21])
        self.assertEqual(events[-1]["status"], "done")
        self.assertEqual([e["until"] for e in events if e["status"] == "chunk_ready"][-1], 921)
        self.assertTrue(vtt.exists())

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_first_subtitles_before_remaining_audio_is_downloaded(self):
        source = self.cache / "source.wav"
        with wave.open(str(source), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * (16000 * 80))
        contents = source.read_bytes()
        self.assert_first_subtitles_stream_before_tail(contents, 44 + 32000 * 40)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_mp3_first_subtitles_before_remaining_audio_is_downloaded(self):
        source = self.cache / "source.mp3"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=80",
                        "-ar", "16000", "-ac", "1", str(source)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        contents = source.read_bytes()
        self.assert_first_subtitles_stream_before_tail(contents, len(contents) // 2)

    def assert_first_subtitles_stream_before_tail(self, contents, prefix):
        release_tail = threading.Event()
        sent_tail = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(contents)))
                self.end_headers()
                try:
                    self.wfile.write(contents[:prefix])
                    self.wfile.flush()
                    if release_tail.wait(8):
                        self.wfile.write(contents[prefix:])
                        self.wfile.flush()
                        sent_tail.set()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        remote_url = f"http://127.0.0.1:{server.server_port}/audio.wav"
        _, audio, vtt = stt.cache_paths(self.cache, remote_url)
        groq_started_before_tail = []

        def transcribe(chunk, *args):
            groq_started_before_tail.append(not sent_tail.is_set())
            with wave.open(str(chunk), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
            return [{"start": 0, "end": duration, "text": "Streaming test"}]

        try:
            with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), patch.object(stt, "groq_segments", side_effect=transcribe):
                iterator = stt.transcript_events(remote_url, "", False, self.cache)
                events = []
                for event in iterator:
                    events.append(event)
                    if event["status"] == "chunk_ready":
                        self.assertEqual(event["until"], 30)
                        self.assertFalse(sent_tail.is_set(), "first chunk waited for the full download")
                        self.assertFalse(audio.exists())
                        break
                else:
                    self.fail(f"No first chunk: {events}")
                release_tail.set()
                events.extend(iterator)
            self.assertTrue(groq_started_before_tail[0])
            coverage = [e["until"] for e in events if e["status"] == "chunk_ready"]
            self.assertEqual(coverage[:2], [30, 60])
            self.assertAlmostEqual(coverage[-1], 80, delta=0.1)
            self.assertEqual(events[-1]["status"], "done")
            self.assertEqual(audio.read_bytes(), contents)
            self.assertTrue(vtt.exists())
        finally:
            release_tail.set()
            server.shutdown()
            server.server_close()

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


if __name__ == "__main__":
    unittest.main()
