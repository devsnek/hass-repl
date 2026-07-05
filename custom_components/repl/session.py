from __future__ import annotations

import ast
import contextlib
import inspect
import io
from typing import cast
import sys
import traceback
from dataclasses import dataclass

from homeassistant.core import HomeAssistant

_FILENAME = "<repl>"
_COMPILE_FLAGS = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT

_DISABLED_MATCHERS = [
    "IPCompleter.magic_matcher",
    "IPCompleter.magic_config_matcher",
    "IPCompleter.magic_color_matcher",
    "IPCompleter.custom_completer_matcher",
]


@dataclass(slots=True)
class Completion:
    text: str
    start: int
    end: int
    type: str
    signature: str


@dataclass(slots=True)
class ExecResult:
    ok: bool
    repr: str | None
    error: str | None


class ReplSession:
    def __init__(self, hass: HomeAssistant) -> None:
        self.namespace: dict = {"__name__": "__repl__", "hass": hass}
        self._completer = self._make_completer(self.namespace)

    @staticmethod
    def _make_completer(namespace: dict):
        from IPython.core.completer import IPCompleter
        from traitlets.config import Config

        config = Config()
        config.IPCompleter.disable_matchers = _DISABLED_MATCHERS
        return IPCompleter(namespace=namespace, config=config)

    async def execute(self, code: str) -> ExecResult:
        try:
            module, result_expr = self._split(code)
        except SyntaxError:
            exc = sys.exc_info()[1]
            s = "".join(traceback.format_exception_only(type(exc), exc))
            return ExecResult(False, None, s)

        value = None
        try:
            if module.body:
                compiled = compile(module, _FILENAME, "exec", flags=_COMPILE_FLAGS)
                result = eval(compiled, self.namespace)
                if inspect.iscoroutine(result):
                    # if compiled.co_flags & inspect.CO_COROUTINE:
                    await result
            if result_expr is not None:
                compiled = compile(result_expr, _FILENAME, "eval", flags=_COMPILE_FLAGS)
                value = eval(compiled, self.namespace)
                if inspect.iscoroutine(value):
                    value = await value
        except BaseException:  # noqa: BLE001
            exc = sys.exc_info()[1]
            assert exc
            tb = exc.__traceback__
            while tb is not None and tb.tb_frame.f_code.co_filename == __file__:
                tb = tb.tb_next
            s = "".join(traceback.format_exception(type(exc), exc, tb))
            return ExecResult(False, None, s)

        return ExecResult(True, self._repr(value), None)

    @staticmethod
    def _split(code: str) -> tuple[ast.Module, ast.Expression | None]:
        module = ast.parse(code, _FILENAME, "exec")
        result_expr = None
        if module.body and isinstance(module.body[-1], ast.Expr):
            last = cast(ast.Expr, module.body.pop())
            result_expr = ast.copy_location(ast.Expression(last.value), last)
        return module, result_expr

    @staticmethod
    def _repr(value: object) -> str | None:
        if value is None:
            return None
        try:
            from IPython.lib.pretty import pretty

            return pretty(value)
        except Exception:  # noqa: BLE001
            return repr(value)

    def complete(self, code: str, cursor_pos: int) -> list[Completion]:
        from IPython.core.completer import provisionalcompleter

        with contextlib.redirect_stderr(io.StringIO()), provisionalcompleter():
            raw = list(self._completer.completions(code, cursor_pos))
        return [
            Completion(c.text, c.start, c.end, c.type or "", c.signature or "")
            for c in raw
        ]
