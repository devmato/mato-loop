"""Owner-scoped invalidation hints; REST remains the source of truth."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from uuid import UUID

Change = tuple[str, UUID | None]


class ChangeEventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[asyncio.Queue[set[Change]]]] = defaultdict(set)

    def publish(self, user_id: UUID | None, changes: set[Change]) -> None:
        subscribers = self._subscribers.get(user_id, ()) if user_id is not None else (
            queue for queues in self._subscribers.values() for queue in queues
        )
        for queue in subscribers:
            pending = queue.get_nowait() if not queue.empty() else set()
            pending.update(changes)
            queue.put_nowait({("all", None)} if len(pending) > 128 else pending)

    async def subscribe(self, user_id: UUID) -> AsyncIterator[set[Change]]:
        queue: asyncio.Queue[set[Change]] = asyncio.Queue(maxsize=1)
        self._subscribers[user_id].add(queue)
        try:
            # Subscribe before reconciliation so concurrent commits cannot fall into a gap.
            yield {("all", None)}
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield set()
        finally:
            self._subscribers[user_id].discard(queue)
            if not self._subscribers[user_id]:
                self._subscribers.pop(user_id, None)


# shortcut: delivery is process-local like run streaming; share events before supporting multiple workers.
change_events = ChangeEventBroker()
