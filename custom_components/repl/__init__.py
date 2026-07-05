from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp.web import Request, Response, WebSocketResponse, WSMsgType
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from . import stream
from .session import ReplSession

_LOGGER = logging.getLogger(__name__)

DOMAIN = "repl"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


class ReplPageView(HomeAssistantView):
    requires_auth = False
    url = "/repl"
    name = "repl:page"

    def __init__(self, html: str) -> None:
        self._html = html

    async def get(self, request: Request) -> Response:
        return Response(text=self._html, content_type="text/html")


class ReplView(HomeAssistantView):
    requires_auth = False
    url = "/api/repl/ws"
    name = "api:repl:ws"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def _authenticate(self, ws: WebSocketResponse) -> bool:
        try:
            msg = await ws.receive()
        except TimeoutError:
            return False
        if msg.type is not WSMsgType.TEXT:
            return False
        try:
            data = msg.json()
        except ValueError:
            data = {}
        token = data.get("access_token")
        if (
            data.get("type") == "auth"
            and token
            and self._hass.auth.async_validate_access_token(token) is not None
        ):
            await ws.send_json({"type": "auth_ok"})
            return True
        await ws.send_json({"type": "auth_invalid"})
        return False

    async def get(self, request: Request) -> WebSocketResponse:
        ws = WebSocketResponse()
        await ws.prepare(request)

        if not await self._authenticate(ws):
            await ws.close()
            return ws

        session = ReplSession(self._hass)

        queue: asyncio.Queue = asyncio.Queue()

        async def drain() -> None:
            while (frame := await queue.get()) is not None:
                await ws.send_json(frame)

        drainer = asyncio.ensure_future(drain())

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = msg.json()
                except ValueError:
                    continue

                msg_id = data.get("id")
                msg_type = data.get("type")
                try:
                    if msg_type == "execute":
                        await self._execute(
                            session, queue, msg_id, data.get("code", "")
                        )
                    elif msg_type == "complete":
                        self._complete(
                            session,
                            queue,
                            msg_id,
                            data.get("code", ""),
                            data.get("cursor_pos", 0),
                        )
                    elif msg_type == "inspect":
                        queue.put_nowait(
                            {
                                "type": "inspect_result",
                                "id": msg_id,
                                "info": session.inspect(data.get("expr", "")),
                            }
                        )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.exception("REPL request failed")
                    queue.put_nowait(
                        {"type": "error", "id": msg_id, "message": str(err)}
                    )
        finally:
            queue.put_nowait(None)
            await drainer

        return ws

    async def _execute(self, session, queue, msg_id, code: str) -> None:
        def on_output(name: str, text: str) -> None:
            queue.put_nowait(
                {"type": "output", "id": msg_id, "stream": name, "text": text}
            )

        token = stream.capture(on_output)
        try:
            result = await session.execute(code)
        finally:
            stream.reset(token)

        queue.put_nowait(
            {
                "type": "result",
                "id": msg_id,
                "ok": result.ok,
                "repr": result.repr,
                "error": result.error,
            }
        )

    def _complete(self, session, queue, msg_id, code: str, cursor_pos: int) -> None:
        matches = session.complete(code, cursor_pos)
        queue.put_nowait(
            {
                "type": "completions",
                "id": msg_id,
                "matches": [
                    {
                        "text": c.text,
                        "start": c.start,
                        "end": c.end,
                        "type": c.type,
                        "signature": c.signature,
                    }
                    for c in matches
                ],
            }
        )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    stream.install()
    html = await hass.async_add_executor_job(
        (Path(__file__).parent / "index.html").read_text
    )
    hass.http.register_view(ReplView(hass))
    hass.http.register_view(ReplPageView(html))
    return True
