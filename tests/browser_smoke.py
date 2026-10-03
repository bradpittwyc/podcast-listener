"""Optional browser integration check: python tests/browser_smoke.py (requires Playwright + Edge)."""

import json
import queue
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    streams, errors = [], []
    with tempfile.TemporaryDirectory() as directory:
        audio_path = Path(directory) / "test.mp3"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=90",
                        "-ar", "16000", "-ac", "1", str(audio_path)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        audio_data = audio_path.read_bytes()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                route = urlparse(self.path).path
                if route == "/api/transcribe_stream":
                    messages = queue.Queue()
                    streams.append(messages)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    try:
                        self.wfile.write(b": connected\n\n")
                        self.wfile.flush()
                        while True:
                            item = messages.get(timeout=20)
                            self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, queue.Empty):
                        return
                elif route.startswith("/cache/"):
                    start, end = 0, len(audio_data) - 1
                    requested = self.headers.get("Range")
                    if requested:
                        limits = requested.removeprefix("bytes=").split("-")
                        start = int(limits[0] or 0)
                        end = min(end, int(limits[1])) if limits[1] else end
                    self.send_response(206 if requested else 200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Accept-Ranges", "bytes")
                    if requested:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(audio_data)}")
                    self.send_header("Content-Length", str(end - start + 1))
                    self.end_headers()
                    try:
                        self.wfile.write(audio_data[start:end + 1])
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                else:
                    content = (ROOT / "static/index.html").read_bytes() if route == "/" else b'{"results":[]}'
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8" if route == "/" else "application/json")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True,
                                                      args=["--autoplay-policy=no-user-gesture-required"])
                page = browser.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("https://cdnjs.cloudflare.com/**", lambda route: route.fulfill(body="", content_type="text/css"))
                page.route("https://via.placeholder.com/**", lambda route: route.abort())
                page.goto(f"http://127.0.0.1:{server.server_port}")
                page.evaluate("startPlay('/cache/one.mp3', 'Test', 'Show', '', '', '')")
                page.wait_for_function("subtitleStream && audio.readyState >= 1")
                assert page.evaluate("audio.paused && cues.length === 0")
                while not streams:
                    page.wait_for_timeout(20)
                streams[0].put({"status": "audio_ready", "local_audio": "/cache/one.mp3"})
                streams[0].put({"status": "cue", "cue": {"start": 0, "end": 29, "text": "Don't start before subtitles."}})
                page.wait_for_function("cues.length === 1")
                assert page.evaluate("audio.paused")
                streams[0].put({"status": "chunk_ready", "until": 30})
                page.wait_for_function("!audio.paused && audio.currentTime > 0")
                page.evaluate("audio.currentTime = 30.1")
                page.wait_for_function("audio.paused && waitingForSubtitles")
                streams[0].put({"status": "cue", "cue": {"start": 30, "end": 59, "text": "Second chunk."}})
                streams[0].put({"status": "chunk_ready", "until": 60})
                page.wait_for_function("!audio.paused && coveredUntil === 60")
                assert page.evaluate("audio.currentTime >= 30")
                page.click("#playBtn")
                assert page.evaluate("audio.paused && !wantsPlayback")
                streams[0].put({"status": "cue", "cue": {"start": 60, "end": 89, "text": "Third chunk."}})
                streams[0].put({"status": "chunk_ready", "until": 90})
                page.wait_for_function("coveredUntil === 90")
                assert page.evaluate("audio.paused")
                # An apostrophe word has a valid native inline handler.
                assert page.locator(".word").first.evaluate("el => typeof el.onclick === 'function'")
                page.evaluate("startPlay('/cache/two.mp3', 'Second', 'Show', '', '', '')")
                page.wait_for_function("nowPlaying.url === '/cache/two.mp3' && cues.length === 0")
                assert page.evaluate("audio.paused")
                assert not errors, errors
                print("Browser passed: waits for subtitles, frontier pause/resume, manual pause, episode switch, native audio and word handlers.")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
