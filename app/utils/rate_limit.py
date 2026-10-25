"""Per-person budgets for the expensive routes (review S17).

Deploying (cluster objects, a job), registering an app or submitting a
version (a clone of the app repository) and the user search (Keycloak's
admin API) each cost far more than a page view. Nothing throttled them, so
one person, or a stolen token, could keep the API, the cluster or Keycloak
busy. Each route gets a budget per person and minute; above it the API
answers 429 ``rate_limited`` with ``Retry-After``.

The counters live in the process (a sliding window per person and route):
with several workers and replicas the budget applies per process, so the
real limit is a small multiple of it. That is enough to stop a runaway
script; it is no quota.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Depends, HTTPException, status

from app.models import User
from app.utils.keycloak_auth import get_current_user_keycloak

WINDOW_SECONDS = 60.0


class SlidingWindow:
    """At most ``limit`` hits per key within ``window`` seconds."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float = WINDOW_SECONDS) -> float:
        """Count a hit; 0 if it is within the budget, else the seconds until there is room again."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return hits[0] + window - now
            hits.append(now)
            return 0.0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindow()


def per_person(name: str, limit: int) -> Callable[..., User]:
    """A dependency: ``limit`` requests per person and minute to the route ``name``."""

    def check(current_user: User = Depends(get_current_user_keycloak)) -> User:
        wait = limiter.hit(f"{name}:{current_user.userId}", limit)
        if wait:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "rate_limited",
                    "message": "Too many requests; please wait a moment and try again",
                },
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )
        return current_user

    return check


# The budgets: far above what a person clicks, far below what a script sends.
DEPLOY = per_person("deploy", 10)
REGISTER_APP = per_person("register-app", 30)
SUBMIT_VERSION = per_person("submit-version", 60)
USER_SEARCH = per_person("user-search", 120)
