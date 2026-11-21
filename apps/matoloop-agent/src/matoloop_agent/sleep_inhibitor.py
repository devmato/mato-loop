from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)
IDLE_GRACE_SECONDS = 5 * 60


class SleepInhibitor:
    """Share one OS sleep assertion across operations on the Agent event loop."""

    def __init__(self, acquire: Callable[[], Callable[[], None] | None]) -> None:
        self._acquire = acquire
        self._release: Callable[[], None] | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._active = 0
        self._warned = False

    @contextmanager
    def hold(self) -> Iterator[None]:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._active == 0 and self._release is None:
            try:
                self._release = self._acquire()
            except OSError as exc:
                if not self._warned:
                    logger.warning("Idle sleep inhibition unavailable: %s", exc)
                    self._warned = True
            else:
                self._warned = False
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            if self._active == 0 and self._release is not None:
                self._timer = asyncio.get_running_loop().call_later(IDLE_GRACE_SECONDS, self.close)

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        release, self._release = self._release, None
        if release is not None:
            try:
                release()
            except OSError as exc:
                logger.warning("Could not release idle sleep inhibition: %s", exc)
