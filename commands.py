"""Hermes's gateway commands as Sabha slash commands.

Sabha shows an app's declared commands when a member types ``/`` in a room the app is in, and
sends the app a ``command`` event instead of posting the text. Hermes declares the same list it
gives Slack, ``/hermes`` first so every command stays reachable as ``/hermes <name>`` when another
app already holds its name.
"""

from __future__ import annotations

from typing import Any, Iterable

# Sabha's own commands, which no app may declare.
RESERVED = frozenset({"help", "play", "me", "invite", "leave", "remind", "topic"})
MAX_COMMANDS = 25
_TEXT_LIMIT = 100


def declarations(slashes: Iterable[tuple[str, str, str]]) -> list[dict[str, str]]:
    """``(name, description, usage_hint)`` triples, already in Slack's name form (which is
    Sabha's), as the body of ``PUT /profile/commands``."""
    declared: list[dict[str, str]] = []
    for name, description, usage_hint in slashes:
        if name in RESERVED or any(d["name"] == name for d in declared):
            continue
        declared.append({"name": name, "description": (description or f"/{name}")[:_TEXT_LIMIT],
                         "usage_hint": (usage_hint or "")[:_TEXT_LIMIT]})
        if len(declared) == MAX_COMMANDS:
            break
    return declared


def command_text(event: dict[str, Any], subcommands: dict[str, str]) -> str:
    """The gateway text for a ``command`` event: ``/name args``, or for ``/hermes <name> args``
    the command it names. Anything else after ``/hermes`` is a question."""
    name = str(event.get("command") or "").lstrip("/")
    text = str(event.get("text") or "").strip()
    if name != "hermes":
        return f"/{name} {text}".strip()
    first, _, rest = text.partition(" ")
    if first in subcommands:
        return f"{subcommands[first]} {rest.strip()}".strip()
    return text or "/help"
