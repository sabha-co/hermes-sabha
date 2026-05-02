"""
Sabha webhook payload parsing and helpers.

The wire envelope arrives as JSON over the ActionCable WebSocket —
Sabha's ``BotEventsChannel`` fans out seven event types. message/boost
events carry ``room`` and ``message``; ``user_*`` events fan out
workspace-globally and carry only ``user``.
"""

from __future__ import annotations

from typing import Any, Mapping

_MESSAGE_BEARING_EVENTS = frozenset(
    {
        "message_created",
        "message_updated",
        "message_deleted",
        "boost_created",
        "boost_deleted",
    }
)
_BOOST_EVENTS = frozenset({"boost_created", "boost_deleted"})
_USER_EVENTS = frozenset({"user_created", "user_deleted"})
_KNOWN_EVENTS = _MESSAGE_BEARING_EVENTS | _USER_EVENTS


class WebhookParseError(ValueError):
    """Raised when a Sabha webhook payload fails validation."""


def parse_webhook_payload(body: Any) -> dict[str, Any]:
    """Validate and normalize a Sabha webhook payload.

    Enforces the discriminated-union shape:
      - message/boost events MUST carry ``room`` and ``message`` (boost events
        additionally require ``boost``).
      - ``user_*`` events MUST NOT be rejected for missing ``room`` /
        ``message``; Sabha's fan-out doesn't include them.
    """
    if not isinstance(body, Mapping):
        raise WebhookParseError("Invalid webhook payload: not an object")

    event = body.get("event")
    if not isinstance(event, str) or not event:
        raise WebhookParseError("Missing or invalid 'event' field")
    if event not in _KNOWN_EVENTS:
        raise WebhookParseError(f"Unknown event type: {event}")

    user = body.get("user")
    if not isinstance(user, Mapping):
        raise WebhookParseError("Missing or invalid 'user' field")

    if event in _MESSAGE_BEARING_EVENTS:
        if not isinstance(body.get("room"), Mapping):
            raise WebhookParseError(f"{event}: missing or invalid 'room' field")
        if not isinstance(body.get("message"), Mapping):
            raise WebhookParseError(f"{event}: missing or invalid 'message' field")
        if event in _BOOST_EVENTS:
            if not isinstance(body.get("boost"), Mapping):
                raise WebhookParseError(
                    f"{event}: missing or invalid 'boost' field"
                )

    return dict(body)


def is_message_bearing(payload: Mapping[str, Any]) -> bool:
    return payload.get("event") in _MESSAGE_BEARING_EVENTS


def resolve_chat_type(room_type: str) -> str:
    """Convert Sabha room type to Hermes chat_type ("dm" | "group")."""
    return "dm" if room_type == "Direct" else "group"


def was_bot_mentioned(payload: Mapping[str, Any], bot_id: int) -> bool:
    """True when ``bot_id`` appears in ``message.mentionees``."""
    msg = payload.get("message") or {}
    mentionees = msg.get("mentionees") or []
    return any(
        isinstance(m, Mapping) and m.get("id") == bot_id for m in mentionees
    )


def is_self_echo(payload: Mapping[str, Any], bot_id: int) -> bool:
    """True when the actor of this event is the bot itself.

    Runs BEFORE the dedup cache so streaming-edit echoes never enter the
    cache or generate ``Skipping duplicate`` log noise. Every ``editMessage``
    the bot issues fans a ``message_updated`` frame back over the WS.
    """
    event = payload.get("event")
    if event in _USER_EVENTS:
        return False
    user = payload.get("user") or {}
    return user.get("id") == bot_id


def build_dedup_key(payload: Mapping[str, Any]) -> str | None:
    """Stable key for FIFO dedup. Returns None for events that bypass the cache."""
    event = payload.get("event")
    msg = payload.get("message") or {}
    boost = payload.get("boost") or {}
    if event == "message_created":
        return f"message_created:{msg.get('id')}"
    if event == "message_updated":
        # Fold updated_at so repeated edits of the same message id stay distinct.
        return f"message_updated:{msg.get('id')}:{msg.get('updated_at')}"
    if event == "message_deleted":
        return f"message_deleted:{msg.get('id')}"
    if event in _BOOST_EVENTS:
        return f"{event}:{boost.get('id')}"
    return None
