from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import AlreadyExists, DeadlineExceeded, NotFound
from google.cloud.firestore_v1.types import Value, WriteResult
from pydantic import ValidationError

from flamingo_bot.answer_count import (
    ANSWER_COUNT_COLLECTION,
    ANSWER_COUNT_DOCUMENT,
    CACHE_SECONDS,
    AnswerCount,
    CachedAnswerCounter,
    InMemoryAnswerCounter,
)
from flamingo_bot.config import Settings
from flamingo_bot.errors import ConfigurationError, ProviderError
from flamingo_bot.providers import answer_count as provider


@pytest.mark.parametrize("value", [-1, True, 1.2, "100", None, 9_007_199_254_740_992])
def test_counter_values_are_strict_nonnegative_safe_integers(value: object) -> None:
    with pytest.raises(ValidationError):
        AnswerCount(answered=value)


@pytest.mark.asyncio
async def test_memory_counter_is_concurrent_and_contains_no_interaction_records() -> None:
    counter = InMemoryAnswerCounter(370)
    results = await asyncio.gather(*(counter.increment() for _ in range(100)))
    assert sorted(row.answered for row in results) == list(range(371, 471))
    assert (await counter.read()).model_dump() == {"answered": 470, "approximate": True}
    assert set(vars(counter)) == {"value", "_lock"}


class CountingStore(InMemoryAnswerCounter):
    def __init__(self) -> None:
        super().__init__(370)
        self.reads = 0

    async def read(self) -> AnswerCount:
        self.reads += 1
        await asyncio.sleep(0)
        return await super().read()


@pytest.mark.asyncio
async def test_public_cache_coalesces_reads_expires_and_updates_from_completion() -> None:
    now = 0.0
    store = CountingStore()
    cached = CachedAnswerCounter(store, time_source=lambda: now)
    results = await asyncio.gather(*(cached.read() for _ in range(20)))
    assert store.reads == 1
    assert all(row is not None and row.answered == 370 for row in results)
    assert (await cached.increment()).answered == 371
    assert (await cached.read()).answered == 371
    assert store.reads == 1
    now += CACHE_SECONDS
    assert (await cached.read()).answered == 371
    assert store.reads == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_missing_or_failed_reads_are_cached_not_faked_as_zero(failure: bool) -> None:
    class UnavailableStore(InMemoryAnswerCounter):
        reads = 0

        async def read(self) -> AnswerCount | None:
            self.reads += 1
            if failure:
                raise ProviderError("unavailable")
            return None

    store = UnavailableStore()
    cached = CachedAnswerCounter(store)
    for _ in range(5):
        if failure:
            with pytest.raises(ProviderError):
                await cached.read()
        else:
            assert await cached.read() is None
    assert store.reads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("old_read", [None, AnswerCount(answered=370), "error"])
async def test_late_read_cannot_erase_a_fresh_successful_increment(old_read: object) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class RacingStore(InMemoryAnswerCounter):
        async def read(self) -> AnswerCount | None:
            started.set()
            await release.wait()
            if old_read == "error":
                raise ProviderError("old read failed")
            return old_read

    cached = CachedAnswerCounter(RacingStore(370))
    reading = asyncio.create_task(cached.read())
    await started.wait()
    assert (await cached.increment()).answered == 371
    release.set()
    result = await reading
    assert result is not None and result.answered == 371


@pytest.mark.asyncio
async def test_counter_timeouts_are_bounded_without_retry(monkeypatch) -> None:
    from flamingo_bot import answer_count

    calls = 0

    class SlowStore(InMemoryAnswerCounter):
        async def increment(self) -> AnswerCount:
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    monkeypatch.setattr(answer_count, "OPERATION_TIMEOUT_SECONDS", 0.01)
    cached = CachedAnswerCounter(SlowStore())
    with pytest.raises(TimeoutError):
        await cached.increment()
    assert calls == 1


def firestore_counter(monkeypatch) -> tuple[provider.FirestoreAnswerCounter, MagicMock]:
    client = MagicMock()
    monkeypatch.setattr(provider, "firestore_credentials", lambda _: None)
    monkeypatch.setattr(provider.firestore_v1, "Client", lambda **_: client)
    store = provider.FirestoreAnswerCounter(Settings(_env_file=None, gcp_project_id="test"))
    return store, client


@pytest.mark.asyncio
async def test_firestore_atomic_increment_uses_server_result_no_retry_or_extra_read(monkeypatch):
    store, client = firestore_counter(monkeypatch)
    reference = client.collection.return_value.document.return_value
    reference.update.return_value = WriteResult(transform_results=[Value(integer_value=371)])
    assert (await store.increment()).answered == 371
    client.collection.assert_called_once_with(ANSWER_COUNT_COLLECTION)
    client.collection.return_value.document.assert_called_once_with(ANSWER_COUNT_DOCUMENT)
    reference.update.assert_called_once()
    args, kwargs = reference.update.call_args
    assert set(args[0]) == {"answered"}
    assert args[0]["answered"].value == 1
    assert kwargs == {"retry": None, "timeout": provider.RPC_TIMEOUT_SECONDS}
    reference.get.assert_not_called()
    reference.create.assert_not_called()
    await store.close()
    client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [NotFound("missing"), DeadlineExceeded("ambiguous")])
async def test_missing_or_ambiguous_write_never_creates_resets_or_retries(monkeypatch, error):
    store, client = firestore_counter(monkeypatch)
    reference = client.collection.return_value.document.return_value
    reference.update.side_effect = error
    with pytest.raises(ProviderError):
        await store.increment()
    reference.update.assert_called_once()
    reference.create.assert_not_called()
    reference.set.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result", [WriteResult(), WriteResult(transform_results=[Value(string_value="bad")])]
)
async def test_invalid_write_results_do_not_fabricate_a_total(monkeypatch, result):
    store, client = firestore_counter(monkeypatch)
    client.collection.return_value.document.return_value.update.return_value = result
    with pytest.raises(ProviderError):
        await store.increment()


@pytest.mark.asyncio
async def test_firestore_read_exports_only_the_public_total(monkeypatch):
    store, client = firestore_counter(monkeypatch)
    reference = client.collection.return_value.document.return_value
    reference.get.return_value.exists = True
    reference.get.return_value.to_dict.return_value = {
        "answered": 370,
        "baseline": 370,
        "initialized_at": datetime.now(UTC),
    }
    assert (await store.read()).model_dump() == {"answered": 370, "approximate": True}
    reference.get.assert_called_once_with(retry=None, timeout=provider.RPC_TIMEOUT_SECONDS)
    reference.get.return_value.exists = False
    assert await store.read() is None


def test_initializer_is_create_only_and_has_no_expiry_or_event_fields(monkeypatch):
    store, client = firestore_counter(monkeypatch)
    reference = client.collection.return_value.document.return_value
    as_of = datetime(2026, 10, 21, tzinfo=UTC)
    store.initialize(370, as_of)
    args, kwargs = reference.create.call_args
    assert set(args[0]) == {"answered", "baseline", "baseline_as_of", "initialized_at"}
    assert args[0]["answered"] == args[0]["baseline"] == 370
    assert args[0]["baseline_as_of"] == as_of
    assert kwargs == {"retry": None, "timeout": provider.RPC_TIMEOUT_SECONDS}
    reference.create.side_effect = AlreadyExists("already initialized")
    with pytest.raises(AlreadyExists):
        store.initialize(400, as_of)
    reference.set.assert_not_called()
    reference.update.assert_not_called()


@pytest.mark.parametrize(
    "baseline,as_of",
    [
        (-1, datetime(2026, 10, 21, tzinfo=UTC)),
        (370, datetime(2026, 10, 21)),
        (370, datetime(2100, 1, 1, tzinfo=UTC)),
    ],
)
def test_initializer_rejects_invalid_baselines_before_writing(monkeypatch, baseline, as_of):
    store, client = firestore_counter(monkeypatch)
    with pytest.raises(ValueError):
        store.initialize(baseline, as_of)
    client.collection.return_value.document.return_value.create.assert_not_called()


def test_counter_refuses_to_write_the_knowledge_database() -> None:
    with pytest.raises(ConfigurationError):
        provider.FirestoreAnswerCounter(
            Settings(_env_file=None, flamingo_quota_database_id="flamingo-rag"),
            client=MagicMock(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("initialize,exists", [(False, False), (False, True), (True, False)])
async def test_operator_dry_run_is_read_only_and_init_requires_explicit_flag(
    monkeypatch, capsys, initialize, exists
):
    import argparse
    from unittest.mock import AsyncMock

    from scripts import initialize_answer_count as cli

    store = MagicMock()
    store.read = AsyncMock(return_value=AnswerCount(answered=400) if exists else None)
    store.close = AsyncMock()
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None, gcp_project_id="PRIVATE"))
    monkeypatch.setattr(cli, "FirestoreAnswerCounter", lambda _: store)
    as_of = datetime(2026, 10, 21, tzinfo=UTC)
    args = argparse.Namespace(baseline=370, baseline_as_of=as_of, initialize=initialize)
    assert await cli.run(args) == 0
    if initialize:
        store.initialize.assert_called_once_with(370, as_of)
    else:
        store.initialize.assert_not_called()
    store.close.assert_awaited_once()
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_operator_cannot_overwrite_existing_counter(monkeypatch):
    import argparse
    from unittest.mock import AsyncMock

    from scripts import initialize_answer_count as cli

    store = MagicMock()
    store.read = AsyncMock(return_value=AnswerCount(answered=400))
    store.close = AsyncMock()
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None, gcp_project_id="test"))
    monkeypatch.setattr(cli, "FirestoreAnswerCounter", lambda _: store)
    with pytest.raises(ValueError, match="already exists"):
        await cli.run(
            argparse.Namespace(
                baseline=370, baseline_as_of=datetime(2026, 10, 21, tzinfo=UTC), initialize=True
            )
        )
    store.initialize.assert_not_called()
    store.close.assert_awaited_once()
