#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from codeop import compile_command
from urllib.parse import urlsplit, urlunsplit
import aiohttp
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

WS_PATH = "/api/repl/ws"


class ReplConnection:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def reader(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = msg.json()
                kind = data.get("type")
                if kind == "output":
                    out = sys.stdout if data["stream"] == "stdout" else sys.stderr
                    out.write(data["text"])
                    out.flush()
                elif kind in ("result", "completions", "error"):
                    fut = self._pending.pop(data.get("id"), None)
                    if fut is not None and not fut.done():
                        fut.set_result(data)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("connection closed"))
            self._pending.clear()

    async def _request(self, payload: dict) -> dict:
        msg_id = self._next_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send_json({**payload, "id": msg_id})
        return await fut

    async def execute(self, code: str) -> dict:
        return await self._request({"type": "execute", "code": code})

    async def complete(self, code: str, cursor_pos: int) -> list[dict]:
        result = await self._request(
            {"type": "complete", "code": code, "cursor_pos": cursor_pos}
        )
        return result.get("matches", [])


class ServerCompleter(Completer):
    def __init__(self, conn: ReplConnection) -> None:
        self._conn = conn

    def get_completions(self, document, complete_event):
        return iter(())

    async def get_completions_async(self, document, complete_event):
        try:
            matches = await self._conn.complete(document.text, document.cursor_position)
        except ConnectionError:
            return
        for match in matches:
            yield Completion(
                match["text"],
                start_position=match["start"] - document.cursor_position,
                display_meta=match.get("type") or "",
            )


def build_ws_url(base: str) -> str:
    parts = urlsplit(base)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(
        parts.scheme, "ws"
    )
    path = parts.path.rstrip("/")
    if not path.endswith(WS_PATH):
        path += WS_PATH
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def is_complete(text: str) -> bool:
    if not text.strip():
        return True
    try:
        return compile_command(text, "<repl>", "exec") is not None
    except SyntaxError, ValueError, OverflowError:
        return True


def make_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("enter")
    def _(event) -> None:
        buffer = event.current_buffer
        if is_complete(buffer.text):
            buffer.validate_and_handle()
        else:
            buffer.insert_text("\n")

    return kb


async def run(url: str, token: str, insecure: bool) -> None:
    ws_url = build_ws_url(url)

    async with aiohttp.ClientSession() as session:
        try:
            ws = await session.ws_connect(
                ws_url, verify_ssl=not insecure, max_msg_size=0
            )
        except aiohttp.ClientError as err:
            sys.exit(f"Failed to connect: {err}")

        async with ws:
            await ws.send_json({"type": "auth", "access_token": token})
            try:
                reply = await ws.receive_json()
            except TypeError, ValueError:
                sys.exit("Authentication failed: unexpected server response")
            if reply.get("type") != "auth_ok":
                sys.exit("Authentication failed: check your token")
            conn = ReplConnection(ws)
            reader_task = asyncio.ensure_future(conn.reader())
            prompt = PromptSession(
                completer=ServerCompleter(conn),
                complete_while_typing=False,
                key_bindings=make_key_bindings(),
                multiline=True,
            )
            print(f"Connected to {ws_url}. Ctrl-D to exit, Tab to complete.")
            try:
                while not reader_task.done():
                    try:
                        with patch_stdout():
                            code = await prompt.prompt_async(">>> ")
                    except KeyboardInterrupt:
                        continue
                    except EOFError:
                        break  # Ctrl-D
                    if not code.strip():
                        continue
                    try:
                        result = await conn.execute(code)
                    except ConnectionError:
                        print("Connection closed by server.", file=sys.stderr)
                        break
                    if result.get("error"):
                        print(result["error"], end="", file=sys.stderr)
                    elif result.get("repr") is not None:
                        print(result["repr"])
            finally:
                reader_task.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description="Home Assistant REPL client")
    parser.add_argument(
        "--url",
        default=os.environ.get("HASS_URL"),
        help="Base URL of Home Assistant, e.g. http://homeassistant.local:8123",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HASS_TOKEN"),
        help="Long-lived access token (or set HASS_TOKEN)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (wss only)",
    )
    args = parser.parse_args()
    if not args.url or not args.token:
        parser.error("both --url/HASS_URL and --token/HASS_TOKEN are required")

    try:
        asyncio.run(run(args.url, args.token, args.insecure))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
