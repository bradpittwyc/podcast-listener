"""Explicit cancellation shared by full-episode and single-sentence jobs."""
import re
import threading
import time
import uuid
from collections import OrderedDict


class StopSignal(threading.Event):
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._callbacks = set()

    def on_cancel(self, callback):
        with self._lock:
            if not self.is_set():
                self._callbacks.add(callback)
                return lambda: self.remove(callback)
        callback()
        return lambda: None

    def remove(self, callback):
        with self._lock:
            self._callbacks.discard(callback)

    def set(self):
        with self._lock:
            super().set()
            callbacks, self._callbacks = self._callbacks, set()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass


class TranscriptionJobs:
    def __init__(self):
        self._lock = threading.Lock()
        self.active = {}
        self._cancelled = OrderedDict()

    @staticmethod
    def token(value):
        if not value:
            return uuid.uuid4().hex
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
            raise ValueError('Invalid transcription request ID')
        return value

    def register(self, token):
        signal = StopSignal()
        with self._lock:
            previous = self.active.get(token)
            cancelled = self._cancelled.get(token, 0) > time.monotonic() - 300
            self.active[token] = signal
        if previous:
            previous.set()
        # Cancellation can arrive before the browser's GET reaches the server.
        if cancelled:
            signal.set()
        return signal

    def cancel(self, token):
        with self._lock:
            self._cancelled[token] = time.monotonic()
            self._cancelled.move_to_end(token)
            while len(self._cancelled) > 256:
                self._cancelled.popitem(last=False)
            signal = self.active.get(token)
        if signal:
            signal.set()

    def release(self, token, signal):
        signal.set()
        with self._lock:
            if self.active.get(token) is signal:
                del self.active[token]
