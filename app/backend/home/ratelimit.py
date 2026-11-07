"""Per-principal token buckets for the calls that cost a yt-dlp run.

Each bucket holds ``per_minute`` tokens and refills at that rate; a call
takes one. Kept in memory only: a restart forgives everyone.
"""

from __future__ import annotations

import threading
import time

__all__ = ["TokenBucket", "youtube_search", "youtube_add", "reset"]


class TokenBucket:
    def __init__(self, per_minute: int):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._lock = threading.Lock()
        self._state: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        """Take a token for ``key``; False when it has none left."""
        now = time.monotonic()
        with self._lock:
            tokens, at = self._state.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - at) * self.rate)
            if tokens < 1.0:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - 1.0, now)
            if len(self._state) > 10000:          # forget the long-idle
                self._state = {k: v for k, v in self._state.items()
                               if now - v[1] < 600}
            return True

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


#: YouTube searches per principal per minute.
youtube_search = TokenBucket(10)
#: YouTube adds per principal per minute.
youtube_add = TokenBucket(6)


def reset() -> None:
    youtube_search.reset()
    youtube_add.reset()
