"""Where to resume Sabha's event stream after a reconnect or a gateway restart.

Sabha's socket is AnyCable, which keeps a short history of each stream (100 events or 5 minutes
by default). Every event carries its stream's ``epoch`` and ``offset``; after subscribing, a
``history`` request asks for everything after the last handled offset, or, for a stream with none
yet, since a time. A position advances only once Hermes has taken the event, so a replay starts
right after it and nothing is handled twice.

Positions are saved with the last moment the app was connected, so a restarted gateway asks for
what arrived while it was down. Saved state from another Sabha is ignored.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Ask a little before the last known moment, for events in flight then.
OVERLAP_SECONDS = 5


class Replay:
    def __init__(self, path: Optional[Path], server: str, clock: Callable[[], float] = time.time):
        self._path, self._server, self._clock = path, server, clock
        self._positions: dict[str, dict[str, Any]] = {}
        self._since: Optional[int] = None
        self._warned = False
        self._load()

    def history(self) -> Optional[dict[str, Any]]:
        """The ``history`` request to send after subscribing, or None when there's nothing to ask."""
        if not self._positions and self._since is None:
            return None
        request: dict[str, Any] = {"streams": dict(self._positions)}
        if self._since is not None:
            request["since"] = self._since
        return request

    def subscribed(self) -> None:
        """A later reconnect asks from here for streams with no position yet."""
        self._since = int(self._clock()) - OVERLAP_SECONDS
        self._save()

    def handled(self, stream_id: Any, epoch: Any, offset: Any) -> None:
        if not (isinstance(stream_id, str) and isinstance(epoch, str) and isinstance(offset, int)):
            return
        known = self._positions.get(stream_id)
        if known is None or known["epoch"] != epoch or offset > known["offset"]:
            self._positions[stream_id] = {"epoch": epoch, "offset": offset}
            self._save()

    def disconnected(self) -> None:
        """Records that the app was connected until now."""
        self._save()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            saved = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("Sabha: ignoring unreadable replay state %s (%s)", self._path, exc)
            return
        if not isinstance(saved, dict) or saved.get("server") != self._server:
            return
        positions = saved.get("positions")
        if isinstance(positions, dict):
            self._positions = {stream: dict(p) for stream, p in positions.items()
                               if isinstance(p, dict) and isinstance(p.get("epoch"), str)
                               and isinstance(p.get("offset"), int)}
        if isinstance(saved.get("connected_at"), int):
            self._since = saved["connected_at"] - OVERLAP_SECONDS

    def _save(self) -> None:
        if self._path is None:
            return
        state = {"server": self._server, "positions": self._positions, "connected_at": int(self._clock())}
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            os.replace(tmp, self._path)
        except OSError as exc:
            if not self._warned:
                self._warned = True
                logger.warning("Sabha: can't save replay state to %s (%s); a restart may miss events", self._path, exc)
