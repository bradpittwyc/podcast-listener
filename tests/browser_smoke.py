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
                            if item is None:
                                return
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
                page.wait_for_function("subtitleStream !== null")
                assert page.evaluate("audio.paused && cues.length === 0")
                while not streams:
                    page.wait_for_timeout(20)
                streams[0].put({"status": "audio_ready", "local_audio": "/cache/one.mp3"})
                streams[0].put({"status": "cue", "cue": {"start": 0, "end": 29, "text": "Don't start before subtitles."}})
                page.wait_for_function("cues.length === 1")
                assert page.evaluate("audio.paused")
                streams[0].put({"status": "chunk_ready", "until": 30})
                page.wait_for_function("!audio.paused && audio.currentTime > 0")
                page.evaluate("window.firstCueNode=document.getElementById('cue-0');renderSubs()")
                assert page.evaluate("window.firstCueNode===document.getElementById('cue-0')")
                page.evaluate("audio.currentTime = 30.1")
                page.wait_for_function("audio.paused && waitingForSubtitles")
                page.evaluate("skipAudio(-20)")
                page.wait_for_function("!audio.paused && !waitingForSubtitles && audio.currentTime < 30")
                page.click("#playBtn")
                page.evaluate("seekPlayback(5)")
                page.wait_for_timeout(100)
                assert page.evaluate("audio.paused && !wantsPlayback")
                page.evaluate("togglePlay();seekPlayback(30.1)")
                page.wait_for_function("audio.paused && waitingForSubtitles")
                # End the real SSE connection and wait for the client's automatic retry.
                streams[0].put(None)
                page.wait_for_function("subtitleRequest >= 3 && subtitleStream !== null")
                for _ in range(100):
                    if len(streams) >= 2:
                        break
                    page.wait_for_timeout(20)
                assert len(streams) >= 2
                active_stream = streams[1]
                active_stream.put({"status":"resumed", "until":30, "cues":[{"start":0,"end":29,"text":"Don't start before subtitles."}]})
                active_stream.put({"status": "cue", "cue": {"start": 30, "end": 59, "text": "Second chunk."}})
                active_stream.put({"status": "chunk_ready", "until": 60})
                page.wait_for_function("!audio.paused && coveredUntil === 60")
                assert page.evaluate("audio.currentTime >= 30")
                page.evaluate("loopA=30;loopB=60;loopOn=true;seekPlayback(60.1)")
                page.wait_for_function("!audio.paused && audio.currentTime < 60")
                assert page.evaluate("!waitingForSubtitles")
                page.evaluate("clearLoop()")
                page.click("#playBtn")
                assert page.evaluate("audio.paused && !wantsPlayback")
                active_stream.put({"status": "cue", "cue": {"start": 60, "end": 89, "text": "Third chunk."}})
                active_stream.put({"status": "chunk_ready", "until": 90})
                page.wait_for_function("coveredUntil === 90")
                assert page.evaluate("audio.paused")
                # Apostrophe words are data, never compiled as inline JavaScript.
                assert page.locator(".word").first.evaluate("el => el.dataset.word === \"don't\"")
                active_stream.put({"status":"done"})
                page.wait_for_function("subtitlesComplete")
                page.evaluate("audio.currentTime=audio.duration-.2;togglePlay()")
                page.wait_for_function("audio.ended",timeout=10000)
                page.evaluate("resumeAndroidPlayback()")
                page.wait_for_timeout(200)
                assert page.evaluate("audio.paused && playbackEnded && !wantsPlayback")
                author='Author "Quoted" onmouseover="window.auditMarker=1'
                page.evaluate("author=>renderChannel({title:'Test',author,image:'',episodes:[{title:'Example',audioUrl:'/cache/three.mp3'}]})",author)
                item=page.locator('.ep-item')
                assert item.get_attribute('onclick') is None
                assert item.get_attribute('onmouseover') is None
                page.evaluate("()=>{playByIdx=(idx)=>{window.clickedEpisode=currentEpisodes[idx].title}}")
                page.evaluate("expandSidebar()")
                item.click()
                assert page.evaluate("window.clickedEpisode==='Example' && window.auditMarker===undefined")
                page.evaluate("startPlay('/cache/two.mp3', 'Second', 'Show', '', '', '')")
                page.wait_for_function("nowPlaying.url === '/cache/two.mp3' && cues.length === 0")
                assert page.evaluate("audio.paused")
                assert not errors, errors
                print("Browser passed: playback completion, seek back, AB frontier loop, safe RSS clicks, incremental DOM, SSE recovery, manual pause and episode switch.")
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
