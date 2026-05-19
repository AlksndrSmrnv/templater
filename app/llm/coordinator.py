from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class LLMCoordinator:
    """Limits parallelism of LLM requests and enforces a minimum delay between starts."""

    concurrency: int = 3
    min_delay: float = 2.0

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.concurrency)
        self._last_start = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        await self._sem.acquire()
        async with self._lock:
            now = time.monotonic()
            wait = self._last_start + self.min_delay - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_start = time.monotonic()

    def release(self) -> None:
        self._sem.release()


_coordinator: LLMCoordinator | None = None


def get_coordinator(concurrency: int, min_delay: float) -> LLMCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = LLMCoordinator(concurrency=concurrency, min_delay=min_delay)
    return _coordinator
