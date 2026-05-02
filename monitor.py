"""
Sabha ActionCable WebSocket monitor.

Connects to ``{websocket_url}/cable?bot_key=…&wid=…``, subscribes to
``BotEventsChannel``, parses each inbound frame into a webhook payload,
filters self-echoes + duplicates, and dispatches to a callback.

Lifecycle: exponential backoff with jitter on disconnect, unbounded
retries on transient errors, stop on auth failures (401/403). Honors
``confirm_subscription`` before dispatching anything — drops in-flight
messages that arrive before the server confirms our subscribe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import aiohttp

from .dedup import DedupCache
from .webhook import (
    build_dedup_key,
    is_self_echo,
    parse_webhook_payload,
    WebhookParseError,
)

logger = logging.getLogger(__name__)


_BOT_EVENTS_IDENT = json.dumps({"channel": "BotEventsChannel"})
_DEDUP_TTL_MS = 5 * 60_000
_DEDUP_MAX_SIZE = 2000
_BACKOFF_BASE_MS = 500
_BACKOFF_MAX_MS = 30_000


def build_websocket_url(
    base_url: str, bot_key: str, websocket_url: Optional[str] = None
) -> str:
    """Build the ``/cable`` URL for a Sabha workspace.

    If ``websocket_url`` is provided (from the registration response), use
    it verbatim. Otherwise derive from ``base_url``: http→ws / https→wss,
    extract a workspace id from a 7+ digit path prefix (e.g. ``/1000006``)
    and pass it as ``wid`` for multi-tenant SaaS.
    """
    if websocket_url:
        return websocket_url

    parsed = urlparse(base_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    host = parsed.netloc
    params = [f"bot_key={bot_key}"]

    wid_match = re.match(r"^/(\d{7,})", parsed.path)
    if wid_match:
        params.append(f"wid={wid_match.group(1)}")

    return f"{ws_scheme}://{host}/cable?" + "&".join(params)


PayloadHandler = Callable[[dict[str, Any]], Awaitable[None]]


class SabhaMonitor:
    """ActionCable WS monitor with FIFO dedup + reconnect."""

    def __init__(
        self,
        ws_url: str,
        bot_id: int,
        on_payload: PayloadHandler,
        *,
        session: Optional[aiohttp.ClientSession] = None,
        on_fatal: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._ws_url = ws_url
        self._bot_id = bot_id
        self._on_payload = on_payload
        self._on_fatal = on_fatal
        self._session = session
        self._owns_session = session is None
        self._dedup = DedupCache(_DEDUP_TTL_MS, _DEDUP_MAX_SIZE)
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop(), name="sabha-monitor")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _run_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                attempt = 0
            except _AuthRejected as e:
                logger.error("Sabha: auth rejected — %s", e)
                if self._on_fatal is not None:
                    self._on_fatal("auth_rejected", str(e))
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                attempt += 1
                delay = self._backoff(attempt)
                logger.warning(
                    "Sabha: WS error (attempt %d), reconnecting in %.1fs: %s",
                    attempt,
                    delay,
                    e,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    continue

    @staticmethod
    def _backoff(attempt: int) -> float:
        cap = min(_BACKOFF_MAX_MS, _BACKOFF_BASE_MS * (2 ** (attempt - 1)))
        return random.uniform(0, cap) / 1000.0

    async def _connect_once(self) -> None:
        session = await self._ensure_session()
        async with session.ws_connect(self._ws_url, heartbeat=30) as ws:
            subscribed = asyncio.Event()
            await ws.send_str(
                json.dumps({"command": "subscribe", "identifier": _BOT_EVENTS_IDENT})
            )

            async for msg in ws:
                if self._stop.is_set():
                    await ws.close()
                    return
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_frame(msg.data, subscribed)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    return
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(f"WS error: {ws.exception()}")

    async def _handle_frame(self, raw: str, subscribed: asyncio.Event) -> None:
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Sabha: non-JSON WS frame: %r", raw[:200])
            return

        kind = envelope.get("type")
        if kind == "ping":
            return
        if kind == "welcome":
            return
        if kind == "confirm_subscription":
            subscribed.set()
            logger.info("Sabha: BotEventsChannel subscribed")
            return
        if kind == "reject_subscription":
            raise _AuthRejected("BotEventsChannel rejected — check bot_key")
        if kind == "disconnect":
            reason = envelope.get("reason", "")
            reconnect = envelope.get("reconnect", True)
            if not reconnect or reason in {"unauthorized", "invalid_request"}:
                raise _AuthRejected(f"Server disconnect: {reason}")
            raise RuntimeError(f"Server disconnect: {reason}")

        # ActionCable wraps real payloads inside ``{"identifier", "message"}``.
        message = envelope.get("message")
        if message is None:
            return
        if not subscribed.is_set():
            logger.debug("Sabha: dropping pre-subscribe frame")
            return

        try:
            payload = parse_webhook_payload(message)
        except WebhookParseError as e:
            logger.warning("Sabha: invalid payload: %s", e)
            return

        if is_self_echo(payload, self._bot_id):
            return

        key = build_dedup_key(payload)
        if key is not None:
            if self._dedup.has(key):
                return
            self._dedup.mark(key)

        try:
            await self._on_payload(payload)
        except Exception:
            logger.exception("Sabha: payload handler raised — rolling back dedup")
            if key is not None:
                self._dedup.unmark(key)


class _AuthRejected(RuntimeError):
    pass
