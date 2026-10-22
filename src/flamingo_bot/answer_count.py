"""A lifetime answer total, not visitor analytics or a conversation history."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from flamingo_bot.errors import ProviderError

ANSWER_COUNT_PATH = "/v1/answer-count"
ANSWER_COUNT_COLLECTION = "service_totals"
ANSWER_COUNT_DOCUMENT = "answers"
CACHE_SECONDS = 45
OPERATION_TIMEOUT_SECONDS = 2.5


class AnswerCount(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    answered: int = Field(ge=0, le=9_007_199_254_740_991)
    # Historical usage is estimated, and outages can miss increments. This is
    # deliberately a usage indicator rather than an exact accounting ledger.
    approximate: bool = True


class InMemoryAnswerCounter:
    """Local/test storage. Never changes the shared production total."""

    def __init__(self, initial: int = 0) -> None:
        self.value = AnswerCount(answered=initial)
        self._lock = asyncio.Lock()

    async def read(self) -> AnswerCount:
        return self.value

    async def increment(self) -> AnswerCount:
        async with self._lock:
            self.value = AnswerCount(answered=self.value.answered + 1)
            return self.value


class CachedAnswerCounter:
    """Bound reads (including missing/failed reads) across public requests.

    An increment supplies the authoritative value immediately to its caller.
    Other instances can lag by one cache window. No background task, listener,
    per-request record, or identity is retained.
    """

    def __init__(
        self,
        store: AnswerCounterStore,
        *,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._time_source = time_source
        self._expires = float("-inf")
        self._value: AnswerCount | None = None
        self._failed = False
        self._read_lock = asyncio.Lock()

    def _remember(self, value: AnswerCount) -> AnswerCount:
        # A slower earlier read/completion must never roll this instance back.
        if self._value is None or value.answered >= self._value.answered:
            self._value = value
        self._failed = False
        self._expires = self._time_source() + CACHE_SECONDS
        return self._value

    async def read(self) -> AnswerCount | None:
        async with self._read_lock:
            if self._time_source() >= self._expires:
                expires_before_read = self._expires
                try:
                    value = await asyncio.wait_for(
                        self.store.read(), timeout=OPERATION_TIMEOUT_SECONDS
                    )
                except (ProviderError, TimeoutError):
                    if self._expires == expires_before_read:
                        self._failed = True
                        self._expires = self._time_source() + CACHE_SECONDS
                else:
                    if value is not None:
                        self._remember(value)
                    elif self._expires == expires_before_read:
                        self._value = None
                        self._failed = False
                        self._expires = self._time_source() + CACHE_SECONDS
            if self._failed:
                raise ProviderError("Answer total temporarily unavailable")
            return self._value

    async def increment(self) -> AnswerCount:
        # Do not retry an ambiguous write: it may already have committed. The
        # Firestore operation itself has a shorter deadline than this outer bound.
        value = await asyncio.wait_for(self.store.increment(), timeout=OPERATION_TIMEOUT_SECONDS)
        return self._remember(value)


class AnswerCounterStore(Protocol):
    async def read(self) -> AnswerCount | None: ...

    async def increment(self) -> AnswerCount: ...
