"""Groq transcription with atomic caches and playback-independent SSE events."""

import hashlib
import json
import math
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

import requests

_locks = [threading.Lock() for _ in range(32)]
HEADERS = {"User-Agent": "Mozilla/5.0"}


def chunk_seconds(offset):
    if offset < 300:
        return min(30, 300 - offset)
    if offset < 900:
        return min(120, 900 - offset)
    return 300


def cache_paths(cache_dir, audio_url):
    key = hashlib.md5(audio_url.encode("utf-8")).hexdigest()
    return key, Path(cache_dir) / f"{key}.mp3", Path(cache_dir) / f"{key}.vtt"


def to_vtt(cues):
    def timestamp(seconds):
        milliseconds = round(seconds * 1000)
        hours, milliseconds = divmod(milliseconds, 3600000)
        minutes, milliseconds = divmod(milliseconds, 60000)
        seconds, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"

    return "WEBVTT\n\n" + "\n\n".join(
        f"{timestamp(c['start'])} --> {timestamp(c['end'])}\n{c['text']}" for c in cues
    ) + "\n"


def atomic_write(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode="w", encoding="utf-8") as f:
        temporary = Path(f.name)
        try:
            f.write(content)
        except BaseException:
            f.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def download_audio(audio_url, path, proxies, stopped, on_data=None):
    # A failed download must never become a reusable audio cache.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        try:
            with requests.get(audio_url, headers=HEADERS, stream=True, timeout=(10, 15), proxies=proxies) as r:
                r.raise_for_status()
                # Small network reads matter for low-bitrate MP3: 64 KiB can contain
                # more than 20 seconds, hiding the last bytes of the first segment.
                for chunk in r.iter_content(chunk_size=16 * 1024):
                    if stopped.is_set():
                        return False
                    f.write(chunk)
                    if on_data:
                        on_data(chunk)
            if f.tell() == 0:
                raise RuntimeError("音频下载为空，请检查 RSS 音频链接。")
            f.close()
            os.replace(temporary, path)
            return True
        finally:
            f.close()
            temporary.unlink(missing_ok=True)


def local_audio_chunks(audio_path, work_dir, stopped):
    offset = 0.0
    while not stopped.is_set():
        length = chunk_seconds(offset)
        chunk = Path(work_dir) / "chunk.wav"
        command = ["ffmpeg", "-nostdin", "-y", "-ss", f"{offset:.6f}", "-i", str(audio_path),
                   "-t", str(length), "-map", "0:a:0", "-vn", "-ar", "16000", "-ac", "1",
                   "-c:a", "pcm_s16le", str(chunk)]
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("FFmpeg 无法处理此音频，请检查文件格式或重新下载。")
        with wave.open(str(chunk), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
        if duration < 0.01:
            return
        yield {"status": "chunk", "path": chunk, "offset": offset, "duration": duration}
        offset += duration
        if duration < length - 0.001:
            return


def remote_audio_chunks(audio_url, audio_path, work_dir, proxies, stopped):
    """Tee one HTTP download to the original cache and a streaming PCM decoder.

    Decode and download keep running while Groq processes an earlier WAV chunk.
    Audio bytes are never exposed as a complete cache until the HTTP request finishes.
    """
    command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-probesize", "65536", "-analyzeduration", "1000000", "-i", "pipe:0", "-map", "0:a:0",
               "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages = queue.Queue()
    cancel = threading.Event()
    downloaded = threading.Event()
    result = {}

    class Cancelled:
        def is_set(self):
            return stopped.is_set() or cancel.is_set()

    cancelled = Cancelled()

    def feed(data):
        if result.get("pipe_closed") or cancelled.is_set():
            return
        try:
            process.stdin.write(data)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            # Let the consumer detect decoder failure and cancel this download.
            result["pipe_closed"] = True

    def transfer():
        try:
            result["complete"] = download_audio(audio_url, audio_path, proxies, cancelled, feed)
            if result["complete"]:
                messages.put({"status": "audio_ready"})
        except Exception:
            result["download_error"] = True
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            downloaded.set()

    def decode():
        try:
            samples, index = 0, 0
            while not cancelled.is_set():
                offset = samples / 16000
                needed = round(chunk_seconds(offset) * 16000) * 2
                data = bytearray()
                while len(data) < needed and not cancelled.is_set():
                    part = process.stdout.read(min(64 * 1024, needed - len(data)))
                    if not part:
                        break
                    data.extend(part)
                # PCM16 must contain complete samples.
                del data[len(data) // 2 * 2:]
                if not data or cancelled.is_set():
                    break
                if len(data) < needed:
                    if process.wait(timeout=15):
                        result["decode_error"] = True
                        break
                    # Only a successful HTTP EOF can produce the final short chunk.
                    # A broken connection must not turn a truncated buffer into subtitles.
                    while not downloaded.wait(0.2):
                        if cancelled.is_set():
                            return
                    if not result.get("complete") or result.get("download_error"):
                        break
                chunk = Path(work_dir) / f"{index:05d}.wav"
                with wave.open(str(chunk), "wb") as wav:
                    wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    wav.writeframes(data)
                duration = len(data) / 32000
                samples += len(data) // 2
                index += 1
                messages.put({"status": "chunk", "path": chunk, "offset": offset, "duration": duration})
                if len(data) < needed:
                    break
            result["decoded_chunks"] = index
        except Exception:
            result["decode_error"] = True
        finally:
            messages.put({"status": "decoder_finished"})

    downloader = threading.Thread(target=transfer, daemon=True)
    decoder = threading.Thread(target=decode, daemon=True)
    decoder.start()
    downloader.start()
    try:
        while not cancelled.is_set():
            try:
                event = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if event["status"] != "decoder_finished":
                yield event
                continue
            return_code = process.wait(timeout=15)
            if return_code or result.get("decode_error"):
                # Never wait for the rest of an episode just to retry a decoder.
                # A completed download may already exist, in which case seeking is cheap.
                if downloaded.is_set() and result.get("complete") and result.get("decoded_chunks", 0) == 0:
                    yield {"status": "audio_ready"}
                    yield from local_audio_chunks(audio_path, work_dir, stopped)
                    return
                raise RuntimeError("此音频无法流式解码或连接已中断，请检查音频源后重试。")
            # Confirm the HTTP request really ended; an interrupted file is not EOF.
            while not downloaded.wait(0.2):
                if cancelled.is_set():
                    return
            if result.get("download_error") or not result.get("complete"):
                raise RuntimeError("音频下载中断，请检查网络后刷新字幕。")
            return
    finally:
        cancel.set()
        if process.poll() is None:
            process.kill()
        downloader.join(timeout=20)
        decoder.join(timeout=5)
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()


def groq_segments(chunk_path, api_key, proxies, stopped):
    for attempt in range(3):
        if stopped.is_set():
            return None
        with chunk_path.open("rb") as f:
            response = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (chunk_path.name, f, "audio/wav")},
                data={"model": "whisper-large-v3-turbo", "response_format": "verbose_json",
                      "language": "en", "timestamp_granularities[]": "segment"},
                timeout=(15, 120), proxies=proxies,
            )
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 2:
                try:
                    delay = min(30, max(1, float(response.headers.get("Retry-After", 2 ** (attempt + 1)))))
                except ValueError:
                    delay = 2 ** (attempt + 1)
                response.close()
                if stopped.wait(delay):
                    return None
                continue
        if not response.ok:
            status = response.status_code
            response.close()
            raise RuntimeError(f"Groq 转写失败（HTTP {status}），请检查密钥、额度或网络。")
        try:
            data = response.json()
        finally:
            response.close()
        segments = data.get("segments")
        if not isinstance(segments, list):
            raise RuntimeError("Groq 未返回带时间戳的字幕，请重试。")
        return segments


def transcript_events(audio_url, transcript_url, force_refresh, cache_dir, proxies=None, stopped=None):
    stopped = stopped if stopped is not None else threading.Event()
    key, audio_path, vtt_path = cache_paths(cache_dir, audio_url)
    lock = _locks[int(key[:8], 16) % len(_locks)]
    while not lock.acquire(timeout=0.2):
        if stopped.is_set():
            return
    try:
        if stopped.is_set():
            return
        if transcript_url and not force_refresh:
            try:
                with requests.get(transcript_url, headers=HEADERS, timeout=(10, 15), proxies=proxies) as r:
                    if r.ok and r.text.lstrip("\ufeff \r\n").startswith("WEBVTT") and "-->" in r.text:
                        yield {"status": "official", "vtt": r.text}
                        return
            except requests.RequestException:
                pass

        if vtt_path.exists() and audio_path.exists() and audio_path.stat().st_size and not force_refresh:
            content = vtt_path.read_text(encoding="utf-8")
            if content.startswith("WEBVTT") and "-->" in content:
                yield {"status": "cached", "vtt": content, "local_audio": f"/cache/{key}.mp3"}
                return

        api_key_env = os.environ.get("GROQ_API_KEY", "").strip()
        if not api_key_env:
            raise RuntimeError("未配置 GROQ_API_KEY；请配置密钥后刷新字幕，字幕就绪后播放。")
        api_keys = [k.strip() for k in api_key_env.split(",") if k.strip()]
        
        if not shutil.which("ffmpeg"):
            raise RuntimeError("找不到 FFmpeg；请安装并加入 PATH 后重启服务。")

        # Start decoding incoming bytes immediately; never wait for the full download.
        with tempfile.TemporaryDirectory(prefix="groq-", dir=cache_dir) as work_dir:
            if force_refresh or not audio_path.exists() or not audio_path.stat().st_size:
                yield {"status": "progress", "detail": "正在边下载边切片，首段字幕就绪后播放。"}
                yield {"status": "audio_source", "audio_url": "/api/audio?url=" + requests.utils.quote(audio_url, safe="")}
                chunks = remote_audio_chunks(audio_url, audio_path, work_dir, proxies, stopped)
            else:
                yield {"status": "audio_ready", "local_audio": f"/cache/{key}.mp3"}
                chunks = local_audio_chunks(audio_path, work_dir, stopped)
            cues = []
            try:
                for i, item in enumerate(chunks):
                    if stopped.is_set():
                        return
                    if item["status"] == "audio_ready":
                        yield {"status": "audio_ready", "local_audio": f"/cache/{key}.mp3"}
                        continue
                    if item["status"] != "chunk":
                        yield item
                        continue
                    chunk, offset, duration = item["path"], item["offset"], item["duration"]
                    yield {"status": "progress", "detail": f"Groq 正在转写 {offset / 60:.1f}–{(offset + duration) / 60:.1f} 分钟"}
                    
                    # 轮询使用 API Key
                    current_key = api_keys[i % len(api_keys)]
                    
                    try:
                        segments = groq_segments(chunk, current_key, proxies, stopped)
                    finally:
                        chunk.unlink(missing_ok=True)
                    if segments is None or stopped.is_set():
                        return
                    for segment in segments:
                        text = str(segment.get("text", "")).strip()
                        start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
                        if not text or not math.isfinite(start) or not math.isfinite(end):
                            continue
                        start, end = max(0.0, start), min(duration, end)
                        if end <= start:
                            continue
                        cue = {"start": start + offset, "end": end + offset, "text": text}
                        cues.append(cue)
                        yield {"status": "cue", "cue": cue}
                    yield {"status": "chunk_ready", "until": offset + duration}
            finally:
                chunks.close()
            if not cues:
                raise RuntimeError("Groq 未识别到语音，请刷新重试。")
            if stopped.is_set():
                return
            atomic_write(vtt_path, to_vtt(cues))
            yield {"status": "done"}
    except Exception as exc:
        # Avoid returning request URLs or authorization information in errors.
        detail = str(exc) if isinstance(exc, RuntimeError) else "字幕转写失败，请检查网络或音频后重试。"
        yield {"status": "error", "detail": detail}
    finally:
        lock.release()


def stream_with_heartbeat(events_factory):
    stopped = threading.Event()
    messages = queue.Queue(maxsize=32)

    def send(item):
        while not stopped.is_set():
            try:
                messages.put(item, timeout=0.2)
                return
            except queue.Full:
                pass

    def produce():
        iterator = events_factory(stopped)
        try:
            for event in iterator:
                if stopped.is_set():
                    break
                send(event)
        finally:
            iterator.close()
            send(None)

    threading.Thread(target=produce, daemon=True).start()
    try:
        yield ": connected\n\n"
        while True:
            try:
                event = messages.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            if event is None:
                return
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        stopped.set()
