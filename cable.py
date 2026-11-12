"""Sabha's app event stream: one Action Cable subscription to ``AppEventsChannel``.

Sabha's WebSocket is AnyCable speaking the Action Cable JSON protocol. The server greets with
``welcome``, pings every few seconds, confirms the subscription, then sends each event as
``{"identifier", "message"}``. A ``disconnect`` frame with ``reconnect: false`` means the key was
refused. The extended protocol stamps each event with its stream position, and a ``history``
request after subscribing replays what was missed (see ``replay.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

if TYPE_CHECKING:
    from .replay import Replay

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
                 on_subscribed: Callable[[], Awaitable[None]] | None = None, replay: Optional[Replay] = None):
        self._session, self._url, self._key = session, socket_url(url), key
        self._on_event, self._on_subscribed, self._replay = on_event, on_subscribed, replay
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
                protocols=("actioncable-v1-ext-json",), heartbeat=None, autoping=True)
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
                    history = self._replay.history() if self._replay else None
                    if self._replay:
                        self._replay.subscribed()
                    if history:
                        await self._ws.send_json({"command": "history", "identifier": IDENTIFIER, "history": history})
                    if self._on_subscribed:
                        await self._on_subscribed()
                elif kind == "confirm_history":
                    logger.info("Sabha: replaying missed events")
                elif kind == "reject_history":
                    logger.warning("Sabha: couldn't replay missed events (history expired, or Sabha restarted)")
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
                    # Handled, or failed past retrying: either way, a replay shouldn't bring it back.
                    if self._replay:
                        self._replay.handled(msg.get("stream_id"), msg.get("epoch"), msg.get("offset"))
            return subscribed
        finally:
            if subscribed and self._replay:
                self._replay.disconnected()
            if not self._ws.closed:
                await self._ws.close()
