"""A thin client for Sabha's apps API (``/api/v1``, bearer key)."""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)


class SabhaApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: Optional[float] = None,
                 payload: Optional[dict[str, Any]] = None):
        super().__init__(f"Sabha API {status} {code}: {message}")
        self.status, self.code, self.retry_after = status, code, retry_after
        self.payload = payload or {}


@dataclass(frozen=True)
class Posted:
    id: str
    room_id: str


class SabhaApi:
    def __init__(self, session: aiohttp.ClientSession, api_url: str, key: str):
        self._session = session
        self._base = api_url.rstrip("/")
        self._auth = {"Authorization": f"Bearer {key}"}
        self._takes_client_message_id = True

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def room(self, room_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/rooms/{room_id}")

    async def post_message(self, room_id: str, html: str, *, parent_message_id: Optional[str] = None,
                           client_message_id: Optional[str] = None) -> Posted:
        return await self._create(room_id, parent_message_id, client_message_id,
                                  lambda extra: {"json": {"body": html, **extra}})

    async def post_file(self, room_id: str, path: str, *, filename: Optional[str] = None,
                        parent_message_id: Optional[str] = None, client_message_id: Optional[str] = None) -> Posted:
        name = filename or Path(path).name
        content = Path(path).read_bytes()

        def request(extra: dict[str, str]) -> dict[str, Any]:
            form = aiohttp.FormData()
            form.add_field("attachment", content, filename=name,
                           content_type=mimetypes.guess_type(name)[0] or "application/octet-stream")
            for field, value in extra.items():
                form.add_field(field, value)
            return {"data": form, "timeout": 120}

        return await self._create(room_id, parent_message_id, client_message_id, request)

    async def edit_message(self, message_id: str, html: str) -> None:
        await self._request("PATCH", f"/messages/{message_id}", json={"body": html})

    async def delete_message(self, message_id: str) -> None:
        await self._request("DELETE", f"/messages/{message_id}")

    async def set_status(self, room_id: str, status: str, *, parent_message_id: Optional[str] = None) -> None:
        """The line under the app's working shell where its reply will land; posting there clears it."""
        await self._request("PUT", f"/rooms/{room_id}/status",
                            json={"status": status, **({"parent_message_id": parent_message_id} if parent_message_id else {})})

    async def clear_status(self, room_id: str, *, parent_message_id: Optional[str] = None) -> None:
        await self._request("DELETE", f"/rooms/{room_id}/status", params=_thread_params(parent_message_id))

    async def declare_commands(self, commands: list[dict[str, str]]) -> list[str]:
        """Replace the app's slash commands. Sabha takes the list whole or not at all; a refused
        list changes nothing and its refused names come back, so the caller can declare the rest."""
        try:
            await self._request("PUT", "/profile/commands", json={"commands": commands})
        except SabhaApiError as exc:
            refused = [str(r.get("name")) for r in exc.payload.get("refusals") or [] if isinstance(r, dict)]
            if exc.status != 422 or not refused:
                raise
            return refused
        return []

    async def respond(self, response_url: str, payload: dict[str, Any]) -> None:
        """Answer a command or a click through its response_url, whose token is its own credential."""
        async with self._session.post(response_url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status >= 400:
                body = await _json_or_empty(resp)
                raise SabhaApiError(resp.status, str(body.get("code") or resp.reason), str(body.get("error") or ""))

    async def download(self, url: str) -> bytes:
        """Fetch an attachment by its signed URL, which carries its own credential."""
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _create(self, room_id: str, parent_message_id: Optional[str], client_message_id: Optional[str],
                      request: Callable[[dict[str, str]], dict[str, Any]]) -> Posted:
        """Post a message. With a client_message_id, a retry after a lost response gets back the post
        Sabha already saved instead of a second one. A server older than that refuses the key; the
        post then goes without it, and so does every later one."""
        path, params = f"/rooms/{room_id}/messages", _thread_params(parent_message_id)
        extra = {"client_message_id": client_message_id} if client_message_id and self._takes_client_message_id else {}
        try:
            data = await self._request("POST", path, params=params, **request(extra))
        except SabhaApiError as exc:
            if not (extra and exc.status == 422 and "client_message_id is not permitted" in str(exc)):
                raise
            logger.info("Sabha: this server doesn't take client_message_id, so retried posts may land twice")
            self._takes_client_message_id = False
            data = await self._request("POST", path, params=params, **request({}))
        return Posted(str(data["id"]), str(data["room_id"]))

    async def _request(self, method: str, path: str, *, json: Any = None, data: Any = None,
                       params: Optional[dict] = None, timeout: float = 30) -> dict[str, Any]:
        async with self._session.request(method, self._base + path, headers=self._auth, json=json, data=data,
                                         params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 204:
                return {}
            payload = await _json_or_empty(resp)
            if resp.status >= 400:
                retry_after = resp.headers.get("Retry-After")
                raise SabhaApiError(resp.status, str(payload.get("code") or resp.reason),
                                    str(payload.get("error") or ""),
                                    float(retry_after) if retry_after and retry_after.isdigit() else None, payload)
            return payload


def _thread_params(parent_message_id: Optional[str]) -> Optional[dict]:
    return {"parent_message_id": parent_message_id} if parent_message_id else None


async def _json_or_empty(resp: aiohttp.ClientResponse) -> dict[str, Any]:
    if resp.content_type != "application/json":
        return {}
    try:
        payload = await resp.json()
    except (aiohttp.ContentTypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
