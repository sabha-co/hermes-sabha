"""The line under Sabha's working shell while the agent works on a reply.

Sabha shows an app's status where its reply will land (a room, or the thread under a message) and
clears it when the app posts there, or after two minutes without an update. Hermes refreshes its
typing indicator every couple of seconds; this keeps that from becoming a request each time.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

DEFAULT = "Thinking…"
MAX_CHARS = 120
# Sabha ends a status left two minutes without an update, so an unchanged line is sent again well
# inside that.
REFRESH_SECONDS = 60.0

Place = tuple[str, Optional[str]]  # room id, and the message whose thread the reply goes in


class StatusLines:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._sent: dict[Place, tuple[str, float]] = {}
        self._answered: set[Place] = set()
        self._quiet: set[str] = set()

    def due(self, place: Place, phrase: Optional[str]) -> Optional[str]:
        """The line to send now, or None when it would change nothing."""
        if place in self._answered or place[0] in self._quiet:
            return None
        line = " ".join((phrase or DEFAULT).split())[:MAX_CHARS] or DEFAULT
        last = self._sent.get(place)
        if last and last[0] == line and self._clock() - last[1] < REFRESH_SECONDS:
            return None
        return line

    def sent(self, place: Place, line: str) -> None:
        self._sent[place] = (line, self._clock())

    def posted(self, place: Place) -> None:
        """The app posted there, which clears its status in Sabha. A typing refresh between the reply
        and the turn's end mustn't put one back."""
        self._answered.add(place)
        self._sent.pop(place, None)

    def quiet(self, chat_id: str) -> None:
        """A sender the gateway is about to turn away gets no status in that room."""
        self._quiet.add(chat_id)

    def finish(self, chat_id: str, thread_id: Optional[str] = None) -> list[Place]:
        """The turn is over. Returns the places whose status is still showing, to clear."""
        self._quiet.discard(chat_id)
        mine = [place for place in self._sent.keys() | self._answered
                if place[0] == chat_id and (thread_id is None or place[1] == thread_id)]
        self._answered.difference_update(mine)
        return [place for place in mine if self._sent.pop(place, None) is not None]
