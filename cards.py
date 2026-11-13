"""Hermes's approval and clarify prompts as Sabha Block Kit cards.

Sabha takes Slack's blocks, so the cards are the Slack adapter's: a mrkdwn section with the prompt
and a row of buttons. A click comes back as a ``block_actions`` event naming the button's
``action_id``, with the ``value`` read from the stored card, never from the browser.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

APPROVAL_ACTIONS = {"once": "hermes_approve_once", "session": "hermes_approve_session",
                    "always": "hermes_approve_always", "deny": "hermes_deny"}
APPROVAL_CHOICES = {action_id: choice for choice, action_id in APPROVAL_ACTIONS.items()}
APPROVAL_OUTCOMES = {"once": "Approved once", "session": "Approved for this session",
                     "always": "Approved permanently", "deny": "Denied"}
CONFIRM_ACTIONS = {"once": "hermes_confirm_once", "always": "hermes_confirm_always", "cancel": "hermes_confirm_cancel"}
CONFIRM_CHOICES = {action_id: choice for choice, action_id in CONFIRM_ACTIONS.items()}
CONFIRM_OUTCOMES = {"once": "Approved once", "always": "Always approved", "cancel": "Cancelled"}
CLARIFY_CHOICE = "hermes_clarify_choice"
CLARIFY_OTHER = "hermes_clarify_other"

SECTION_LIMIT = 3000
_LABEL_LIMIT = 75
# Sabha's cap on a card's controls, less the "Other" button.
_CLARIFY_CHOICES = 9


def escape(text: str) -> str:
    """mrkdwn reads ``<…>`` as a link or mention and ``&`` as an entity; Slack's escaping keeps them literal."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:SECTION_LIMIT]}}


def _button(label: str, action_id: str, value: str, style: str = "") -> dict[str, Any]:
    button = {"type": "button", "text": {"type": "plain_text", "text": label[:_LABEL_LIMIT]},
              "action_id": action_id, "value": value}
    return {**button, "style": style} if style else button


def approval(text: str, actions: list[tuple[str, str, str]], session_key: str) -> list[dict[str, Any]]:
    """``actions`` are Hermes's ``(label, choice, style)`` rows."""
    buttons = [_button(label, APPROVAL_ACTIONS[choice], session_key, style) for label, choice, style in actions]
    return [_section(text), {"type": "actions", "elements": buttons}]


def confirm(text: str, confirm_id: str) -> list[dict[str, Any]]:
    """A slash command asking before it acts, like ``/new`` dropping the conversation."""
    buttons = [_button("Approve Once", CONFIRM_ACTIONS["once"], confirm_id, "primary"),
               _button("Always Approve", CONFIRM_ACTIONS["always"], confirm_id),
               _button("Cancel", CONFIRM_ACTIONS["cancel"], confirm_id, "danger")]
    return [_section(text), {"type": "actions", "elements": buttons}]


def confirm_text(title: str, message: str) -> str:
    """Hermes's message carries its own heading, so the title shows only when there's no message."""
    return escape(message or f"Confirm {title}")


def clarify(question: str, choices: list[Any], clarify_id: str) -> list[dict[str, Any]]:
    buttons = [_button(str(choice).strip() or f"Option {index + 1}", f"{CLARIFY_CHOICE}_{index}",
                       f"{clarify_id}|{index}")
               for index, choice in enumerate(choices[:_CLARIFY_CHOICES])]
    buttons.append(_button("Other…", CLARIFY_OTHER, f"{clarify_id}|other"))
    return [_section(question_text(question)), {"type": "actions", "elements": buttons}]


def question_text(question: str) -> str:
    return f"❓ {escape(question or '')}"


def answered(text: str, note: str) -> list[dict[str, Any]]:
    """A card once it's answered: its prompt with the outcome under it, and no buttons."""
    return [_section(text), {"type": "context", "elements": [{"type": "mrkdwn", "text": escape(note)}]}]


@dataclass
class Card:
    kind: str  # "approval", "confirm" or "clarify"
    text: str
    chat_id: str
    session_key: str = ""
    request_id: Optional[str] = None
    clarify_id: str = ""
    confirm_id: str = ""
    thread_id: Optional[str] = None


class Cards:
    """Cards still waiting for an answer, by message id. Hermes keeps its own pending approvals and
    clarifies only in memory, so after a restart a card has nothing left to answer either way."""

    def __init__(self, limit: int = 500):
        self._cards: OrderedDict[str, Card] = OrderedDict()
        self._limit = limit

    def add(self, message_id: str, card: Card) -> None:
        self._cards[message_id] = card
        while len(self._cards) > self._limit:
            self._cards.popitem(last=False)

    def get(self, message_id: str) -> Optional[Card]:
        return self._cards.get(message_id)

    def pop(self, message_id: str) -> Optional[Card]:
        return self._cards.pop(message_id, None)

    def pop_clarify(self, clarify_id: str) -> Optional[tuple[str, Card]]:
        for message_id, card in self._cards.items():
            if card.kind == "clarify" and card.clarify_id == clarify_id:
                return message_id, self._cards.pop(message_id)
        return None
