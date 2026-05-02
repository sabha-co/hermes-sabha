"""
HTTP client for Sabha's Bot API.

All endpoints authenticate via ``Authorization: Bearer <bot_key>``. The
``api_base_url`` is the full bot-API base (e.g.
``https://sabha.co/1000006/api/bots``) — every path below is a suffix
appended to it.

Mutating message ops are id-only on the wire: ``editMessage(id, text)``,
``deleteMessage(id)``, reactions all hit ``/messages/:id[…]``. The server
resolves the room from the message and authorizes via the bot's room
access. Threading uses ``parent_message_id`` as a query param on send;
the URL room is the parent, the server resolves the thread room
internally and returns the resolved ``room_id`` in the response body.

Outbound text is markdown -> Trix HTML — every method that takes ``text``
runs it through ``markdown_to_sabha_rich_text`` before POST/PATCH.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import aiohttp

from .format import markdown_to_sabha_rich_text

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_SECONDS = 30.0


class SabhaApiError(Exception):
    """REST call failed. Carries status + body for diagnostics."""

    def __init__(self, status: int, message: str, *, path: str = "") -> None:
        super().__init__(f"Sabha API {status} on {path}: {message}")
        self.status = status
        self.path = path
        self.body = message


class SabhaClient:
    """Thin async wrapper around Sabha's bot REST surface."""

    def __init__(
        self,
        api_base_url: str,
        bot_key: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._bot_key = bot_key
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._owns_session = session is None
        self._session = session

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _headers(self, content_type: Optional[str] = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._bot_key}"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        json: Any = None,
    ) -> aiohttp.ClientResponse:
        url = f"{self._api_base_url}{path}"
        session = await self._ensure_session()
        resp = await session.request(
            method,
            url,
            headers=headers,
            data=data,
            json=json,
        )
        if resp.status >= 400:
            body = await resp.text()
            resp.release()
            raise SabhaApiError(resp.status, body, path=path)
        return resp

    # ---- Identity ----

    async def get_bot_self(self) -> dict[str, Any]:
        """``GET /me`` — returns the bot's own user record.

        Used at connect-time to discover the numeric bot id, which the
        monitor needs for self-echo filtering and the adapter needs for
        the @-mention group filter.
        """
        resp = await self._request("GET", "/me", headers=self._headers())
        try:
            return await resp.json()
        finally:
            resp.release()

    # ---- Messaging (write) ----

    async def send_message(
        self,
        room_id: int | str,
        text: str,
        *,
        parent_message_id: Optional[int | str] = None,
    ) -> Optional[dict[str, int]]:
        """Send a markdown message. Returns ``{id, room_id}`` or None.

        When ``parent_message_id`` is provided the server routes the
        message into the parent's thread room (creating it idempotently
        via ``Rooms::Thread.find_or_create_for``); the response's
        ``room_id`` is the resolved thread room, which may differ from
        the URL room.
        """
        body = markdown_to_sabha_rich_text(text)
        path = f"/rooms/{room_id}/messages"
        if parent_message_id is not None:
            path = f"{path}?parent_message_id={parent_message_id}"
        resp = await self._request(
            "POST",
            path,
            headers=self._headers("text/plain"),
            data=body.encode("utf-8"),
        )
        try:
            return await self._parse_send_response(resp)
        finally:
            resp.release()

    async def send_attachment(
        self,
        room_id: int | str,
        file_path: str,
        *,
        filename: Optional[str] = None,
    ) -> Optional[dict[str, int]]:
        """Multipart upload to ``POST /rooms/:id/messages``."""
        with open(file_path, "rb") as fh:
            data = aiohttp.FormData()
            data.add_field(
                "attachment",
                fh,
                filename=filename or file_path.rsplit("/", 1)[-1],
            )
            resp = await self._request(
                "POST",
                f"/rooms/{room_id}/messages",
                headers=self._headers(),
                data=data,
            )
            try:
                return await self._parse_send_response(resp)
            finally:
                resp.release()

    async def edit_message(self, message_id: int | str, text: str) -> None:
        """Id-only PATCH. Server resolves room from message id."""
        body = markdown_to_sabha_rich_text(text)
        resp = await self._request(
            "PATCH",
            f"/messages/{message_id}",
            headers=self._headers("text/plain"),
            data=body.encode("utf-8"),
        )
        resp.release()

    async def delete_message(self, message_id: int | str) -> None:
        resp = await self._request(
            "DELETE", f"/messages/{message_id}", headers=self._headers()
        )
        resp.release()

    # ---- Reactions ----

    async def add_reaction(self, message_id: int | str, emoji: str) -> None:
        resp = await self._request(
            "POST",
            f"/messages/{message_id}/boosts",
            headers=self._headers("application/json"),
            json={"content": emoji},
        )
        resp.release()

    async def remove_reaction(
        self, message_id: int | str, boost_id: int | str
    ) -> None:
        resp = await self._request(
            "DELETE",
            f"/messages/{message_id}/boosts/{boost_id}",
            headers=self._headers(),
        )
        resp.release()

    # ---- Directory ----

    async def list_rooms(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/rooms", headers=self._headers())
        try:
            return await resp.json()
        finally:
            resp.release()

    async def list_users(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/users", headers=self._headers())
        try:
            return await resp.json()
        finally:
            resp.release()

    # ---- Attachment fetch ----

    async def fetch_attachment(self, signed_url: str) -> bytes:
        """Download a signed attachment URL.

        Sabha attachment URLs come pre-signed in the inbound payload and
        expire ~1h, so the monitor must fetch them inline before handing
        the event to ``handle_message``.
        """
        session = await self._ensure_session()
        async with session.get(signed_url) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise SabhaApiError(resp.status, body, path=signed_url)
            return await resp.read()

    # ---- Internals ----

    @staticmethod
    async def _parse_send_response(
        resp: aiohttp.ClientResponse,
    ) -> Optional[dict[str, int]]:
        try:
            data = await resp.json(content_type=None)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        msg_id = data.get("id")
        room_id = data.get("room_id")
        if not isinstance(msg_id, int) or not isinstance(room_id, int):
            return None
        return {"id": msg_id, "room_id": room_id}
