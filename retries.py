"""Ids that make a retried send land once.

Every post carries a client_message_id. A send that fails may still have posted: a timeout, or a
5xx after Sabha saved the message. The gateway retries a failed send by sending the same content
again, so the ids of a failed send are held under that send and handed back for the retry. Sabha
then answers each chunk it already saved with that message instead of posting it again.
"""

from __future__ import annotations

import time
import uuid
from typing import Callable, Hashable

# Long enough to cover the gateway's own retries and a later redelivery of the same send.
HOLD_SECONDS = 600
MAX_HELD = 64


class RetryIds:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._held: dict[Hashable, tuple[float, list[str]]] = {}

    def take(self, send: Hashable, count: int) -> list[str]:
        """The ids held for this send if it failed before, otherwise new ones."""
        now = self._clock()
        held = self._held.pop(send, None)
        if held and now - held[0] < HOLD_SECONDS and len(held[1]) == count:
            return held[1]
        return [str(uuid.uuid4()) for _ in range(count)]

    def hold(self, send: Hashable, ids: list[str]) -> None:
        self._held[send] = (self._clock(), ids)
        while len(self._held) > MAX_HELD:
            del self._held[next(iter(self._held))]
