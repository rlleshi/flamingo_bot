"""Summary records computed at publication from the generation's own structured records.

No model writes these. They are counts, rankings and averages over the Pulsi day lines, the
protest map's city records and the Flamingo Dossier's records, so every figure traces to
published data. They answer questions that need arithmetic over many records (the biggest
days, a month's average, protests per country, cases per legal status), which no single
retrieved passage holds.

Each record is one chunk, built directly rather than through the chunker: the Dossier status
record lists every case title and is longer than the chunker's budget.
"""

from __future__ import annotations

import hashlib
import logging
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

from flamingo_bot.chunking import LocalTokenEstimator
from flamingo_bot.models import ChunkRecord

logger = logging.getLogger("flamingo_bot.summaries")

SUMMARY_VERSION = "1.0.0"
#: Summary records bypass the chunker's 700-token budget (the Dossier status list is about
#: 1,300 tokens); a record past this size is skipped rather than embedded.
MAX_SUMMARY_TOKENS = 6000

PULSI_URL = "https://www.diaspora-zbarkon.com/pulsi"
MAP_URL = "https://pomodoren.github.io/flamingo-map/"
DOSSIER_URL = "https://flamingo-revolution.github.io/flamingo-dossier/"

_MONTHS_SQ = [
    "janar", "shkurt", "mars", "prill", "maj", "qershor",
    "korrik", "gusht", "shtator", "tetor", "nëntor", "dhjetor",
]  # fmt: skip
_MONTHS_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip
_DAY_LINE = re.compile(
    r"Day (\d+), (\d{4}-\d\d-\d\d) \(([^)]*)\)(, [^:]*)?: (.*?)\.\s*(?:\n|$)"
)
_PEAK = re.compile(r"\bpeak ([\d.]+)")
_MEAN = re.compile(r"\bmean ([\d.]+)")
_CITY = re.compile(r"City: (.+?), ([^,\n]+)\n")
_CITY_COUNT = re.compile(r"Protest records for this city: (\d+)")
_PROTEST_DATE = re.compile(r"Date: (\d{4}-\d\d-\d\d)")
_DOSSIER_STATUS = {
    "convicted": "convicted / me dënim",
    "investigation": "under investigation / nën hetim",
    "no-process": "no legal process / pa proces ligjor",
    "unverified": "unverified claims / pretendime të paverifikuara",
}


def _label(date: str) -> str:
    year, month, day = (int(part) for part in date.split("-"))
    return (
        f"{day} {_MONTHS_SQ[month - 1]} {year} / {day} {_MONTHS_EN[month - 1]} {year}"
    )


def _record(
    name: str,
    *,
    origin: ChunkRecord,
    source_id: str,
    source_label: str,
    title: str,
    text: str,
    url: str,
    content_type: str,
    section: str,
    path: str,
) -> ChunkRecord:
    document_id = f"stats-{name}"
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ChunkRecord(
        chunk_id=hashlib.sha256(f"{document_id}\x000\x00{content_hash}".encode()).hexdigest()[:32],
        document_id=document_id,
        source_id=source_id,
        source_label=source_label,
        repository_path=origin.repository_path,
        relative_path=path,
        revision=origin.revision,
        canonical_url=url,
        language="sq",
        content_type=content_type,
        title=title,
        text=text,
        legal_status=None,
        metadata={
            "section": section,
            "record_id": document_id,
            "source_kind": "computed-statistics",
            "summary_version": SUMMARY_VERSION,
        },
        content_hash=content_hash,
        token_count=LocalTokenEstimator().count(text),
        chunk_index=0,
    )


def _pulsi(chunks: Sequence[ChunkRecord]) -> list[ChunkRecord]:
    index = sorted(
        (chunk for chunk in chunks if chunk.title.startswith("Pulsi: indeksi")),
        key=lambda chunk: chunk.chunk_index,
    )
    days: dict[int, dict[str, Any]] = {}
    for chunk in index:
        for number, date, _, saturday, values in _DAY_LINE.findall(chunk.text):
            peak = _PEAK.search(values)
            mean = _MEAN.search(values)
            days[int(number)] = {
                "day": int(number),
                "date": date,
                "saturday": "Saturday" in saturday,
                "peak": float(peak.group(1)) if peak else None,
                "mean": float(mean.group(1)) if mean else "not published",
            }
    every = [days[key] for key in sorted(days)]
    with_figure = [day for day in every if day["peak"] is not None]
    if not index or not with_figure:
        return []
    without_figure = [day for day in every if day["peak"] is None]
    first, last = every[0], every[-1]
    anchor_day = index[0].metadata.get("index_100_day", 21)
    anchor_date = index[0].metadata.get("index_100_date", "2026-06-20")
    anchor_year, anchor_month, anchor_dom = (int(part) for part in str(anchor_date).split("-"))
    head = (
        "Source: Diaspora Zbarkon\n"
        "Document: Pulsi: statistikat e indeksit / Pulsi index statistics\n"
        "Computed from the published day-by-day Pulsi index (not a number of people; "
        f"100 = day {anchor_day}, {anchor_dom} {_MONTHS_EN[anchor_month - 1]} {anchor_year}). "
        "Llogaritur nga indeksi ditor i publikuar (jo numër njerëzish; "
        f"100 = dita {anchor_day}, {anchor_dom} {_MONTHS_SQ[anchor_month - 1]} {anchor_year}).\n\n"
    )

    def line(day: dict[str, Any]) -> str:
        saturday = ", e shtunë / Saturday" if day["saturday"] else ""
        return (
            f"day {day['day']} ({_label(day['date'])}{saturday}): "
            f"peak {day['peak']}, mean {day['mean']}"
        )

    top = sorted(with_figure, key=lambda day: -day["peak"])
    low = sorted(with_figure, key=lambda day: day["peak"])
    duration = (
        head
        + f"Duration / Kohëzgjatja: {len(every)} protest days recorded, from day 1 "
        f"({_label(first['date'])}) to day {last['day']} ({_label(last['date'])}); "
        f"{len(every)} ditë proteste. {len(with_figure)} days carry a published figure; "
        f"{len(without_figure)} do not "
        f"(days {', '.join(str(day['day']) for day in without_figure)}).\n\n"
        "Top 10 days by peak / 10 ditët me pikun më të lartë:\n"
        + "\n".join(f"{rank}. {line(day)}" for rank, day in enumerate(top[:10], 1))
        + "\n\nLowest 5 days by peak / 5 ditët me pikun më të ulët:\n"
        + "\n".join(f"{rank}. {line(day)}" for rank, day in enumerate(low[:5], 1))
    )
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for day in with_figure:
        by_month[day["date"][:7]].append(day)
    rows = []
    for year_month, month_days in sorted(by_month.items()):
        biggest = max(month_days, key=lambda day: day["peak"])
        year, month = year_month.split("-")
        rows.append(
            f"{_MONTHS_SQ[int(month) - 1]} / {_MONTHS_EN[int(month) - 1]} {year}: "
            f"{len(month_days)} days with a figure, average peak "
            f"{statistics.mean(day['peak'] for day in month_days):.2f}, biggest day "
            f"{biggest['day']} ({_label(biggest['date'])}) at {biggest['peak']}"
        )
    since_october = sorted(
        (day for day in with_figure if day["date"] >= "2026-10-01"), key=lambda day: -day["peak"]
    )[:3]
    months = (
        head
        + "By month / Sipas muajve (average of daily peaks and the biggest day of each month):\n"
        + "\n".join(rows)
        + "\n\nBiggest days since 1 October / Ditët më të mëdha që nga 1 tetori:\n"
        + "\n".join(line(day) for day in since_october)
    )
    saturdays = [day["peak"] for day in with_figure if day["saturday"]]
    others = [day["peak"] for day in with_figure if not day["saturday"]]
    at_ten = [day for day in with_figure if day["peak"] >= 10]
    records = [duration, months]
    if saturdays and others and at_ten:
        records.append(
            head
            + f"Saturdays and other days / E shtuna dhe ditët e tjera: {len(saturdays)} "
            f"Saturdays with a figure, average peak {statistics.mean(saturdays):.2f}, median "
            f"{statistics.median(saturdays):.2f}; {len(others)} other days, average peak "
            f"{statistics.mean(others):.2f}, median {statistics.median(others):.2f}. "
            f"The three biggest days ({', '.join(str(day['day']) for day in top[:3])}) were "
            f"all Saturdays: {all(day['saturday'] for day in top[:3])}.\n\n"
            f"Days at 10 or above / Ditët me pik 10 ose më shumë: {len(at_ten)} in all; the "
            f"most recent was day {at_ten[-1]['day']} ({_label(at_ten[-1]['date'])}) at "
            f"{at_ten[-1]['peak']}. Since 1 September: "
            + ", ".join(
                f"day {day['day']} ({day['date']}, {day['peak']})"
                for day in at_ten
                if day["date"] >= "2026-09-01"
            )
            + "."
        )
    return [
        _record(
            f"pulsi-{number}",
            origin=index[0],
            source_id="diaspora-zbarkon",
            source_label="Diaspora Zbarkon",
            title="Pulsi: statistikat e indeksit / Pulsi index statistics",
            text=text,
            url=PULSI_URL,
            content_type="participation-statistics",
            section="index",
            path="computed/pulsi-stats",
        )
        for number, text in enumerate(records, 1)
    ]


def _map(chunks: Sequence[ChunkRecord]) -> list[ChunkRecord]:
    cities: list[tuple[str, str, int]] = []
    origin: ChunkRecord | None = None
    for chunk in chunks:
        if chunk.content_type != "map-city":
            continue
        city_match = _CITY.search(chunk.text)
        count = _CITY_COUNT.search(chunk.text)
        if city_match and count:
            cities.append(
                (city_match.group(1), city_match.group(2).strip(), int(count.group(1)))
            )
            origin = origin or chunk
    if origin is None:
        return []
    dates = []
    for chunk in chunks:
        if chunk.content_type != "map-protest" or "Place:" not in chunk.text:
            continue
        found = _PROTEST_DATE.search(chunk.text)
        if found and "Albania" not in chunk.text.split("Place:")[1].split("\n")[0]:
            dates.append(found.group(1))
    dates.sort()
    abroad = [city for city in cities if city[1] != "Albania"]
    home = [city for city in cities if city[1] == "Albania"]
    by_country: Counter[str] = Counter()
    for city in abroad:
        by_country[city[1]] += city[2]
    text = (
        "Source: Harta e Protestave\n"
        "Document: Harta e Protestave: statistikat sipas shteteve dhe qyteteve / Protest map "
        "statistics by country and city\n"
        "Computed from the map's city records (protest records per city). Llogaritur nga "
        "regjistrat e qyteteve të hartës.\n\n"
        f"Abroad / Jashtë Shqipërisë: {sum(city[2] for city in abroad)} protest records in "
        f"{len(abroad)} cities in {len(by_country)} countries; first "
        f"{dates[0] if dates else '?'}, latest {dates[-1] if dates else '?'}.\n"
        f"In Albania / Në Shqipëri: {sum(city[2] for city in home)} protest records in "
        f"{len(home)} places.\n\n"
        "Countries by protest records / Shtetet sipas protestave:\n"
        + "\n".join(
            f"{rank}. {country}: {count}"
            for rank, (country, count) in enumerate(
                sorted(by_country.items(), key=lambda item: (-item[1], item[0])), 1
            )
        )
        + "\n\nCities abroad with the most protests / Qytetet jashtë Shqipërisë me më shumë "
        "protesta:\n"
        + "\n".join(
            f"{rank}. {city[0]} ({city[1]}): {city[2]}"
            for rank, city in enumerate(
                sorted(abroad, key=lambda city: (-city[2], city[0]))[:12], 1
            )
        )
        + "\n\nPlaces in Albania / Vendet në Shqipëri:\n"
        + ", ".join(
            f"{city[0]} {city[2]}" for city in sorted(home, key=lambda city: (-city[2], city[0]))
        )
    )
    return [
        _record(
            "map",
            origin=origin,
            source_id="flamingo-revolution",
            source_label="Harta e Protestave",
            title="Harta e Protestave: statistikat sipas shteteve dhe qyteteve / Protest map "
            "statistics",
            text=text,
            url=MAP_URL,
            content_type="map-overview",
            section="map",
            path="computed/map-stats",
        )
    ]


def _dossier(chunks: Sequence[ChunkRecord]) -> list[ChunkRecord]:
    documents: dict[str, ChunkRecord] = {}
    for chunk in chunks:
        if chunk.source_id == "flamingo-dossier":
            documents.setdefault(chunk.document_id, chunk)
    if not documents:
        return []
    by_status: dict[str, list[str]] = defaultdict(list)
    by_category: dict[str, list[str]] = defaultdict(list)
    for chunk in documents.values():
        by_status[chunk.legal_status or "unknown"].append(chunk.title)
        by_category[str(chunk.metadata.get("category", "?"))].append(chunk.title)
    head = (
        "Source: Dosja Flamingo\n"
        "Document: Dosja Flamingo: statistikat sipas statusit ligjor dhe kategorisë / Flamingo "
        "Dossier statistics\n"
        "Computed from the dossier's records and the legal status each record carries. "
        "'Convicted' means the record reports a final conviction of the people it names, not "
        "of everyone mentioned in it.\n\n"
    )
    status = (
        head
        + f"{len(documents)} records in all / {len(documents)} raste gjithsej.\n\n"
        + "\n\n".join(
            f"{_DOSSIER_STATUS.get(name, name)}: {len(titles)}\n- " + "\n- ".join(sorted(titles))
            for name, titles in sorted(by_status.items(), key=lambda item: (-len(item[1]), item[0]))
        )
    )
    categories = (
        head
        + "By category / Sipas kategorisë:\n"
        + "\n".join(
            f"{name}: {len(titles)}"
            for name, titles in sorted(
                by_category.items(), key=lambda item: (-len(item[1]), item[0])
            )
        )
    )
    origin = next(iter(documents.values()))
    return [
        _record(
            "dossier-status",
            origin=origin,
            source_id="flamingo-dossier",
            source_label="Dosja Flamingo",
            title="Dosja Flamingo: statistikat sipas statusit ligjor / Dossier statistics by "
            "legal status",
            text=status,
            url=DOSSIER_URL,
            content_type="dossier-statistics",
            section="dossier",
            path="computed/dossier-stats",
        ),
        _record(
            "dossier-category",
            origin=origin,
            source_id="flamingo-dossier",
            source_label="Dosja Flamingo",
            title="Dosja Flamingo: statistikat sipas kategorisë / Dossier statistics by category",
            text=categories,
            url=DOSSIER_URL,
            content_type="dossier-statistics",
            section="dossier",
            path="computed/dossier-stats",
        ),
    ]


def summary_chunks(chunks: Sequence[ChunkRecord]) -> list[ChunkRecord]:
    """Summary records for the sources present; a source without data contributes none."""
    records = []
    for record in [*_pulsi(chunks), *_map(chunks), *_dossier(chunks)]:
        if record.token_count > MAX_SUMMARY_TOKENS:
            logger.warning(
                "summary_record_skipped record=%s tokens=%d", record.document_id, record.token_count
            )
            continue
        records.append(record)
    return records
