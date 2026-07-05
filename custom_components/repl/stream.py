from __future__ import annotations

import sys
from collections.abc import Callable
from contextvars import ContextVar
from typing import TextIO

OutputSink = Callable[[str, str], None]

_SINK: ContextVar[OutputSink | None] = ContextVar("repl_output_sink", default=None)


class _StreamProxy:
    def __init__(self, name: str, real: TextIO) -> None:
        self._name = name
        self._real = real

    def write(self, data: str) -> int:
        sink = _SINK.get()
        if sink is None:
            return self._real.write(data)
        if data:
            sink(self._name, data)
        return len(data)

    def flush(self) -> None:
        if _SINK.get() is None:
            self._real.flush()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def install() -> None:
    if not isinstance(sys.stdout, _StreamProxy):
        sys.stdout = _StreamProxy("stdout", sys.stdout)
    if not isinstance(sys.stderr, _StreamProxy):
        sys.stderr = _StreamProxy("stderr", sys.stderr)


def capture(sink: OutputSink):
    return _SINK.set(sink)


def reset(token) -> None:
    _SINK.reset(token)
