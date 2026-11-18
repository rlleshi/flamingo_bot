from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pytest

from flamingo_bot.models import ChunkRecord
from flamingo_bot.planning import MAX_METADATA_MATCHES, SearchPlan
from flamingo_bot.search_index import (
    SNAPSHOT_PART_BYTES,
    SearchIndex,
    chunk_month,
    keyword_tokens,
    pack_snapshot,
    unpack_snapshot,
)


def row(
    chunk_id: str,
    *,
    source_id: str = "diaspora-zbarkon",
    title: str = "Title",
    text: str = "Evidence text.",
    record_id: str | None = None,
    chunk_index: int = 0,
    content_type: str = "markdown",
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "source_id": source_id,
        "source_label": source_id,
        "title": title,
        "text": text,
        "canonical_url": f"https://example.org/{chunk_id}",
        "legal_status": None,
        "content_type": content_type,
        "chunk_index": chunk_index,
        "metadata": {"record_id": record_id or chunk_id, **metadata},
    }


def index_of(rows: list[dict[str, Any]], vectors: list[list[float]]) -> SearchIndex:
    return SearchIndex("generation-test", rows, np.asarray(vectors, dtype=np.float32))


def test_keyword_tokens_fold_albanian_letters_and_meet_case_endings() -> None:
    tokens = keyword_tokens("Protestuesit e lënduar dhe të plagosur")
    assert tokens == ["protes", "lendua", "plagos"]
    assert keyword_tokens("PLAGOSURIT u lënduan")[0] == keyword_tokens("plagosur")[0]
    assert keyword_tokens("Çështja") == ["ceshtj"]


def test_keyword_search_ranks_matches_and_returns_one_hit_per_record() -> None:
    rows = [
        row("a1", title="Policia", text="Protestues të lënduar nga policia.", record_id="a"),
        row("a2", title="Policia", text="Protestues të lënduar, vazhdim.", record_id="a"),
        row("b", title="Kuvendi", text="Seanca e Kuvendit."),
        row("c", title="Spitali", text="Dy protestues të plagosur në spital."),
    ]
    index = index_of(rows, [[1.0, 0.0]] * 4)
    hits = index.keyword_search("protestuesit e lenduar", 10)
    assert [hit.metadata["record_id"] for hit in hits] == ["a", "c"]
    assert all(hit.metadata["retrieval_method"] == "bm25" for hit in hits)
    assert index.keyword_search("fjalë që mungon", 10) == []


def test_vector_search_is_exact_per_source_and_respects_threshold_and_limit() -> None:
    rows = [
        row("near", source_id="lapsi"),
        row("middle", source_id="lapsi"),
        row("far", source_id="lapsi"),
        row("other-source", source_id="citizens"),
    ]
    index = index_of(rows, [[1.0, 0.0], [0.8, 0.6], [0.0, 1.0], [1.0, 0.0]])
    hits = index.search([1.0, 0.0], limit=5, distance_threshold=0.65, source_id="lapsi")
    assert [hit.chunk_id for hit in hits] == ["near", "middle"]
    assert hits[0].distance == pytest.approx(0.0, abs=1e-6)
    assert hits[1].distance == pytest.approx(0.2, abs=1e-6)
    assert [hit.chunk_id for hit in index.search([1, 0], limit=1, distance_threshold=2)] == ["near"]
    assert index.search([1.0, 0.0], limit=5, distance_threshold=0.65, source_id="absent") == []


def test_distance_to_takes_the_closest_query() -> None:
    index = index_of([row("a")], [[0.0, 1.0]])
    assert index.distance_to("a", [[1.0, 0.0], [0.0, 2.0]]) == pytest.approx(0.0, abs=1e-6)
    assert index.distance_to("a", []) == 2.0


def test_metadata_lookup_follows_firestore_order_limits_and_the_chunk_zero_rule() -> None:
    rows = [row(f"day-{n:03d}", protest_day=7, event_date="2026-06-06") for n in range(40)]
    rows += [
        row("index-0", section="index", chunk_index=0),
        row("index-1", section="index", chunk_index=1),
        row("unapproved", source_id="elsewhere", section="index"),
    ]
    index = index_of(rows, [[1.0, 0.0]] * len(rows))
    plan = SearchPlan.fallback("Day 7").model_copy(update={"protest_days": [7]})
    anchors = index.metadata_passages(plan, [[1.0, 0.0]])
    # Firestore returns the first 32 matches in document-ID order, then the anchor cap applies.
    assert all(int(item.chunk_id.split("-")[1]) < MAX_METADATA_MATCHES for item in anchors)
    assert all(item.metadata["retrieval_method"] == "metadata-anchor" for item in anchors)
    index_plan = SearchPlan.fallback("Peak").model_copy(update={"participation_section": "index"})
    assert [item.chunk_id for item in index.metadata_passages(index_plan, [[1, 0]])] == ["index-0"]


def test_metadata_equality_never_matches_across_types() -> None:
    index = index_of([row("text-day", protest_day="7")], [[1.0, 0.0]])
    plan = SearchPlan.fallback("Day 7").model_copy(update={"protest_days": [7]})
    assert index.metadata_passages(plan, [[1.0, 0.0]]) == []


def test_day_notes_keep_the_closest_language_version_and_spread_the_budget_by_month() -> None:
    rows = []
    vectors = []
    for day, date in ((1, "2026-06-01"), (2, "2026-06-02"), (3, "2026-07-01"), (4, "2026-08-01")):
        for language, vector in (("sq", [1.0, 0.0]), ("en", [0.6, 0.8])):
            rows.append(row(f"d{day}-{language}", section="day", protest_day=day, event_date=date))
            vectors.append(vector)
    index = index_of(rows, vectors)
    notes = index.day_notes([[1.0, 0.0]], start="2026-06-01", end="2026-07-31", budget=2)
    assert [note.chunk_id for note in notes] == ["d1-sq", "d3-sq"]
    assert index.day_notes([[1.0, 0.0]], start=None, end=None, budget=0) == []


def test_chunk_month_skips_free_text_dates() -> None:
    from flamingo_bot.models import RetrievedChunk

    def chunk(**metadata: Any) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id="x",
            source_id="s",
            source_label="s",
            title="t",
            text="x",
            canonical_url="https://example.org",
            distance=0.1,
            metadata=metadata,
        )

    free_text = chunk(reported_event_date="early October", research_date="2026-10-20")
    assert chunk_month(free_text) == "2026-10"
    assert chunk_month(chunk()) == "undated"


def chunk_record(number: int, text: str, dimensions: int = 4) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"chunk-{number}",
        document_id=f"document-{number}",
        source_id="diaspora-zbarkon",
        source_label="Diaspora Zbarkon",
        repository_path="/repo",
        relative_path="page.md",
        revision="abc",
        canonical_url="https://example.org/page",
        language="sq",
        content_type="markdown",
        title=f"Title {number}",
        text=text,
        metadata={"section": "day", "protest_day": number},
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        token_count=10,
        chunk_index=0,
        embedding=[float(number + 1), 1.0, 0.0, 0.5][:dimensions],
    )


def test_snapshot_round_trip_rebuilds_the_same_index() -> None:
    chunks = [chunk_record(number, f"Teksti {number} ë ç") for number in range(5)]
    manifest, records, vectors = pack_snapshot(chunks, 4)
    index = unpack_snapshot("generation-test", manifest, records, vectors)
    assert [item["chunk_id"] for item in index.records] == [chunk.chunk_id for chunk in chunks]
    assert index.records[2]["metadata"] == {"section": "day", "protest_day": 2}
    hit = index.search([3.0, 1.0, 0.0, 0.5], limit=1, distance_threshold=0.01)
    assert hit[0].chunk_id == "chunk-2"


def test_snapshot_parts_fit_firestore_documents_and_corruption_is_detected() -> None:
    rng = np.random.default_rng(7)
    big = [
        chunk_record(number, f"Teksti {number}").model_copy(
            update={"embedding": [float(value) for value in rng.random(1024)]}
        )
        for number in range(300)
    ]
    manifest, records, vectors = pack_snapshot(big, 1024)
    assert all(len(part) <= SNAPSHOT_PART_BYTES for part in records + vectors)
    assert manifest["vector_parts"] == len(vectors) > 1
    damaged = [vectors[0][:-1] + b"\x00", *vectors[1:]]
    with pytest.raises(ValueError, match="hash"):
        unpack_snapshot("generation-test", manifest, records, damaged)
    with pytest.raises(ValueError, match="missing parts"):
        unpack_snapshot("generation-test", manifest, records, vectors[:-1])


def test_snapshot_refuses_chunks_without_embeddings() -> None:
    chunk = chunk_record(1, "Teksti").model_copy(update={"embedding": None})
    with pytest.raises(ValueError):
        pack_snapshot([chunk], 4)


def test_day_notes_skip_zero_queries_key_undated_days_by_date_and_cap_the_budget() -> None:
    rows = [
        row("a", section="day", event_date="2026-06-01"),
        row("b", section="day", event_date="2026-07-01"),
        row("c", section="day", event_date="2026-08-01"),
    ]
    index = index_of(rows, [[1.0, 0.0], [0.9, 0.1], [0.5, 0.5]])
    notes = index.day_notes([[0.0, 0.0], [1.0, 0.0]], start=None, end=None, budget=2)
    # Three days without a protest-day number stay three days; two months fit the budget of
    # two, so the closest days are kept and the zero query is ignored.
    assert [note.chunk_id for note in notes] == ["a", "b"]
    assert all(note.distance < 0.2 for note in notes)
    assert index.day_notes([[0.0, 0.0]], start=None, end=None, budget=2) == []
