"""Gemini 3.5 file transcription with real word timestamps and resumable chunks."""
import base64
import io
import json
import os
import queue
import re
import subprocess
import threading
import time
import wave
import concurrent.futures

import requests

MODEL = 'gemini-3.5-transcribe'
CACHE_SUFFIX = '-gemini-3.5-transcribe-words-v1'
ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/interactions'
SEGMENT_SECONDS = 20 * 60
_pool_lock = threading.Lock()
_key_locks = {}
_quota_deadlines = {}


class ASRError(RuntimeError):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def api_key():
    keys = api_keys()
    return keys[0] if keys else ''


def api_keys():
    keys = [os.environ.get(f'GEMINI_TRANSCRIPTION_KEY_{index}', '').strip() for index in range(1, 6)]
    return list(dict.fromkeys(key for key in keys if key)) or ([os.environ['GEMINI_API_KEY'].strip()] if os.environ.get('GEMINI_API_KEY', '').strip() else [])


def wav_audio(pcm):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(pcm)
    return output.getvalue()


def payload(pcm):
    return {'model': MODEL, 'store': False,
            'input': [{'type': 'audio', 'mime_type': 'audio/wav',
                       'data': base64.b64encode(wav_audio(pcm)).decode('ascii')}],
            'generation_config': {'transcription_config': {
                'mode': {'type': 'verbatim', 'timestamp_granularities': ['word']}}}}


def rate_limit_error(response):
    message = ''
    try:
        body = response.json()
        error = body.get('error', {}) if isinstance(body, dict) else {}
        message = error.get('message', '') if isinstance(error, dict) else ''
        if not isinstance(message, str):
            message = ''
    except (ValueError, TypeError):
        pass
    wait = ''
    retry_seconds = 60
    try:
        retry_seconds = int(response.headers.get('Retry-After', '60'))
        if 0 < retry_seconds <= 7 * 86400:
            minutes = (retry_seconds + 59) // 60
            hours, minutes = divmod(minutes, 60)
            wait = f'约 {hours} 小时 {minutes} 分钟后恢复。' if hours else f'约 {minutes} 分钟后恢复。'
    except (ValueError, TypeError, AttributeError):
        pass
    daily = re.search(r'limit:\s*(\d+) requests per day on Free Tier', message)
    if daily:
        detail = f'Gemini 免费转写每日额度已用完（{daily.group(1)} 次/天）。'
    else:
        detail = 'Gemini 转写达到调用额度或速率限制。'
    error = ASRError(detail + wait + '已有字幕和断点已保留，请额度恢复后点击继续。', False)
    error.rate_limited = True
    error.retry_after = max(1, min(retry_seconds, 7 * 86400))
    return error


def transcribe_with_keys(pcm, proxies, stopped, keys, preferred):
    failure = None
    while not stopped.is_set():
        available = False
        for step in range(len(keys)):
            key = keys[(preferred + step) % len(keys)]
            with _pool_lock:
                if _quota_deadlines.get(key, 0) > time.monotonic():
                    continue
                lock = _key_locks.setdefault(key, threading.Lock())
            available = True
            if not lock.acquire(timeout=.05):
                continue
            try:
                if stopped.is_set():
                    raise ASRError('转写已取消。', False)
                with _pool_lock:
                    if _quota_deadlines.get(key, 0) > time.monotonic():
                        continue
                try:
                    return request_transcription(pcm, proxies, stopped, key=key)
                except ASRError as error:
                    if not getattr(error, 'rate_limited', False):
                        raise
                    failure = error
                    with _pool_lock:
                        _quota_deadlines[key] = time.monotonic() + error.retry_after
            finally:
                lock.release()
        if not available:
            raise failure or ASRError('所有 Gemini 转写 Key 暂时达到额度限制，已有字幕和断点已保留，请额度恢复后继续。', False)
        stopped.wait(.1)
    raise ASRError('转写已取消。', False)


def request_transcription(pcm, proxies, stopped, key=None):
    """Return promptly on cancellation; an in-flight HTTP call has a bounded timeout."""
    if stopped.is_set():
        raise ASRError('转写已取消。', False)
    key = key or api_key()
    if not key:
        raise ASRError('请在设置中填写 Gemini API Key。', False)
    result = queue.Queue(maxsize=1)
    session = requests.Session()
    remove = stopped.on_cancel(session.close) if hasattr(stopped, 'on_cancel') else lambda: None

    def run():
        uploaded = None
        try:
            if stopped.is_set():
                return
            if len(pcm) > 18 * 1024 * 1024:
                audio = wav_audio(pcm)
                with session.post('https://generativelanguage.googleapis.com/upload/v1beta/files',
                        headers={'x-goog-api-key': key, 'X-Goog-Upload-Protocol': 'resumable',
                                 'X-Goog-Upload-Command': 'start', 'X-Goog-Upload-Header-Content-Length': str(len(audio)),
                                 'X-Goog-Upload-Header-Content-Type': 'audio/wav'},
                        json={'file': {'display_name': 'Podcast transcription segment'}},
                        proxies=proxies, timeout=(10, 60)) as response:
                    if not response.ok:
                        raise rate_limit_error(response) if response.status_code == 429 else ASRError(f'Gemini 音频上传返回 HTTP {response.status_code}。', response.status_code >= 500)
                    upload_url = response.headers.get('X-Goog-Upload-URL', '')
                    upload_chunk = int(response.headers.get('X-Goog-Upload-Chunk-Granularity', 8 * 1024 * 1024))
                if not upload_url.startswith('https://generativelanguage.googleapis.com/'):
                    raise ASRError('Gemini 未返回有效的音频上传地址。')
                if stopped.is_set():
                    return
                # Resumable chunks avoid a single 38 MB request being reset by
                # a network intermediary and keep cancellation responsive.
                if not 0 < upload_chunk <= 64 * 1024 * 1024:
                    raise ASRError('Gemini 音频上传分块大小无效。')
                for position in range(0, len(audio), upload_chunk):
                    if stopped.is_set():
                        return
                    part = audio[position:position + upload_chunk]
                    final = position + len(part) == len(audio)
                    with session.post(upload_url, headers={'X-Goog-Upload-Offset': str(position),
                            'X-Goog-Upload-Command': 'upload, finalize' if final else 'upload',
                            'Content-Type': 'audio/wav'}, data=part, proxies=proxies, timeout=(10, 60)) as response:
                        if not response.ok:
                            raise rate_limit_error(response) if response.status_code == 429 else ASRError(f'Gemini 音频上传返回 HTTP {response.status_code}。', response.status_code >= 500)
                        if final:
                            uploaded = response.json()['file']
                body = {'model': MODEL, 'store': False,
                        'input': [{'type': 'audio', 'uri': uploaded['uri'], 'mime_type': 'audio/wav'}],
                        'generation_config': {'transcription_config': {
                            'mode': {'type': 'verbatim', 'timestamp_granularities': ['word']}}}}
            else:
                body = payload(pcm)
            if stopped.is_set():
                return
            with session.post(ENDPOINT, headers={'x-goog-api-key': key}, json=body,
                              proxies=proxies, timeout=(10, 300)) as response:
                if not response.ok:
                    status = response.status_code
                    if status == 429:
                        error = rate_limit_error(response)
                    else:
                        error = ASRError(f'Gemini 转写返回 HTTP {status}，请检查密钥、模型权限或网络。', status >= 500)
                    result.put(error)
                    return
                result.put(response.json())
        except ASRError as error:
            result.put(error)
        except Exception:
            result.put(ASRError('Gemini 转写请求失败或超时，请检查网络或代理后重试。'))
        finally:
            if uploaded and uploaded.get('name', '').startswith('files/'):
                try:
                    session.delete('https://generativelanguage.googleapis.com/v1beta/' + uploaded['name'],
                                   headers={'x-goog-api-key': key}, proxies=proxies, timeout=(5, 10)).close()
                except Exception:
                    pass
            session.close()

    threading.Thread(target=run, daemon=True).start()
    deadline = time.monotonic() + 600
    try:
        while not stopped.is_set():
            if time.monotonic() >= deadline:
                raise ASRError('Gemini 单段转写超时，请点击继续。')
            try:
                response = result.get(timeout=.2)
            except queue.Empty:
                continue
            if isinstance(response, Exception):
                raise response
            return response
        raise ASRError('转写已取消。', False)
    finally:
        remove()
        session.close()


def seconds(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+)?s', value):
        raise ASRError('Gemini 返回了无效的词时间戳，请重试。')
    return float(value[:-1])


def words_from_response(response, offset, duration):
    if not isinstance(response, dict) or response.get('status') != 'completed':
        raise ASRError('Gemini 未完成本段转写，请重试。')
    words = []
    has_text = False
    try:
        for step in response.get('steps', []):
            if step.get('type') != 'model_output':
                continue
            for content in step.get('content', []):
                if content.get('type') != 'text':
                    continue
                has_text = has_text or bool(content.get('text', '').strip())
                for annotation in content.get('annotations', []):
                    if annotation.get('type') != 'word_info':
                        continue
                    text = annotation['text']
                    start, end = seconds(annotation['start_offset']), seconds(annotation['end_offset'])
                    if not isinstance(text, str) or not text.strip() or not 0 <= start <= end <= duration + .1 or start > duration:
                        raise ASRError('Gemini 返回了无效的词时间戳，请重试。')
                    words.append({'start': offset + start, 'end': offset + min(duration, end), 'text': text.strip()})
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ASRError('Gemini 转写响应格式异常，请重试。') from None
    if has_text and not words:
        # Never invent subtitle times from a transcript without annotations.
        raise ASRError('Gemini 未返回词时间戳，无法同步字幕，请重试。')
    # Gemini quantizes short words to the same start/end instant. Keep their
    # text and attach them to a timed neighbour using only returned endpoints.
    # Annotation order follows transcript text, which can place a speaker's
    # interjection after a longer overlapping turn. Subtitle order is temporal.
    words.sort(key=lambda word: word['start'])
    aligned = []
    pending = None
    for word in words:
        if pending is not None:
            word = {'start': pending['start'], 'end': word['end'],
                    'text': join_text(pending['text'], word['text'])}
        if word['end'] > word['start']:
            aligned.append(word)
            pending = None
        else:
            pending = word
    if pending:
        if not aligned:
            raise ASRError('Gemini 未返回可用的字幕时间范围，请重试。')
        aligned[-1]['text'] = join_text(aligned[-1]['text'], pending['text'])
        aligned[-1]['end'] = max(aligned[-1]['end'], pending['end'])
    return aligned


def join_text(previous, following):
    # Preserve punctuation and avoid inserting spaces between Chinese characters.
    if re.match(r'^[,.;:!?，。！？、）\]\}"\u201d\u2019]', following):
        separator = ''
    elif re.search(r'[\u3400-\u9fff]$', previous) and re.match(r'[\u3400-\u9fff]', following):
        separator = ''
    else:
        separator = ' '
    return previous + separator + following


def sentences(words, pending=None):
    ready = []
    for word in words:
        if pending and word['start'] - pending['end'] > 1.2:
            ready.append(pending)
            pending = None
        pending = (dict(word) if pending is None else
                   {'start': pending['start'], 'end': max(pending['end'], word['end']), 'text': join_text(pending['text'], word['text'])})
        if re.search(r'[.!?。！？]["\u201d\u2019)\]]*$', pending['text']) or pending['end'] - pending['start'] >= 15:
            ready.append(pending)
            pending = None
    return ready, pending


def audio_duration(audio_path):
    command = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(audio_path)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30, check=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        duration = float(json.loads(result.stdout)['format']['duration'])
        if not 0 < duration < 86400:
            raise ValueError()
        return duration
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise ASRError('无法读取节目音频时长，请检查音频后重试。', False) from None


def decode_segment(audio_path, start, end, stopped):
    if stopped.is_set():
        raise ASRError('转写已取消。', False)
    command = ['ffmpeg', '-nostdin', '-loglevel', 'error', '-ss', str(start), '-i', str(audio_path),
               '-t', str(end - start), '-map', '0:a:0', '-vn', '-ar', '16000', '-ac', '1', '-f', 's16le', 'pipe:1']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    remove = stopped.on_cancel(process.kill) if hasattr(stopped, 'on_cancel') else lambda: None
    try:
        pcm, _ = process.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise ASRError('节目片段解码超时，请重试。') from None
    finally:
        remove()
    if stopped.is_set():
        raise ASRError('转写已取消。', False)
    if process.returncode or not pcm:
        raise ASRError('节目片段解码失败，请重试。')
    return pcm


def segment_ranges(resume, duration):
    start = resume
    while start < duration - .001:
        index = int(start // SEGMENT_SECONDS)
        end = min(duration, (index + 1) * SEGMENT_SECONDS)
        yield index, start, end
        start = end


def transcript_events(audio_url, audio_path, vtt_path, checkpoint_path, proxies, stopped):
    from transcription import atomic_write, read_checkpoint, to_vtt, download_audio
    from transcription_jobs import StopSignal
    keys = api_keys()
    if not keys:
        raise ASRError('请配置 Gemini 转写 Key。', False)
    state = read_checkpoint(checkpoint_path)
    offset, index, cues = state['until'], state['next_chunk'], state['cues']
    pending = state.get('pending_sentence')
    coverage = (cues[-1]['end'] if cues else 0) if pending else offset
    yield {'status': 'resumed', 'until': coverage, 'cues': list(cues)}
    signal = StopSignal()
    remove = stopped.on_cancel(signal.set) if hasattr(stopped, 'on_cancel') else lambda: None
    messages = queue.Queue(maxsize=32)

    def send(item):
        while not signal.is_set() and not stopped.is_set():
            try:
                messages.put(item, timeout=.2)
                return
            except queue.Full:
                pass

    def save():
        atomic_write(checkpoint_path, json.dumps({'version': 1, 'until': offset,
                     'next_chunk': index, 'cues': cues, 'pending_sentence': pending}, ensure_ascii=False))

    def download():
        try:
            last = time.monotonic()
            downloaded = 0
            def on_data(data):
                nonlocal last, downloaded
                downloaded += len(data)
                if time.monotonic() - last >= 5:
                    last = time.monotonic()
                    send({'status': 'download_progress', 'bytes': downloaded})
            if not download_audio(audio_url, audio_path, proxies, signal, on_data):
                send(ASRError('音频下载已取消。', False))
        except Exception:
            send(ASRError('节目音频下载失败，请检查网络后重试。'))
        finally:
            send(None)

    def wait_events(future):
        while not future.done():
            if stopped.is_set() or signal.is_set():
                signal.set()
                return
            try:
                item = messages.get(timeout=.2)
                if isinstance(item, dict):
                    yield item
            except queue.Empty:
                pass

    def process_segment(number, start, end):
        path = checkpoint_path.with_name(checkpoint_path.stem + f'.segment-{start:.6f}-{end:.6f}.json')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('start') == start and data.get('end') == end and isinstance(data.get('words'), list):
                words = data['words']
                if all(isinstance(w.get('text'), str) and start <= w['start'] < w['end'] <= end + .1 for w in words):
                    return words, path
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        send({'status': 'progress', 'detail': f'正在转写第 {number + 1} 段（每段 20 分钟）', 'timeout_seconds': 780})
        pcm = decode_segment(audio_path, start, end, signal)
        result = transcribe_with_keys(pcm, proxies, signal, keys, number % len(keys))
        try:
            words = words_from_response(result, start, len(pcm) / 32000)
        except ASRError:
            atomic_write(path.with_suffix('.response.json'), json.dumps(result, ensure_ascii=False))
            raise
        if signal.is_set():
            raise ASRError('转写已取消。', False)
        atomic_write(path, json.dumps({'start': start, 'end': end, 'words': words}, ensure_ascii=False))
        return words, path

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(keys)))
    try:
        if not audio_path.exists() or not audio_path.stat().st_size:
            yield {'status': 'progress', 'detail': '正在下载节目音频，随后同时转写 20 分钟片段'}
            downloader = executor.submit(download)
            while not downloader.done() or not messages.empty():
                if stopped.is_set():
                    return
                try:
                    item = messages.get(timeout=.2)
                except queue.Empty:
                    continue
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, dict):
                    yield item
            if not audio_path.exists():
                raise ASRError('节目音频下载未完成，请重试。')
        yield {'status': 'audio_ready', 'local_audio': f'/cache/{audio_path.name}'}
        duration = audio_duration(audio_path)
        ranges = list(segment_ranges(offset, duration))
        parallel = min(5, len(keys))
        for batch_start in range(0, len(ranges), parallel):
            batch = ranges[batch_start:batch_start + parallel]
            futures = [executor.submit(process_segment, *segment) for segment in batch]
            failure = None
            for segment, future in zip(batch, futures):
                yield from wait_events(future)
                if stopped.is_set() or signal.is_set():
                    return
                try:
                    words, path = future.result()
                except Exception as error:
                    failure = failure or error
                    continue
                if failure is not None:
                    continue  # Later segments remain saved for the next resume.
                ready, pending = sentences(words, pending)
                cues.extend(ready)
                offset, index = segment[2], segment[0] + 1
                save()
                path.unlink(missing_ok=True)
                for cue in ready:
                    yield {'status': 'cue', 'cue': cue}
                yield {'status': 'chunk_ready', 'until': (cues[-1]['end'] if cues else 0) if pending else offset}
            if failure is not None:
                raise failure
        if stopped.is_set():
            return
        yield {'status': 'finishing'}
        if pending:
            cues.append(pending)
            final = pending
            pending = None
            save()
            yield {'status': 'cue', 'cue': final}
        if not cues:
            raise ASRError('Gemini 未识别到语音，请检查音频后重试。', False)
        atomic_write(vtt_path, to_vtt(cues))
        checkpoint_path.unlink(missing_ok=True)
        yield {'status': 'done'}
    finally:
        signal.set()
        remove()
        executor.shutdown(wait=False, cancel_futures=True)


def regenerate(audio_url, audio_path, start, end, stopped=None, proxies=None):
    stopped = stopped if stopped is not None else threading.Event()
    if stopped.is_set():
        raise ASRError('转写已取消。', False)
    if not api_key():
        raise ASRError('请在设置中填写 Gemini API Key。', False)
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
        process.kill()
        process.communicate()
        raise ASRError('读取单句音频超时，请重试。') from None
    finally:
        remove()
    if stopped.is_set():
        raise ASRError('转写已取消。', False)
    if process.returncode or not pcm:
        raise ASRError('无法读取这句音频，请检查音频源后重试。')
    keys = api_keys()
    words = words_from_response(transcribe_with_keys(pcm, proxies, stopped, keys, int(start // SEGMENT_SECONDS) % len(keys)), begin, len(pcm) / 32000)
    selected = [word for word in words if start <= (word['start'] + word['end']) / 2 <= end]
    if not selected:
        raise ASRError('这一句未识别到语音，原字幕已保留。')
    text = selected[0]['text']
    for word in selected[1:]:
        text = join_text(text, word['text'])
    return {'start': max(start, selected[0]['start']), 'end': min(end, selected[-1]['end']),
            'text': text, 'source_start': start, 'source_end': end}
