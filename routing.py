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
    return body_text(event.get("message") or {})


def body_text(message: dict[str, Any]) -> str:
    body = message.get("body") or {}
    return body.get("markdown") or body.get("plain") or ""


def thread_context(parent: dict[str, Any], replies: list[dict[str, Any]], *, current_id: Any,
                   app_user_id: Optional[str]) -> Optional[str]:
    """The thread so far, oldest first, as Hermes's Slack adapter shows it: the parent, then the
    replies before the message being answered. The app's own replies are marked as its turns."""
    def line(message: dict[str, Any], prefix: str = "") -> Optional[str]:
        text = body_text(message).strip()
        if not text:
            return None
        creator = message.get("creator") or {}
        if app_user_id and str(creator.get("id")) == str(app_user_id):
            return f"{prefix}[assistant] {text}"
        return f"{prefix}{creator.get('name') or 'someone'}: {text}"

    earlier = [m for m in reversed(replies) if str(m.get("id")) != str(current_id)]
    lines = [l for l in [line(parent, "[thread parent] "), *map(line, earlier)] if l]
    if not lines:
        return None
    return ("[Thread context — prior messages in this thread (not yet in conversation history):]\n"
            + "\n".join(lines) + "\n[End of thread context]\n\n")
