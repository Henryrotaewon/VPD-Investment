"""Bounded, content-free latency records; no URLs, payloads or exception text."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from contextlib import contextmanager
from functools import wraps
import json
import os
import re
import time
import uuid

_inherited = os.getenv('MAGI_LATENCY_TRACE', '')
trace = ContextVar('latency_trace', default=_inherited if re.fullmatch(r'[a-f0-9]{12}', _inherited) else 'background')


def emit(stage, elapsed_ms, outcome='ok', **numbers):
    record = dict(stage=stage, trace=trace.get(), elapsed_ms=round(elapsed_ms, 2), outcome=outcome)
    record.update({k: round(v, 2) for k, v in numbers.items() if isinstance(v, (int, float))})
    print('latency ' + json.dumps(record, separators=(',', ':')), flush=True)


@contextmanager
def span(stage):
    started = time.monotonic()
    outcome = 'ok'
    try:
        yield
    except BaseException:
        outcome = 'error'
        raise
    finally:
        emit(stage, (time.monotonic() - started) * 1000, outcome)


def timed(stage, new_trace=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            token = trace.set(uuid.uuid4().hex[:12]) if new_trace else None
            try:
                with span(stage):
                    return fn(*args, **kwargs)
            finally:
                if token is not None:
                    trace.reset(token)
        return wrapped
    return decorate


class TimedExecutor(ThreadPoolExecutor):
    def submit(self, fn, /, *args, **kwargs):
        queued = time.monotonic()
        context = copy_context()
        stage = self._thread_name_prefix
        def execute():
            emit(stage + '.queue', (time.monotonic() - queued) * 1000)
            with span(stage + '.run'):
                return fn(*args, **kwargs)
        future = super().submit(context.run, execute)
        future.latency_trace = trace.get()
        return future
