"""
Message dedup cache with TTL.

Prevents duplicate processing when the WebSocket reconnects deliver
events that were already handled in a previous connection.

FIFO eviction by insertion order — NOT LRU. ``has()`` does not refresh
position, which is deliberate: entries expire on wall-clock TTL, so the
oldest insert is also the one closest to its natural expiry. True LRU
would let an entry outlive its expiry under churn.
"""

import time
from collections import OrderedDict


class DedupCache:
    """FIFO + TTL dedup cache."""

    def __init__(self, ttl_ms: int, max_size: int) -> None:
        self._ttl_ms = ttl_ms
        self._max_size = max_size
        self._entries: OrderedDict[str, float] = OrderedDict()
        self._last_purge_ms = self._now_ms()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _purge(self) -> None:
        now = self._now_ms()
        if now - self._last_purge_ms < self._ttl_ms / 2:
            return
        self._last_purge_ms = now
        expired = [k for k, exp in self._entries.items() if exp <= now]
        for k in expired:
            self._entries.pop(k, None)

    def has(self, key: str) -> bool:
        self._purge()
        return key in self._entries

    def mark(self, key: str) -> None:
        """Mark a key as seen. Evicts the oldest entry when at capacity."""
        self._purge()
        if len(self._entries) >= self._max_size and key not in self._entries:
            self._entries.popitem(last=False)
        self._entries[key] = self._now_ms() + self._ttl_ms

    def unmark(self, key: str) -> None:
        """Drop a previously-marked key. Used to roll back optimistic marks."""
        self._entries.pop(key, None)

    def size(self) -> int:
        return len(self._entries)
