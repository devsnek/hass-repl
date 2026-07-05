from __future__ import annotations

import asyncio
import logging

from aiohttp.web import Request, WebSocketResponse, WSMsgType
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from . import stream
from .session import ReplSession

_LOGGER = logging.getLogger(__name__)

DOMAIN = "repl"


class ReplView(HomeAssistantView):
    requires_auth = True
    url = "/api/repl/ws"
    name = "api:repl:ws"

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: Request) -> WebSocketResponse:
        ws = WebSocketResponse()
        await ws.prepare(request)

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
    hass.http.register_view(ReplView(hass))
    return True
