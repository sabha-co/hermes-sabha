"""A thin client for Sabha's apps API (``/api/v1``, bearer key)."""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


class SabhaApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: Optional[float] = None):
        super().__init__(f"Sabha API {status} {code}: {message}")
        self.status, self.code, self.retry_after = status, code, retry_after


@dataclass(frozen=True)
class Posted:
    id: str
    room_id: str


class SabhaApi:
    def __init__(self, session: aiohttp.ClientSession, api_url: str, key: str):
        self._session = session
        self._base = api_url.rstrip("/")
        self._auth = {"Authorization": f"Bearer {key}"}

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def room(self, room_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/rooms/{room_id}")

    async def post_message(self, room_id: str, html: str, *, parent_message_id: Optional[str] = None) -> Posted:
        data = await self._request("POST", f"/rooms/{room_id}/messages", json={"body": html},
                                   params=_thread_params(parent_message_id))
        return Posted(str(data["id"]), str(data["room_id"]))

    async def post_file(self, room_id: str, path: str, *, filename: Optional[str] = None,
                        parent_message_id: Optional[str] = None) -> Posted:
        name = filename or Path(path).name
        form = aiohttp.FormData()
        form.add_field("attachment", Path(path).read_bytes(), filename=name,
                       content_type=mimetypes.guess_type(name)[0] or "application/octet-stream")
        data = await self._request("POST", f"/rooms/{room_id}/messages", data=form,
                                   params=_thread_params(parent_message_id), timeout=120)
        return Posted(str(data["id"]), str(data["room_id"]))

    async def edit_message(self, message_id: str, html: str) -> None:
        await self._request("PATCH", f"/messages/{message_id}", json={"body": html})

    async def delete_message(self, message_id: str) -> None:
        await self._request("DELETE", f"/messages/{message_id}")

    async def add_reaction(self, message_id: str, emoji: str) -> str:
        data = await self._request("POST", f"/messages/{message_id}/reactions", text=emoji)
        return str(data["id"])

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        await self._request("DELETE", f"/messages/{message_id}/reactions/{reaction_id}")

    async def download(self, url: str) -> bytes:
        """Fetch an attachment by its signed URL, which carries its own credential."""
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            return await resp.read()

    async def _request(self, method: str, path: str, *, json: Any = None, text: Optional[str] = None,
                       data: Any = None, params: Optional[dict] = None, timeout: float = 30) -> dict[str, Any]:
        headers = dict(self._auth)
        if text is not None:
            headers["Content-Type"] = "text/plain; charset=utf-8"
            data = text.encode()
        async with self._session.request(method, self._base + path, headers=headers, json=json, data=data,
                                         params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 204:
                return {}
            payload = await _json_or_empty(resp)
            if resp.status >= 400:
                retry_after = resp.headers.get("Retry-After")
                raise SabhaApiError(resp.status, str(payload.get("code") or resp.reason),
                                    str(payload.get("error") or ""),
                                    float(retry_after) if retry_after and retry_after.isdigit() else None)
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
