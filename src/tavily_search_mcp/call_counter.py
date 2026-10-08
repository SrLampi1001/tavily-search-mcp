"""In-memory call counter for the Tavily budget.

Per SPECS §7: Tavily has no live credit-balance endpoint (the only related
endpoint, `GET /usage`, is documented as for billing-period summaries, not
per-remaining-balance). The server counts calls made this process instead and
surfaces `calls_this_session` on every response so the model can self-budget.

The counter lives in the lifespan context so it shuts down with the FastMCP
server rather than leaking across restarts. Two server processes sharing one
API key each count their own calls; for a single-process deployment that's
accurate enough.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class CallCounter:
    """Thread-safe wrapper around the local session counter."""

    calls_this_session: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def charge(self) -> int:
        """Increment the counter after a successful upstream call.

        Returns the post-increment value (the same one the model will see in
        `usage.calls_this_session`). No-op on cache hits — callers only invoke
        this after the upstream returned.
        """
        with self._lock:
            self.calls_this_session += 1
            return self.calls_this_session

    def snapshot(self) -> dict[str, int]:
        """Read-only view returned to the model in every call."""
        with self._lock:
            return {"calls_this_session": self.calls_this_session}
