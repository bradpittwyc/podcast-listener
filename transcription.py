"""Alibaba streaming transcription with atomic caches and cancellable SSE."""

import hashlib
import json
import math
import os
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path

import requests
import aliyun
import corrections

_locks = [threading.Lock() for _ in range(32)]
HEADERS = {"User-Agent": "Mozilla/5.0"}


class TranscriptionError(RuntimeError):
    def __init__(self, detail, retryable=True):
        super().__init__(detail)
        self.retryable = retryable


def read_checkpoint(path):
    empty = {"until": 0.0, "next_chunk": 0, "cues": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        until, index, cues = data["until"], data["next_chunk"], data["cues"]
        if data.get("version") != 1 or not isinstance(until, (int, float)) or not math.isfinite(until) or until <= 0:
            return empty
        if type(index) is not int or index < 0 or not isinstance(cues, list):
            return empty
        for cue in cues:
            start, end = cue["start"], cue["end"]
            if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= until + 0.001 or not isinstance(cue["text"], str):
                return empty
        result = {"until": until, "next_chunk": index, "cues": cues}
        pending = data.get('pending_sentence')
        if pending is not None:
            if (not isinstance(pending, dict) or not isinstance(pending.get('text'), str)
                    or not math.isfinite(pending['start']) or not math.isfinite(pending['end'])
                    or not 0 <= pending['start'] < pending['end'] <= until + .001):
                return empty
            result['pending_sentence'] = pending
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return empty


def chunk_seconds(offset):
    if offset < 300:
        return min(30, 300 - offset)
    if offset < 900:
        return min(120, 900 - offset)
    return 300


def cache_paths(cache_dir, audio_url):
    key = hashlib.md5(audio_url.encode("utf-8")).hexdigest()
    subtitle_key = key + '-aliyun-sentences-v2'
    return key, Path(cache_dir) / f"{key}.mp3", Path(cache_dir) / f"{subtitle_key}.vtt"


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


def transcript_events(audio_url, transcript_url, force_refresh, cache_dir, proxies=None, stopped=None):
    if stopped is not None and stopped.is_set():
        return
    path = corrections.path_for(cache_dir, audio_url)
    if force_refresh:
        path.unlink(missing_ok=True)
    for event in _transcript_events(audio_url, transcript_url, force_refresh, cache_dir, proxies, stopped):
        records = corrections.load(path)
        if event['status'] == 'cue':
            event = {**event, 'cue': corrections.apply([event['cue']], records)[0]}
        elif event['status'] == 'resumed':
            event = {**event, 'cues': corrections.apply(event['cues'], records)}
        elif event['status'] in ('official', 'cached'):
            event = {**event, 'vtt': corrections.apply_vtt(event['vtt'], records)}
        yield event


def _transcript_events(audio_url, transcript_url, force_refresh, cache_dir, proxies=None, stopped=None):
    stopped = stopped if stopped is not None else threading.Event()
    key, audio_path, vtt_path = cache_paths(cache_dir, audio_url)
    checkpoint_key = key + '-aliyun-sentences-v2'
    checkpoint_path = Path(cache_dir) / f"{checkpoint_key}.progress.json"
    lock = _locks[int(key[:8], 16) % len(_locks)]
    while not lock.acquire(timeout=0.2):
        if stopped.is_set():
            return
    try:
        if force_refresh:
            checkpoint_path.unlink(missing_ok=True)
            vtt_path.unlink(missing_ok=True)
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

        if not aliyun.api_keys():
            raise TranscriptionError("字幕服务未配置，请检查设置", retryable=False)
        if not shutil.which("ffmpeg"):
            raise TranscriptionError("找不到 FFmpeg；请安装并加入 PATH 后重启服务。", retryable=False)
        yield from aliyun.transcript_events(audio_url, audio_path, vtt_path, checkpoint_path, proxies, stopped)
    except Exception as exc:
        # Avoid returning request URLs or authorization information in errors.
        detail = str(exc) if isinstance(exc, RuntimeError) else "字幕转写失败，请检查网络或音频后重试。"
        yield {"status": "error", "detail": detail, "retryable": getattr(exc, "retryable", True)}
    finally:
        lock.release()


def stream_with_heartbeat(events_factory, stopped=None):
    stopped = stopped if stopped is not None else threading.Event()
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
                if event.get('status') in ('done', 'cached', 'official', 'error'):
                    break
        finally:
            iterator.close()
            send(None)

    threading.Thread(target=produce, daemon=True).start()
    try:
        yield ": connected\n\n"
        while not stopped.is_set():
            try:
                event = messages.get(timeout=15)
            except queue.Empty:
                yield 'data: {"status": "heartbeat"}\n\n'
                continue
            if event is None:
                return
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event.get('status') in ('done', 'cached', 'official', 'error'):
                return
    finally:
        stopped.set()
