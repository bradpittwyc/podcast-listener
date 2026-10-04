"""Duplex Alibaba ASR streams, alternating keys with durable subtitle checkpoints."""
import os
import requests
import json
import math
import re
import queue
import subprocess
import threading
import time
import uuid
import websocket

_retry_key_lock = threading.Lock()
_retry_key_index = 0


def regenerate(audio_url, audio_path, start, end, stopped=None):
    """Decode and upload only a sentence window; word alignment removes context."""
    global _retry_key_index
    stopped = stopped if stopped is not None else threading.Event()
    if stopped.is_set(): raise ASRError('转写已取消。', retryable=False)
    keys = api_keys()
    if not keys:
        raise ASRError('字幕服务未配置，请检查设置', retryable=False)
    with _retry_key_lock:
        key = keys[_retry_key_index % len(keys)]
        _retry_key_index += 1
    begin = max(0, start - .5)
    command = ['ffmpeg', '-nostdin', '-loglevel', 'error']
    if not audio_path.exists():
        command += ['-rw_timeout', '15000000', '-user_agent', 'Mozilla/5.0']
    command += ['-ss', str(begin), '-i', str(audio_path) if audio_path.exists() else audio_url,
                '-t', str(end - begin + .5), '-map', '0:a:0', '-vn', '-ar', '16000', '-ac', '1', '-f', 's16le', 'pipe:1']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    remove = stopped.on_cancel(process.kill) if hasattr(stopped, 'on_cancel') else lambda: None
    try:
        pcm, _ = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill(); process.communicate()
        raise ASRError('读取单句音频超时，请重试。') from None
    finally:
        remove()
    if stopped.is_set(): raise ASRError('转写已取消。', retryable=False)
    if process.returncode or not pcm:
        raise ASRError('无法读取这句音频，请检查音频源后重试。')
    # This buffer is bounded to one sentence, not an episode.
    task = StreamTask(key, begin, 0, os.environ.get('ALIYUN_REGION', 'beijing'), stopped)
    task.window = (start, end)
    results = []; failure = []
    def upload():
        try:
            for index in range(0, len(pcm), 3200):
                if task.closed.is_set() or stopped.is_set(): return
                task.send(pcm[index:index+3200]); time.sleep(.02)
            task.finish()
        except Exception:
            failure.append(ASRError('单句音频上传失败，请重试。'))
    sender = threading.Thread(target=upload, daemon=True); sender.start()
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            if stopped.is_set(): raise ASRError('转写已取消。', retryable=False)
            if failure: raise failure[0]
            try: message = task.messages.get(timeout=.2)
            except queue.Empty: continue
            if message['status'] == 'error': raise message['error']
            if message['status'] == 'finished': break
            cue = message['cue']
            if (cue['start'] + cue['end']) / 2 >= start and (cue['start'] + cue['end']) / 2 <= end:
                results.append(cue)
        else:
            raise ASRError('单句转写超时，请重试。')
        if not results: raise ASRError('这一句未识别到语音，原字幕已保留。')
        combined = None
        for cue in results: combined = append_fragment(combined, cue)
        combined.update(start=max(start, combined['start']), end=min(end, combined['end']), source_start=start, source_end=end)
        return combined
    finally:
        task.close(); sender.join(timeout=3)


class ASRError(RuntimeError):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def task_error(header):
    code = str(header.get('error_code', '')).lower()
    permanent = any(word in code for word in ('auth', 'apikey', 'api_key', 'invalidparameter', 'accessdenied', 'modelnotfound', 'arrearage', 'quota', 'freetieronly'))
    return ASRError('阿里云转写额度或模型权限不可用，请检查阿里云模型额度设置。' if permanent else
                    '阿里云转写暂时失败，正在从断点重连。', retryable=not permanent)


def root(region=None):
    region = region or os.environ.get("ALIYUN_REGION", "beijing")
    return "https://dashscope-intl.aliyuncs.com" if region == "singapore" else "https://dashscope.aliyuncs.com"


def api_keys():
    first = os.environ.get("DASHSCOPE_API_KEY_1", "") or os.environ.get("DASHSCOPE_API_KEY", "")
    return list(dict.fromkeys(k.strip() for k in (first, os.environ.get("DASHSCOPE_API_KEY_2", "")) if k.strip()))


def aligned_tokens(sentence):
    """Use canonical sentence spelling/spacing; ASR 'words' may be subword pieces."""
    canonical = sentence.get('text', '')
    if not canonical: return None
    normalize = lambda text: text.lower().replace('’', "'").replace('‘', "'")
    search = normalize(canonical); cursor = 0; tokens = []
    for word in sentence.get('words', []):
        token = str(word.get('text', '')).strip()
        if not token:
            tokens.append(''); continue
        punctuation = str(word.get('punctuation') or '')
        if punctuation and not token.endswith(punctuation): token += punctuation
        position = search.find(normalize(token), cursor)
        if position < 0:
            return None
        # A missing lexical token cannot be assigned someone else's timestamp.
        if re.search(r'\w', canonical[cursor:position]): return None
        finish = position + len(token)
        tokens.append(canonical[cursor:finish]); cursor = finish
    if re.search(r'\w', canonical[cursor:]): return None
    if tokens: tokens[-1] += canonical[cursor:]
    return tokens


def sentence_cues(sentence, offset, bounds=None):
    """Split stable ASR words only at sentence-ending punctuation."""
    words = sentence.get('words')
    if not isinstance(words, list) or not words:
        return [{'start': offset + sentence['begin_time'] / 1000,
                 'end': offset + sentence['end_time'] / 1000, 'text': sentence['text'].strip()}]
    tokens = aligned_tokens(sentence)
    if sentence.get('text') and tokens is None:
        return [{'start': offset + sentence['begin_time'] / 1000,
                 'end': offset + sentence['end_time'] / 1000, 'text': sentence['text'].strip()}]
    parts = []; text = ''; start = end = None
    for index, word in enumerate(words):
        begin, finish = word.get('begin_time'), word.get('end_time')
        if (not isinstance(begin, (int, float)) or not isinstance(finish, (int, float))
                or not math.isfinite(begin) or not math.isfinite(finish) or not 0 <= begin < finish):
            # Never manufacture equally spaced timestamps for words without alignment.
            return [{'start': offset + sentence['begin_time'] / 1000,
                     'end': offset + sentence['end_time'] / 1000, 'text': sentence['text'].strip()}]
        if bounds and not bounds[0] <= offset + (begin + finish) / 2000 <= bounds[1]:
            continue
        if tokens is not None:
            token = tokens[index]
        else:
            token = str(word.get('text', ''))
            punctuation = str(word.get('punctuation') or '')
            if punctuation and not token.rstrip().endswith(punctuation):
                token = token.rstrip() + punctuation
        if not token.strip():
            continue
        if start is None:
            start = begin
        if tokens is None and text and not token[0].isspace() and re.search(r'[A-Za-z0-9]$', text) and re.match(r'[A-Za-z0-9]', token) and token.lower() != "n't":
            text += ' '
        text += token; end = finish
        if ends_sentence(text) and not (tokens is not None and index + 1 < len(tokens) and tokens[index+1] and not tokens[index+1][0].isspace() and re.match(r'[A-Za-z0-9]', tokens[index+1])):
            parts.append({'start': offset + start / 1000, 'end': offset + end / 1000, 'text': text.strip()})
            text = ''; start = None
    if text:
        parts.append({'start': offset + start / 1000, 'end': offset + end / 1000, 'text': text.strip()})
    return parts


def ends_sentence(text):
    return bool(re.search(r'[.!?。！？][\"\u201d\u2019\)\]]*$', text.rstrip()))


def append_fragment(pending, cue):
    if pending is None:
        return dict(cue)
    previous, following = pending['text'].rstrip(), cue['text'].lstrip()
    separator = ' ' if re.search(r'[A-Za-z0-9]$', previous) and re.match(r'[A-Za-z0-9]', following) else ''
    return {'start': pending['start'], 'end': cue['end'], 'text': previous + separator + following}


def pcm_frames(audio_url, audio_path, proxies, stopped, resume):
    from transcription import download_audio
    cached = audio_path.exists() and audio_path.stat().st_size > 0
    source = str(audio_path) if cached else "pipe:0"
    command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-probesize", "65536", "-analyzeduration", "1000000"]
    if cached:
        command += ["-ss", str(resume)]
    command += ["-i", source, "-map", "0:a:0", "-vn", "-ar", "16000", "-ac", "1", "-f", "s16le", "pipe:1"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result = {}; finished = threading.Event()

    def transfer():
        try:
            def feed(data):
                if not stopped.is_set():
                    process.stdin.write(data)
                    process.stdin.flush()
            result["complete"] = download_audio(audio_url, audio_path, proxies, stopped, feed)
        except Exception:
            result["complete"] = False
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            finished.set()

    def watchdog():
        while process.poll() is None:
            if stopped.wait(.2):
                process.kill()
                return

    watcher = threading.Thread(target=watchdog, daemon=True); watcher.start()
    downloader = None
    if not cached:
        downloader = threading.Thread(target=transfer, daemon=True); downloader.start()
    try:
        remaining = 0 if cached else round(resume * 32000)
        while remaining and not stopped.is_set():
            part = process.stdout.read(min(3200, remaining))
            if not part:
                raise RuntimeError("音频长度短于已保存的断点。")
            remaining -= len(part)
        while not stopped.is_set():
            frame = process.stdout.read(3200)
            if not frame:
                break
            yield frame
        if not stopped.is_set():
            if process.wait(timeout=15):
                raise RuntimeError("音频流式解码失败。")
            if not cached:
                if not finished.wait(20) or not result.get("complete"):
                    raise RuntimeError("音频下载中断，请重连。")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        if downloader:
            downloader.join(timeout=2)
        else:
            process.stdin.close()


class StreamTask:
    """A duplex WebSocket for one recoverable segment; results arrive during upload."""
    def __init__(self, key, offset, index, region, stopped=None):
        self.offset, self.index, self.duration = offset, index, 0
        self.messages = queue.Queue(maxsize=32)
        self.closed = threading.Event()
        self.deadline = time.monotonic() + (30 if offset < 300 else 120 if offset < 900 else 300) / 5 + 120
        self.remove_cancel = lambda: None
        self.finish_started = None
        self.window = None
        self.task_id = uuid.uuid4().hex
        host = root(region).split('://')[1]
        try:
            self.ws = websocket.create_connection('wss://' + host + '/api-ws/v1/inference',
                header=['Authorization: Bearer ' + key], timeout=20, http_no_proxy=[host])
        except websocket.WebSocketBadStatusException as error:
            raise ASRError('阿里云连接被拒绝，请检查密钥和地域。', retryable=error.status_code not in (400, 401, 403, 404)) from None
        except Exception:
            raise ASRError("阿里云 Streaming 连接失败，请检查网络、地域和密钥。") from None
        if stopped is not None and hasattr(stopped, 'on_cancel'):
            self.remove_cancel = stopped.on_cancel(self.cancel)
        try:
            if stopped is not None and stopped.is_set():
                raise ASRError('转写已取消。', retryable=False)
            self.ws.send(json.dumps({'header': {'action': 'run-task', 'task_id': self.task_id, 'streaming': 'duplex'},
            'payload': {'task_group': 'audio', 'task': 'asr', 'function': 'recognition',
                'model': 'qwen-audio-3.0-asr-flash-streaming',
                'parameters': {'format': 'pcm', 'sample_rate': 16000, 'max_sentence_silence': 500, 'multi_threshold_mode_enabled': True, 'heartbeat': True}, 'input': {}}}))
            reply = json.loads(self.ws.recv())
            if reply.get('header', {}).get('event') != 'task-started':
                raise task_error(reply.get('header', {}))
        except Exception:
            self.remove_cancel()
            self.ws.close()
            raise
        self.ws.settimeout(1)
        self.receiver = threading.Thread(target=self.receive, daemon=True); self.receiver.start()

    def receive(self):
        seen = set(); last = time.monotonic()
        try:
            while not self.closed.is_set():
                if time.monotonic() > getattr(self, 'deadline', float('inf')):
                    raise ASRError('阿里云单段转写超过时限，正在从断点重连。')
                if self.finish_started is not None and time.monotonic() - self.finish_started > 45:
                    raise ASRError('阿里云未确认任务结束，正在从断点重连。')
                try:
                    raw = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    if time.monotonic() - last > 90:
                        raise RuntimeError("阿里云 Streaming 长时间未响应，正在从断点重连。")
                    continue
                if not raw:
                    raise RuntimeError("阿里云 Streaming 连接中断，正在从断点重连。")
                last = time.monotonic(); data = json.loads(raw)
                event = data.get('header', {}).get('event')
                if event == 'task-failed':
                    raise task_error(data.get('header', {}))
                if event == 'task-finished':
                    self.put({'status': 'finished'}); return
                sentence = data.get('payload', {}).get('output', {}).get('sentence', {})
                if event == 'result-generated' and sentence.get('sentence_end') and not sentence.get('heartbeat'):
                    ident = (sentence.get('sentence_id'), sentence.get('begin_time'), sentence.get('end_time'))
                    if ident in seen:
                        continue
                    seen.add(ident)
                    if sentence.get('text', '').strip() and sentence.get('end_time') is not None:
                        for cue in sentence_cues(sentence, self.offset, self.window):
                            self.put({'status': 'cue', 'cue': cue})
        except Exception as error:
            if not self.closed.is_set():
                self.put({'status': 'error', 'error': error if isinstance(error, RuntimeError) else
                                  RuntimeError("阿里云 Streaming 连接中断，正在从断点重连。")})

    def put(self, message):
        while not self.closed.is_set():
            try:
                self.messages.put(message, timeout=.2)
                return
            except queue.Full:
                continue

    def send(self, frame):
        if self.closed.is_set(): raise ASRError('转写已取消。', retryable=False)
        # Publish the submitted duration before a fast receiver can return its cue.
        self.duration += len(frame) / 32000
        self.ws.send_binary(frame)

    def finish(self):
        if self.closed.is_set() or self.finish_started is not None: return
        self.finish_started = time.monotonic()
        self.ws.send(json.dumps({'header': {'action': 'finish-task', 'task_id': self.task_id, 'streaming': 'duplex'},
                                 'payload': {'input': {}}}))

    def cancel(self):
        self.closed.set(); self.ws.abort()

    def close(self):
        self.remove_cancel()
        self.cancel(); self.ws.close(); self.receiver.join(timeout=2)


def transcript_events(audio_url, audio_path, vtt_path, checkpoint_path, proxies, stopped):
    from transcription import read_checkpoint, chunk_seconds, atomic_write, to_vtt
    keys = api_keys()
    if not keys:
        from transcription import TranscriptionError
        raise TranscriptionError("字幕服务未配置，请检查设置", retryable=False)
    checkpoint = read_checkpoint(checkpoint_path)
    cues = list(checkpoint['cues']); resume = checkpoint['until']; index = checkpoint['next_chunk']
    pending_sentence = checkpoint.get('pending_sentence')
    # Replace browser state even at zero when migrating old paragraph-level caches.
    yield {'status': 'resumed', 'until': cues[-1]['end'] if pending_sentence and cues else (0 if pending_sentence else resume), 'cues': list(cues)}
    yield {'status': 'audio_source', 'audio_url': '/api/audio?url=' + requests.utils.quote(audio_url, safe='')}
    cancel = threading.Event(); tasks = queue.Queue(maxsize=2); active = []; failure = []
    audio_ended = threading.Event()
    capacity = threading.Semaphore(min(2, len(keys)))
    region = os.environ.get('ALIYUN_REGION', 'beijing')

    class Signal:
        def is_set(self): return stopped.is_set() or cancel.is_set()
        def wait(self, seconds):
            deadline = time.monotonic() + seconds
            while not self.is_set() and time.monotonic() < deadline:
                cancel.wait(min(.1, max(0, deadline - time.monotonic())))
            return self.is_set()
    signal = Signal()

    def send_audio():
        task = None; offset = resume; number = index
        try:
            for frame in pcm_frames(audio_url, audio_path, proxies, signal, resume):
                if signal.is_set(): break
                if task is None:
                    while not capacity.acquire(timeout=.2):
                        if signal.is_set(): return
                    if signal.is_set(): return
                    task = StreamTask(keys[number % len(keys)], offset, number, region, stopped)
                    if signal.is_set():
                        task.close(); return
                    active.append(task); tasks.put(task)
                # A resumed offset may leave less than 100 ms before the next boundary.
                remaining = max(2, round((chunk_seconds(offset) - task.duration) * 32000))
                remaining -= remaining % 2
                head, tail = frame[:remaining], frame[remaining:]
                task.send(head)
                if task.duration >= chunk_seconds(offset) - .001:
                    task.finish(); offset = round(offset + task.duration, 6); number += 1; task = None
                if tail:
                    while not capacity.acquire(timeout=.2):
                        if signal.is_set(): return
                    if signal.is_set(): return
                    task = StreamTask(keys[number % len(keys)], offset, number, region, stopped)
                    if signal.is_set():
                        task.close(); return
                    active.append(task); tasks.put(task); task.send(tail)
                # Small binary frames and bounded tasks prevent uploading an entire episode ahead.
                if signal.wait(.02): break
            if task is not None and not signal.is_set(): task.finish()
            if not signal.is_set(): audio_ended.set()
        except Exception as error:
            failure.append(error if isinstance(error, RuntimeError) else RuntimeError("音频上传中断，请重连。"))
            if task is not None:
                while not signal.is_set():
                    try:
                        task.messages.put({'status': 'error', 'error': failure[-1]}, timeout=.2)
                        break
                    except queue.Full:
                        continue
        finally:
            while not signal.is_set():
                try: tasks.put(None, timeout=.2); break
                except queue.Full: pass

    sender = threading.Thread(target=send_audio, daemon=True); sender.start()
    finishing_sent = False
    try:
        while not signal.is_set():
            try: task = tasks.get(timeout=.2)
            except queue.Empty: continue
            if task is None:
                if failure: raise failure[0]
                break
            while not signal.is_set():
                if audio_ended.is_set() and not finishing_sent:
                    finishing_sent = True
                    yield {'status': 'finishing'}
                try: message = task.messages.get(timeout=.2)
                except queue.Empty: continue
                if message['status'] == 'error': raise message['error']
                if message['status'] == 'finished':
                    until = task.offset + task.duration
                    atomic_write(checkpoint_path, json.dumps({'version': 1, 'until': until,
                        'next_chunk': task.index + 1, 'cues': cues, 'pending_sentence': pending_sentence}, ensure_ascii=False))
                    if pending_sentence is None:
                        yield {'status': 'chunk_ready', 'until': until}
                    task.close(); active.remove(task); capacity.release(); break
                cue = message['cue']
                cue['end'] = min(cue['end'], task.offset + task.duration)
                if cue['end'] > cue['start'] and cue['start'] >= task.offset:
                    pending_sentence = append_fragment(pending_sentence, cue)
                    completed = ends_sentence(pending_sentence['text'])
                    if completed:
                        cue = pending_sentence
                        cues.append(cue)
                        pending_sentence = None
                    atomic_write(checkpoint_path, json.dumps({'version': 1, 'until': cue['end'],
                        'next_chunk': task.index, 'cues': cues, 'pending_sentence': pending_sentence}, ensure_ascii=False))
                    if completed:
                        yield {'status': 'cue', 'cue': cue}
                        yield {'status': 'chunk_ready', 'until': cue['end']}
        if signal.is_set(): return
        if pending_sentence:
            cues.append(pending_sentence)
            atomic_write(checkpoint_path, json.dumps({'version': 1, 'until': pending_sentence['end'],
                'next_chunk': read_checkpoint(checkpoint_path)['next_chunk'], 'cues': cues}, ensure_ascii=False))
            yield {'status': 'cue', 'cue': pending_sentence}
            yield {'status': 'chunk_ready', 'until': pending_sentence['end']}
        if not cues: raise RuntimeError("阿里云未识别到语音，请重试。")
        atomic_write(vtt_path, to_vtt(cues)); checkpoint_path.unlink(missing_ok=True)
        yield {'status': 'done'}
    finally:
        cancel.set()
        for task in list(active): task.close()
        sender.join(timeout=3)
        for task in list(active): task.close()
