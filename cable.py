"""Sabha's app event stream: one Action Cable subscription to ``AppEventsChannel``.

Sabha's WebSocket is AnyCable speaking the Action Cable JSON protocol. The server greets with
``welcome``, pings every few seconds, confirms the subscription, then sends each event as
``{"identifier", "message"}``. A ``disconnect`` frame with ``reconnect: false`` means the key was
refused. There is no replay: events sent while the socket is down are gone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

logger = logging.getLogger(__name__)

IDENTIFIER = json.dumps({"channel": "AppEventsChannel"})

# Unanswered for this long and the socket is treated as dead, though TCP may not know yet.
_SILENCE_TIMEOUT = 20.0
_BACKOFF_BASE, _BACKOFF_MAX = 1.0, 60.0


class KeyRefused(Exception):
    """Sabha refused the app key; reconnecting won't help."""


def socket_url(url: str) -> str:
    """The app's page shows the URL with ``?bot_key=…`` for clients that can't set headers. This
    client sends the key as a header, so drop it from the URL and keep it out of proxy logs."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "bot_key"]
    return urlunsplit(parts._replace(query=urlencode(query)))


class EventStream:
    def __init__(self, session: aiohttp.ClientSession, url: str, key: str,
                 on_event: Callable[[dict[str, Any]], Awaitable[None]],
                 on_subscribed: Callable[[], Awaitable[None]] | None = None):
        self._session, self._url, self._key = session, socket_url(url), key
        self._on_event, self._on_subscribed = on_event, on_subscribed
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._closing = False

    async def run(self) -> None:
        """Hold the subscription open until ``close()``, reconnecting with backoff. Raises
        ``KeyRefused`` when Sabha rejects the key."""
        delay = _BACKOFF_BASE
        while not self._closing:
            try:
                subscribed = await self._session_once()
                if subscribed:
                    delay = _BACKOFF_BASE
            except KeyRefused:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Sabha: event stream dropped (%s)", exc)
            if self._closing:
                return
            wait = delay * (1 + 0.2 * random.random())
            logger.info("Sabha: reconnecting to event stream in %.1fs", wait)
            await asyncio.sleep(wait)
            delay = min(delay * 2, _BACKOFF_MAX)

    async def answer(self, event_id: str, **fields: Any) -> None:
        """Answer a ``command`` event inline, as Slack's Socket Mode acknowledgement does."""
        if self._ws is None or self._ws.closed:
            raise ConnectionError("Sabha event stream is not connected")
        data = json.dumps({"action": "answer", "id": event_id, **fields})
        await self._ws.send_json({"command": "message", "identifier": IDENTIFIER, "data": data})

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def _session_once(self) -> bool:
        subscribed = False
        try:
            self._ws = await self._session.ws_connect(
                self._url, headers={"Authorization": f"Bearer {self._key}"},
                protocols=("actioncable-v1-json",), heartbeat=None, autoping=True)
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in (401, 403):
                raise KeyRefused(f"WebSocket handshake refused (HTTP {exc.status})") from exc
            raise
        try:
            while not self._closing:
                try:
                    frame = await self._ws.receive(timeout=_SILENCE_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("Sabha: no ping for %.0fs, reconnecting", _SILENCE_TIMEOUT)
                    return subscribed
                if frame.type != aiohttp.WSMsgType.TEXT:
                    if frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                      aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        return subscribed
                    continue
                try:
                    msg = json.loads(frame.data)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "ping":
                    continue
                if kind == "welcome":
                    await self._ws.send_json({"command": "subscribe", "identifier": IDENTIFIER})
                elif kind == "confirm_subscription":
                    subscribed = True
                    logger.info("Sabha: subscribed to app events")
                    if self._on_subscribed:
                        await self._on_subscribed()
                elif kind == "reject_subscription":
                    raise KeyRefused("AppEventsChannel refused the subscription: the key isn't an app's")
                elif kind == "disconnect":
                    if msg.get("reconnect") is False:
                        raise KeyRefused(f"Sabha closed the socket: {msg.get('reason') or 'unauthorized'}")
                    return subscribed
                elif kind is None and isinstance(msg.get("message"), dict):
                    try:
                        await self._on_event(msg["message"])
                    except Exception:
                        logger.exception("Sabha: event handler failed")
            return subscribed
        finally:
            if not self._ws.closed:
                await self._ws.close()
