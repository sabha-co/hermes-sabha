"""Where an inbound Sabha message lives in Hermes' terms, and where its reply goes.

A Sabha thread is a room of its own, rooted at a message in a parent room. Hermes keys sessions by
chat and thread, as on Slack, so a message in a thread is addressed as ``(parent room, parent
message)``. A reply then always posts to the parent room with ``parent_message_id``, which opens
the thread if it isn't there yet and reuses it if it is. Nothing has to remember thread rooms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Route:
    chat_id: str
    chat_type: str  # "dm", "group" or "channel"
    thread_id: Optional[str]


def inbound_route(event: dict[str, Any], *, reply_in_thread: bool) -> Route:
    room = event.get("room") or {}
    message = event.get("message") or {}
    thread = message.get("thread") or {}

    if thread.get("parent_message_id") and thread.get("parent_room_id"):
        return Route(str(thread["parent_room_id"]), "channel", str(thread["parent_message_id"]))

    room_id = str(room.get("id", ""))
    if room.get("type") == "Direct":
        return Route(room_id, "dm" if int(room.get("members") or 2) <= 2 else "group", None)
    if room.get("type") in ("Open", "Closed") and reply_in_thread:
        return Route(room_id, "channel", str(message.get("id")))
    return Route(room_id, "channel", None)


def is_from_self(event: dict[str, Any], app_user_id: Optional[str]) -> bool:
    return bool(app_user_id) and str((event.get("user") or {}).get("id")) == str(app_user_id)


def message_text(event: dict[str, Any]) -> str:
    body = (event.get("message") or {}).get("body") or {}
    return body.get("markdown") or body.get("plain") or ""
