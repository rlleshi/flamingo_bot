from __future__ import annotations

import hashlib
from typing import Any

from flamingo_bot.models import ChunkRecord
from flamingo_bot.summaries import DOSSIER_URL, SUMMARY_VERSION, summary_chunks


def chunk(
    chunk_id: str,
    text: str,
    *,
    title: str = "Title",
    source_id: str = "diaspora-zbarkon",
    content_type: str = "markdown",
    chunk_index: int = 0,
    legal_status: str | None = None,
    document_id: str | None = None,
    **metadata: Any,
) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        document_id=document_id or f"document-{chunk_id}",
        source_id=source_id,
        source_label=source_id,
        repository_path="/repo",
        relative_path="page.md",
        revision="rev-1",
        canonical_url="https://example.org/page",
        language="sq",
        content_type=content_type,
        title=title,
        text=text,
        legal_status=legal_status,
        metadata=metadata,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        token_count=10,
        chunk_index=chunk_index,
    )


PULSI_LINES = (
    "Day 1, 2026-05-31 (31 maj 2026 / 31 May 2026): peak 4.78, mean 3.89, median 4.22.\n"
    "Day 7, 2026-06-06 (6 qershor 2026 / 6 June 2026), e shtunë / Saturday: peak 50.0, "
    "mean 9.1, median 8.0.\n"
    "Day 9, 2026-06-08 (8 qershor 2026 / 8 June 2026): peak not published, mean not "
    "published, median not published.\n"
)
PULSI_MORE = (
    "Day 120, 2026-09-27 (27 shtator 2026 / 27 September 2026), e shtunë / Saturday: "
    "peak 12.5, mean 4.0, median 3.9.\n"
    "Day 140, 2026-10-17 (17 tetor 2026 / 17 October 2026): peak 2.5, mean 2.0, median 1.9.\n"
)


def pulsi_chunks() -> list[ChunkRecord]:
    title = "Pulsi: indeksi i pjesëmarrjes ditë pas dite"
    return [
        chunk("p1", PULSI_MORE, title=title, chunk_index=1, section="index"),
        chunk("p0", PULSI_LINES, title=title, chunk_index=0, section="index"),
    ]


def test_pulsi_summaries_rank_days_and_count_missing_figures() -> None:
    records = summary_chunks(pulsi_chunks())
    assert [record.document_id for record in records] == [
        "stats-pulsi-1",
        "stats-pulsi-2",
        "stats-pulsi-3",
    ]
    duration, months, saturdays = (record.text for record in records)
    assert "5 protest days recorded, from day 1" in duration
    assert "to day 140 (17 tetor 2026 / 17 October 2026)" in duration
    assert "4 days carry a published figure; 1 do not (days 9)" in duration
    assert (
        "1. day 7 (6 qershor 2026 / 6 June 2026, e shtunë / Saturday): peak 50.0, mean 9.1"
        in duration
    )
    assert "qershor / June 2026: 1 days with a figure, average peak 50.00" in months
    assert "Biggest days since 1 October / Ditët më të mëdha që nga 1 tetori:\nday 140" in months
    assert "2 Saturdays with a figure, average peak 31.25" in saturdays
    assert "Since 1 September: day 120 (2026-09-27, 12.5)." in saturdays
    for record in records:
        assert record.metadata == {
            "section": "index",
            "record_id": record.document_id,
            "source_kind": "computed-statistics",
            "summary_version": SUMMARY_VERSION,
        }
        assert record.chunk_index == 0
        assert record.revision == "rev-1"
        assert record.content_hash == hashlib.sha256(record.text.encode()).hexdigest()


def test_map_summary_counts_protests_by_country_and_city() -> None:
    def city(name: str, country: str, count: int) -> ChunkRecord:
        text = (
            f"Source: Harta e Protestave\nCity: {name}, {country}\n\n"
            f"Protest records for this city: {count}.\n"
        )
        return chunk(f"city-{name}", text, source_id="flamingo-revolution", content_type="map-city")

    def protest(name: str, place: str, date: str) -> ChunkRecord:
        text = f"Source: Harta e Protestave\nProtest: {name}\nPlace: {place}\nDate: {date}\n"
        return chunk(
            f"protest-{name}", text, source_id="flamingo-revolution", content_type="map-protest"
        )

    records = summary_chunks(
        [
            city("Berlin", "Germany", 5),
            city("Munich", "Germany", 3),
            city("Paris", "France", 8),
            city("Tirana", "Albania", 40),
            protest("one", "Berlin, Germany", "2026-06-02"),
            protest("two", "Paris, France", "2026-11-01"),
            protest("home", "Tirana, Albania", "2026-05-31"),
        ]
    )
    (record,) = records
    assert record.content_type == "map-overview"
    assert (
        "16 protest records in 3 cities in 2 countries; first 2026-06-02, latest 2026-11-01"
        in record.text
    )
    # Ties are ordered by name, so the text does not depend on chunk order.
    assert "1. France: 8\n2. Germany: 8" in record.text
    assert "In Albania / Në Shqipëri: 40 protest records in 1 places." in record.text


def test_dossier_summaries_count_legal_status_and_category_with_the_real_address() -> None:
    def case(number: int, status: str, category: str) -> ChunkRecord:
        return chunk(
            f"case-{number}",
            "Case text.",
            title=f"Case {number}",
            source_id="flamingo-dossier",
            legal_status=status,
            document_id=f"case-document-{number}",
            category=category,
        )

    records = summary_chunks(
        [
            case(1, "convicted", "health"),
            case(2, "investigation", "health"),
            case(3, "convicted", "land"),
        ]
    )
    status, category = records
    assert status.canonical_url == category.canonical_url == DOSSIER_URL
    assert "3 records in all" in status.text
    assert "convicted / me dënim: 2\n- Case 1\n- Case 3" in status.text
    assert "health: 2\nland: 1" in category.text


def test_sources_without_data_add_no_summaries() -> None:
    assert summary_chunks([chunk("page", "A page without statistics.")]) == []


def test_the_index_reference_comes_from_the_data_and_peaks_alone_count() -> None:
    title = "Pulsi: indeksi i pjesëmarrjes ditë pas dite"
    text = (
        "Day 1, 2026-05-31 (31 maj 2026 / 31 May 2026): peak 4.0, mean 3.0, median 3.0.\n"
        "Day 2, 2026-06-01 (1 qershor 2026 / 1 June 2026): peak 6.0, mean not published, "
        "median not published.\n"
    )
    records = summary_chunks(
        [
            chunk(
                "p0",
                text,
                title=title,
                section="index",
                index_100_day=30,
                index_100_date="2026-06-29",
            )
        ]
    )
    assert "100 = day 30, 29 June 2026" in records[0].text
    assert "100 = dita 30, 29 qershor 2026" in records[0].text
    assert "2 days carry a published figure; 0 do not" in records[0].text
    top = "1. day 2 (1 qershor 2026 / 1 June 2026): peak 6.0, mean not published"
    assert top in records[0].text


def test_oversized_summary_records_are_skipped(monkeypatch: Any, caplog: Any) -> None:
    from flamingo_bot import summaries

    monkeypatch.setattr(summaries, "MAX_SUMMARY_TOKENS", 10)
    assert summary_chunks(pulsi_chunks()) == []
    assert "summary_record_skipped" in caplog.text
