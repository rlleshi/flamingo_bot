from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Any

import pytest

from flamingo_bot.config import Settings
from flamingo_bot.errors import ProviderError
from flamingo_bot.models import RetrievedChunk
from flamingo_bot.planning import (
    BROAD_FALLBACK_SELECTED,
    FollowUpQueries,
    QueryExpansion,
    QuestionScope,
    SearchPlan,
    scope_period,
)
from flamingo_bot.prompting import BROAD_ANSWER_NOTE, NAMING_RULE
from flamingo_bot.providers.memory import (
    InMemoryVectorStore,
    ScriptedAnswerGenerator,
    ScriptedPlanner,
    ScriptedRanker,
)
from flamingo_bot.retrieval import (
    ChatService,
    IndexCache,
    broad_pool,
    spread_by_month,
    whole_months,
)
from flamingo_bot.search_index import SearchIndex

AS_OF = date(2026, 11, 18)
BROAD = QuestionScope(broad=True, period_start="2026-06-01", period_end="2026-11-18")
QUESTION = "Me fol per dhunen e policise gjate protestave"


def passage(chunk_id: str, *, title: str | None = None, **metadata: Any) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id=metadata.pop("source_id", "diaspora-zbarkon"),
        source_label="Diaspora Zbarkon",
        title=title or f"Report {chunk_id}",
        text=f"Policia dhe protestuesit, raport {chunk_id}.",
        canonical_url=f"https://example.org/{chunk_id}",
        distance=0.1,
        metadata={"record_id": chunk_id, **metadata},
    )


def archive() -> InMemoryVectorStore:
    records = [
        (passage("jun", event_date="2026-06-20"), [1.0, 0.0]),
        (passage("jul", event_date="2026-07-23"), [0.95, 0.05]),
        (passage("oct", event_date="2026-10-15"), [0.9, 0.1]),
        (passage("nov", event_date="2026-11-05"), [0.9, 0.12]),
        (passage("may", event_date="2026-05-20"), [1.0, 0.0]),  # before the period
        (
            passage("day-140", title="Pulsi day 140", section="day", protest_day=140,
                    event_date="2026-10-17"),
            [0.0, 1.0],  # far from every query: only the day-note lookup reaches it
        ),
    ]
    return InMemoryVectorStore(records)


class FixedEmbedder:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def embed_texts(self, texts: Any) -> list[list[float]]:
        self.texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]


def service(
    planner: ScriptedPlanner, ranker: ScriptedRanker, store: InMemoryVectorStore | None = None
) -> tuple[ChatService, FixedEmbedder, ScriptedAnswerGenerator]:
    embedder = FixedEmbedder()
    generator = ScriptedAnswerGenerator("Përmbledhje [S1].")
    return (
        ChatService(
            settings=Settings(_env_file=None),
            embedder=embedder,
            vector_store=store or archive(),
            answer_generator=generator,
            query_planner=planner,
            passage_ranker=ranker,
        ),
        embedder,
        generator,
    )


def broad_planner(**overrides: Any) -> ScriptedPlanner:
    options: dict[str, Any] = {
        "scope": BROAD,
        "expansion": QueryExpansion(
            passage="Policia përdori gaz lotsjellës.", queries=["plagosur"]
        ),
        "follow_up": FollowUpQueries(queries=["gaz lotsjellës tetor"]),
    }
    options.update(overrides)
    return ScriptedPlanner(SearchPlan.fallback("police violence at the protests"), **options)


@pytest.mark.asyncio
async def test_broad_questions_search_more_routes_and_get_a_longer_answer() -> None:
    planner = broad_planner()
    ranker = ScriptedRanker(broad_ids=["C1", "C2", "C3", "C4", "C5"])
    chat, embedder, generator = service(planner, ranker)
    citations, stream = await chat.stream(QUESTION, as_of=AS_OF)
    assert [part async for part in stream]
    assert embedder.texts == [
        "police violence at the protests",
        QUESTION,
        "Policia përdori gaz lotsjellës.",
        "plagosur",
        "gaz lotsjellës tetor",
    ]
    assert len(planner.expansion_calls) == len(planner.follow_up_calls) == 1
    assert ranker.calls == []
    prompt = json.loads(str(ranker.broad_calls[0]["prompt"]))
    titles = [item["title"] for item in prompt["passages"]]
    assert "Pulsi day 140" in titles  # a day note in the period, found by date not similarity
    assert "Report may" not in titles  # outside the visitor's period
    assert prompt["allowed"] == 30
    assert "Allowed: at most 30 IDs." in str(ranker.broad_calls[0]["instructions"])
    assert len(citations) == 5
    call = generator.calls[0]
    assert BROAD_ANSWER_NOTE in str(call["instructions"])
    assert str(call["instructions"]).endswith(NAMING_RULE)
    assert call["max_output_tokens"] == "3000"


@pytest.mark.asyncio
async def test_narrow_answers_keep_the_naming_rule_and_the_configured_length() -> None:
    planner = ScriptedPlanner(SearchPlan.fallback("Day 140"))
    chat, _, generator = service(planner, ScriptedRanker(["C1"]))
    citations, stream = await chat.stream("What happened on day 140?", as_of=AS_OF)
    assert [part async for part in stream]
    assert len(citations) == 1
    instructions = str(generator.calls[0]["instructions"])
    assert instructions.endswith(NAMING_RULE)
    assert BROAD_ANSWER_NOTE not in instructions
    assert generator.calls[0]["max_output_tokens"] is None
    assert planner.expansion_calls == []


@pytest.mark.asyncio
async def test_scope_failure_answers_as_a_narrow_question_without_logging_it(
    caplog: Any,
) -> None:
    planner = broad_planner(scope=ProviderError("provider detail"))
    ranker = ScriptedRanker(["C1"], broad_ids=["C1"])
    chat, _, _ = service(planner, ranker)
    citations, _ = await chat.stream(QUESTION, as_of=AS_OF)
    assert citations
    assert ranker.broad_calls == [] and len(ranker.calls) == 1
    assert "scope_fallback error=ProviderError" in caplog.text
    assert "provider detail" not in caplog.text
    assert QUESTION not in caplog.text


@pytest.mark.asyncio
async def test_expansion_and_follow_up_failures_still_answer_from_the_planner_queries(
    caplog: Any,
) -> None:
    planner = broad_planner(
        expansion=ProviderError("expansion detail"), follow_up=ProviderError("follow-up detail")
    )
    chat, embedder, _ = service(planner, ScriptedRanker(broad_ids=["C1", "C2"]))
    citations, _ = await chat.stream(QUESTION, as_of=AS_OF)
    assert len(citations) == 2
    assert embedder.texts == ["police violence at the protests", QUESTION]
    assert "expansion_fallback" in caplog.text and "follow_up_fallback" in caplog.text


@pytest.mark.asyncio
async def test_broad_selector_failure_spreads_the_pool_across_months(caplog: Any) -> None:
    chat, _, _ = service(broad_planner(), ScriptedRanker(broad_ids=ProviderError("busy")))
    citations, _ = await chat.stream(QUESTION, as_of=AS_OF)
    assert "broad_selector_fallback" in caplog.text
    assert 1 < len(citations) <= BROAD_FALLBACK_SELECTED
    assert {citation.title for citation in citations} >= {"Report jun", "Report oct"}


@pytest.mark.asyncio
async def test_broad_selection_keeps_at_most_three_reference_headlines() -> None:
    records = [
        (passage(f"ref{n}", event_date="2026-10-0" + str(n + 1), content_use="reference",
                 source_id="lapsi"), [1.0, 0.0])
        for n in range(5)
    ] + [(passage("full", event_date="2026-10-20"), [1.0, 0.0])]
    ranker = ScriptedRanker(broad_ids=[f"C{n}" for n in range(1, 7)])
    chat, _, _ = service(broad_planner(), ranker, InMemoryVectorStore(records))
    citations, _ = await chat.stream(QUESTION, as_of=AS_OF)
    assert len(citations) == 4
    assert sum(1 for citation in citations if citation.title.startswith("Report ref")) == 3


def test_broad_pool_puts_anchors_first_and_orders_the_rest_by_month() -> None:
    anchor = passage("anchor", event_date="2026-11-01", retrieval_method="metadata-anchor")
    fused = [passage(f"m{month}", event_date=f"2026-{month:02d}-10") for month in (9, 6, 7)]
    note = passage("note", event_date="2026-08-01", section="day")
    pool = broad_pool(fused=fused, anchors=[anchor], notes=[note])
    assert [chunk.chunk_id for chunk in pool] == ["anchor", "m6", "m7", "note", "m9"]


def test_spread_by_month_takes_each_month_in_turn() -> None:
    pool = [
        passage(f"{month}-{n}", event_date=f"2026-{month:02d}-1{n}")
        for month in (6, 7)
        for n in range(3)
    ]
    assert [chunk.chunk_id for chunk in spread_by_month(pool, 3)] == ["6-0", "7-0", "6-1"]


def test_scope_period_accepts_dates_and_months_and_ignores_text() -> None:
    month = QuestionScope(broad=True, period_start="2026-10", period_end="2026-10")
    assert scope_period(month) == ("2026-10-01", "2026-10-31")
    september = QuestionScope(broad=True, period_start="2026-09", period_end="2026-09")
    assert scope_period(september) == ("2026-09-01", "2026-09-30")  # a real last day
    february = QuestionScope(broad=True, period_start="2026-02", period_end="2026-02")
    assert scope_period(february) == ("2026-02-01", "2026-02-28")
    text = QuestionScope(broad=True, period_start="tetor", period_end="")
    assert scope_period(text) == (None, None)


@pytest.mark.asyncio
async def test_failed_reload_keeps_serving_the_loaded_generation() -> None:
    class FlakyStore(InMemoryVectorStore):
        fail = False

        async def load_index(self, generation_id: str) -> SearchIndex:
            if self.fail:
                raise ProviderError("unavailable")
            return await super().load_index(generation_id)

    store = FlakyStore([(passage("a"), [1.0, 0.0])])
    cache = IndexCache(store)
    first = await cache.for_generation("generation-1")
    store.fail = True
    assert await cache.for_generation("generation-2") is first
    with pytest.raises(ProviderError):
        await IndexCache(store).for_generation("generation-2")


class CountingStore(InMemoryVectorStore):
    def __init__(self, records: Any, *, fail: bool = False) -> None:
        super().__init__(records)
        self.loads = 0
        self.fail = fail
        self.release = asyncio.Event()

    async def load_index(self, generation_id: str) -> SearchIndex:
        self.loads += 1
        await self.release.wait()
        if self.fail:
            raise ProviderError("unavailable")
        return await super().load_index(generation_id)


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_load_that_survives_a_cancelled_request() -> None:
    store = CountingStore([(passage("a"), [1.0, 0.0])])
    cache = IndexCache(store)
    waiting = [asyncio.create_task(cache.for_generation("generation-1")) for _ in range(5)]
    await asyncio.sleep(0)
    waiting[0].cancel()  # a visitor goes away while the index loads
    store.release.set()
    results = await asyncio.gather(*waiting[1:])
    assert store.loads == 1
    assert all(result is results[0] for result in results)
    assert cache.loaded is results[0]


@pytest.mark.asyncio
async def test_a_failed_cold_load_fails_fast_until_the_retry_pause_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flamingo_bot import retrieval

    store = CountingStore([(passage("a"), [1.0, 0.0])], fail=True)
    store.release.set()
    cache = IndexCache(store)
    for _ in range(3):
        with pytest.raises(ProviderError):
            await cache.for_generation("generation-1")
    assert store.loads == 1
    monkeypatch.setattr(retrieval, "INDEX_RETRY_SECONDS", 0.0)
    store.fail = False
    assert len(await cache.for_generation("generation-1")) == 1
    assert store.loads == 2


@pytest.mark.asyncio
async def test_a_broad_selection_of_only_unknown_ids_falls_back(caplog: Any) -> None:
    chat, _, _ = service(broad_planner(), ScriptedRanker(broad_ids=["C999"]))
    citations, _ = await chat.stream(QUESTION, as_of=AS_OF)
    assert citations
    assert "broad_selector_fallback error=ValueError" in caplog.text


@pytest.mark.asyncio
async def test_a_failed_follow_up_embedding_keeps_the_first_pass(caplog: Any) -> None:
    class FailingSecondCall(FixedEmbedder):
        async def embed_texts(self, texts: Any) -> list[list[float]]:
            if self.texts:
                raise ProviderError("embedding unavailable")
            return await super().embed_texts(texts)

    planner = broad_planner()
    ranker = ScriptedRanker(broad_ids=["C1", "C2"])
    chat = ChatService(
        settings=Settings(_env_file=None),
        embedder=FailingSecondCall(),
        vector_store=archive(),
        answer_generator=ScriptedAnswerGenerator("Përmbledhje [S1]."),
        query_planner=planner,
        passage_ranker=ranker,
    )
    citations, _ = await chat.stream(QUESTION, as_of=AS_OF)
    assert len(citations) == 2
    assert "follow_up_fallback stage=embedding error=ProviderError" in caplog.text


def test_scope_period_ignores_impossible_and_reversed_dates() -> None:
    impossible = QuestionScope(broad=True, period_start="2026-02-31", period_end="2026-03-10")
    assert scope_period(impossible) == (None, "2026-03-10")
    reversed_range = QuestionScope(broad=True, period_start="2026-11-01", period_end="2026-10-01")
    assert scope_period(reversed_range) == (None, None)


def october_week() -> tuple[QuestionScope, InMemoryVectorStore]:
    """Three records from early October, closest to every query, and one inside 13-17 October."""
    records = [
        (passage(f"early{n}", event_date=f"2026-10-0{n + 1}"), [1.0, 0.0]) for n in range(3)
    ]
    records.append((passage("inside", event_date="2026-10-15"), [0.8, 0.6]))
    week = QuestionScope(broad=True, period_start="2026-10-13", period_end="2026-10-17")
    return week, InMemoryVectorStore(records)


@pytest.mark.asyncio
async def test_a_named_period_is_searched_before_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    from flamingo_bot import retrieval

    monkeypatch.setattr(retrieval, "BROAD_HITS_PER_QUERY", 2)
    monkeypatch.setattr(retrieval, "BROAD_MIN_DATED_PASSAGES", 1)
    week, store = october_week()
    ranker = ScriptedRanker(broad_ids=["C1"])
    chat, _, _ = service(broad_planner(scope=week), ranker, store)
    await chat.stream(QUESTION, as_of=AS_OF)
    prompt = json.loads(str(ranker.broad_calls[0]["prompt"]))
    assert [item["title"] for item in prompt["passages"]] == ["Report inside"]


@pytest.mark.asyncio
async def test_a_period_with_too_little_also_searches_its_whole_months(caplog: Any) -> None:
    caplog.set_level(logging.INFO, logger="flamingo_bot.retrieval")
    week, store = october_week()
    ranker = ScriptedRanker(broad_ids=["C1"])
    chat, _, _ = service(broad_planner(scope=week), ranker, store)
    await chat.stream(QUESTION, as_of=AS_OF)
    prompt = json.loads(str(ranker.broad_calls[0]["prompt"]))
    titles = {item["title"] for item in prompt["passages"]}
    assert titles == {"Report inside", "Report early0", "Report early1", "Report early2"}
    assert "broad_period dated=1 widened=True" in caplog.text


def test_whole_months_stretches_a_period_to_its_months() -> None:
    assert whole_months("2026-10-13", "2026-10-17") == ("2026-10-01", "2026-10-31")
    assert whole_months(None, "2026-11-18") == (None, "2026-11-30")
    assert whole_months("2026-09-07", None) == ("2026-09-01", None)


@pytest.mark.asyncio
async def test_the_selector_fallback_keeps_the_relevance_cutoff() -> None:
    records = [
        (passage("near", event_date="2026-10-15"), [1.0, 0.0]),
        (passage("far", event_date="2026-10-16"), [0.0, 1.0]),  # found by its words only
        (
            passage("day-140", title="Pulsi day 140", section="day", protest_day=140,
                    event_date="2026-10-17"),
            [0.0, 1.0],
        ),
    ]
    ranker = ScriptedRanker(broad_ids=ProviderError("busy"))
    chat, _, _ = service(broad_planner(), ranker, InMemoryVectorStore(records))
    citations, _ = await chat.stream(QUESTION, as_of=AS_OF)
    assert {citation.title for citation in citations} == {"Report near", "Pulsi day 140"}


@pytest.mark.asyncio
async def test_a_fallback_with_nothing_relevant_answers_without_evidence() -> None:
    records = [(passage("far", event_date="2026-10-16"), [0.0, 1.0])]
    ranker = ScriptedRanker(broad_ids=ProviderError("busy"))
    chat, _, generator = service(broad_planner(), ranker, InMemoryVectorStore(records))
    citations, stream = await chat.stream(QUESTION, as_of=AS_OF)
    assert citations == []
    assert [part async for part in stream]
    assert generator.calls == []
