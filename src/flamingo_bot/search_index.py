"""In-memory search over one published generation.

A ``SearchIndex`` holds every chunk of one generation with its embedding and answers, without
I/O: exact cosine vector search per source (the same results as Firestore's flat vector index),
exact metadata lookups with Firestore's ordering and limits, keyword (BM25) search, and the
Pulsi day notes of a period. Each request pins one index, so every lookup in it reads the same
generation.

The packed snapshot written at publication lets a serving instance load a generation from a
few large documents instead of decoding thousands of embedding arrays one by one.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

import numpy as np
import numpy.typing as npt

from flamingo_bot.models import ChunkRecord, RetrievedChunk
from flamingo_bot.planning import (
    MAX_ANCHORS,
    MAX_METADATA_MATCHES,
    SOURCE_IDS,
    SearchPlan,
    deduplicate,
    metadata_filters,
)

FloatMatrix = npt.NDArray[np.float32]

#: Fields kept for every chunk; the embedding is stored separately as one float32 matrix.
RECORD_FIELDS = (
    "chunk_id",
    "source_id",
    "source_label",
    "title",
    "text",
    "canonical_url",
    "legal_status",
    "content_type",
    "chunk_index",
    "metadata",
)

SNAPSHOT_FORMAT = 1
#: Firestore documents hold at most 1 MiB, field names and overhead included.
SNAPSHOT_PART_BYTES = 900_000
SNAPSHOT_DTYPE = "<f4"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_FOLD = str.maketrans({"ë": "e", "Ë": "e", "ç": "c", "Ç": "c"})
_STOP_WORDS = frozenset(
    """dhe e te të në ne nga per për me më që qe se si i u ka kanë kane ishte është eshte janë
    jane do the of and to in a for on is are was were by with at from that this as it be or an
    not""".translate(_FOLD).split()
)
_WORD = re.compile(r"[a-z0-9]+")


def record_key(chunk: RetrievedChunk) -> tuple[str, str]:
    """One key per source record, shared by its chunks and language versions' passages."""
    return (chunk.source_id, str(chunk.metadata.get("record_id") or chunk.title))


def chunk_month(chunk: RetrievedChunk) -> str:
    """The month a passage is about, from the first ISO date field; some records hold text."""
    for field in ("event_date", "reported_event_date", "research_date", "published_date"):
        value = chunk.metadata.get(field)
        if isinstance(value, str) and _ISO_DATE.match(value):
            return value[:7]
    return "undated"


def keyword_tokens(text: str, prefix: int = 6) -> list[str]:
    """Lowercase words with ë/ç folded, cut to a prefix so Albanian case endings meet."""
    folded = unicodedata.normalize("NFC", text).lower().translate(_FOLD)
    return [
        word[:prefix] for word in _WORD.findall(folded) if word not in _STOP_WORDS and len(word) > 1
    ]


def to_chunk(
    record: dict[str, Any], distance: float, extra: dict[str, Any] | None = None
) -> RetrievedChunk:
    metadata = dict(record.get("metadata") or {})
    if extra:
        metadata.update(extra)
    return RetrievedChunk(
        chunk_id=str(record["chunk_id"]),
        source_id=str(record["source_id"]),
        source_label=str(record["source_label"]),
        title=str(record["title"]),
        text=str(record["text"]),
        canonical_url=str(record["canonical_url"]),
        legal_status=(str(record["legal_status"]) if record.get("legal_status") else None),
        distance=max(0.0, float(distance)),
        metadata=metadata,
    )


def _field(record: dict[str, Any], dotted: str) -> Any:
    value: Any = record
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _matches(value: Any, operation: str, wanted: Any) -> bool:
    """Firestore filter semantics: equality across types is false, ranges need one type."""
    if value is None:
        return False
    if operation == "==":
        return bool(value == wanted)
    if type(value) is not type(wanted):
        return False
    if operation == ">=":
        return bool(value >= wanted)
    if operation == "<":
        return bool(value < wanted)
    raise ValueError(f"Unsupported metadata operation: {operation}")


def _unit(vector: Sequence[float]) -> FloatMatrix:
    query = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(query)
    if not np.isfinite(norm):
        raise ValueError("Query embedding has a non-finite length")
    if norm == 0:  # matches nothing: every distance is 1
        return np.zeros_like(query)
    result: FloatMatrix = query / norm
    return result


class KeywordIndex:
    """BM25 over title and text, one list across all sources, one hit per source record."""

    def __init__(
        self,
        records: Sequence[dict[str, Any]],
        *,
        k1: float = 1.2,
        b: float = 0.75,
        prefix: int = 6,
    ) -> None:
        self.records = records
        self.prefix = prefix
        self.k1 = k1
        self.b = b
        postings_docs: dict[str, list[int]] = defaultdict(list)
        postings_tfs: dict[str, list[int]] = defaultdict(list)
        lengths: list[int] = []
        for row, record in enumerate(records):
            counts = Counter(keyword_tokens(f"{record['title']} {record['text']}", prefix))
            lengths.append(sum(counts.values()))
            for term, frequency in counts.items():
                postings_docs[term].append(row)
                postings_tfs[term].append(frequency)
        self.lengths = np.asarray(lengths, dtype=np.float64)
        self.average_length = float(self.lengths.mean()) if lengths else 0.0
        count = len(records)
        self.idf = {
            term: math.log(1 + (count - len(rows) + 0.5) / (len(rows) + 0.5))
            for term, rows in postings_docs.items()
        }
        self.postings = {
            term: (
                np.asarray(rows, dtype=np.int64),
                np.asarray(postings_tfs[term], dtype=np.float64),
            )
            for term, rows in postings_docs.items()
        }

    def scores(self, text: str) -> npt.NDArray[np.float64]:
        scores = np.zeros(len(self.records), dtype=np.float64)
        if not self.records or self.average_length == 0:
            return scores
        for term in dict.fromkeys(keyword_tokens(text, self.prefix)):
            posting = self.postings.get(term)
            if posting is None:
                continue
            rows, frequencies = posting
            norm = 1 - self.b + self.b * self.lengths[rows] / self.average_length
            scores[rows] += (
                self.idf[term] * frequencies * (self.k1 + 1) / (frequencies + self.k1 * norm)
            )
        return scores

    def search(self, text: str, limit: int) -> list[RetrievedChunk]:
        scores = self.scores(text)
        matched = np.flatnonzero(scores > 0)
        if matched.size == 0 or limit <= 0:
            return []
        order = matched[np.argsort(-scores[matched], kind="stable")][: limit * 3]
        hits: list[RetrievedChunk] = []
        seen: set[tuple[str, str]] = set()
        for row in order:
            chunk = to_chunk(self.records[int(row)], 0.5, {"retrieval_method": "bm25"})
            key = record_key(chunk)
            if key in seen:
                continue
            seen.add(key)
            hits.append(chunk)
            if len(hits) == limit:
                break
        return hits


class SearchIndex:
    def __init__(
        self,
        generation_id: str,
        records: Sequence[dict[str, Any]],
        vectors: FloatMatrix,
    ) -> None:
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] != len(records):
            raise ValueError("Search index vectors do not match its records")
        if len(records) and vectors.shape[1] == 0:
            raise ValueError("Search index vectors have no dimensions")
        self.generation_id = generation_id
        self.records = list(records)
        grouped: dict[str, list[int]] = defaultdict(list)
        for row, record in enumerate(self.records):
            grouped[str(record["source_id"])].append(row)
        # One normalized matrix with each source's rows together, so a source is a contiguous
        # slice (a view, not a copy) and searching it never copies the matrix. Within a source
        # the rows keep the records' order. ``position`` maps a record row to its matrix row.
        self.rows_by_source: dict[str, npt.NDArray[np.int64]] = {}
        self.slices: dict[str, slice] = {}
        offset = 0
        for source, rows in grouped.items():
            self.rows_by_source[source] = np.asarray(rows, dtype=np.int64)
            self.slices[source] = slice(offset, offset + len(rows))
            offset += len(rows)
        self.matrix_rows = np.asarray(
            [row for rows in grouped.values() for row in rows], dtype=np.int64
        )
        self.position = np.empty(len(self.records), dtype=np.int64)
        self.position[self.matrix_rows] = np.arange(len(self.records), dtype=np.int64)
        unit: FloatMatrix = vectors[self.matrix_rows]  # the index's only copy of the vectors
        norms = np.linalg.norm(unit, axis=1, keepdims=True)
        if not np.all(np.isfinite(norms)):
            raise ValueError("Search index holds a non-finite embedding")
        norms[norms == 0] = 1.0  # a zero embedding stays zero and matches nothing
        unit /= norms
        self.unit = unit
        self.row_by_id = {str(record["chunk_id"]): row for row, record in enumerate(self.records)}
        if len(self.row_by_id) != len(self.records):
            raise ValueError("Search index chunk IDs are not unique")
        self.keywords = KeywordIndex(self.records)
        self.day_rows = [
            row
            for row, record in enumerate(self.records)
            if (record.get("metadata") or {}).get("section") == "day"
        ]

    @property
    def dimensions(self) -> int:
        return int(self.unit.shape[1])

    def _vector(self, row: int) -> FloatMatrix:
        vector: FloatMatrix = self.unit[self.position[row]]
        return vector

    def __len__(self) -> int:
        return len(self.records)

    def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int,
        distance_threshold: float,
        source_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """Exact cosine search, nearest first, within the distance threshold."""
        if source_id is None:
            rows = self.matrix_rows
            matrix = self.unit
        else:
            rows = self.rows_by_source.get(source_id, np.asarray([], dtype=np.int64))
            matrix = self.unit[self.slices.get(source_id, slice(0, 0))]
        if rows.size == 0 or limit <= 0:
            return []
        distances = np.maximum(0.0, 1.0 - matrix @ _unit(query_vector))
        hits: list[RetrievedChunk] = []
        for position in np.argsort(distances, kind="stable"):
            distance = float(distances[position])
            if distance > distance_threshold:
                break
            hits.append(to_chunk(self.records[int(rows[position])], distance))
            if len(hits) == limit:
                break
        return hits

    def keyword_search(self, text: str, limit: int) -> list[RetrievedChunk]:
        return self.keywords.search(text, limit)

    def distance_to(self, chunk_id: str, vectors: Sequence[Sequence[float]]) -> float:
        """The chunk's smallest cosine distance to any of the request's query vectors."""
        row = self._vector(self.row_by_id[chunk_id])
        return min((max(0.0, float(1.0 - row @ _unit(vector))) for vector in vectors), default=2.0)

    def metadata_passages(
        self, plan: SearchPlan, vectors: Sequence[Sequence[float]]
    ) -> list[RetrievedChunk]:
        """Exact lookups with Firestore's semantics: per filter the first 32 matches in
        document-ID order (range filters order by their field first), the chunk-0 rule for the
        participation index, then deduplication and the anchor cap."""
        if not vectors:
            return []
        units = [_unit(vector) for vector in vectors]
        found: dict[str, tuple[int, RetrievedChunk]] = {}
        for conditions in metadata_filters(plan):
            matched = [
                record
                for record in self.records
                if all(
                    _matches(_field(record, field), op, value) for field, op, value in conditions
                )
            ]
            range_fields = [field for field, op, _ in conditions if op != "=="]
            if range_fields:
                matched.sort(
                    key=lambda record: (_field(record, range_fields[0]), record["chunk_id"])
                )
            else:
                matched.sort(key=lambda record: record["chunk_id"])
            for record in matched[:MAX_METADATA_MATCHES]:
                if record["source_id"] not in SOURCE_IDS:
                    continue
                if (record.get("metadata") or {}).get("section") == "index" and record.get(
                    "chunk_index"
                ) != 0:
                    continue
                row = self._vector(self.row_by_id[str(record["chunk_id"])])
                distance = min(max(0.0, float(1.0 - row @ unit)) for unit in units)
                item = to_chunk(record, distance, {"retrieval_method": "metadata-anchor"})
                found[item.chunk_id] = (int(record.get("chunk_index") or 0), item)
        values = sorted(found.values(), key=lambda pair: (pair[0], pair[1].distance))
        return deduplicate([item for _, item in values])[:MAX_ANCHORS]

    def day_notes(
        self,
        vectors: Sequence[Sequence[float]],
        *,
        start: str | None,
        end: str | None,
        budget: int,
    ) -> list[RetrievedChunk]:
        """Pulsi day notes in the period: the closest language version of each protest day,
        then the budget spread evenly over the period's months, closest days first."""
        if not vectors or not self.day_rows or budget <= 0:
            return []
        # float64 query units, as in the evaluated pipeline, so near-equal days order the same
        queries = []
        for vector in vectors:
            norm = np.linalg.norm(np.asarray(vector, dtype=np.float64))
            if np.isfinite(norm) and norm > 0:
                queries.append(np.asarray(vector, dtype=np.float32) / norm)
        if not queries:
            return []
        units = np.stack(queries)
        best_per_day: dict[object, tuple[float, int]] = {}
        for row in self.day_rows:
            metadata = self.records[row].get("metadata") or {}
            event_date = metadata.get("event_date") or ""
            if not isinstance(event_date, str) or not event_date:
                continue
            if (start and event_date < start) or (end and event_date > end):
                continue
            distance = float(np.min(1.0 - units @ self._vector(row)))
            day = metadata.get("protest_day")
            identity = day if day is not None else ("date", event_date)
            if identity not in best_per_day or distance < best_per_day[identity][0]:
                best_per_day[identity] = (distance, row)
        per_month: dict[str, list[tuple[float, int]]] = defaultdict(list)
        for distance, row in best_per_day.values():
            event_date = str((self.records[row].get("metadata") or {}).get("event_date", ""))
            per_month[event_date[:7]].append((distance, row))
        share = max(1, budget // max(1, len(per_month)))
        picked = [
            (month, distance, row)
            for month, rows in sorted(per_month.items())
            for distance, row in sorted(rows)[:share]
        ]
        if len(picked) > budget:  # more months than notes: keep the closest days
            closest = set(sorted(picked, key=lambda item: (item[1], item[2]))[:budget])
            picked = [item for item in picked if item in closest]
        return [to_chunk(self.records[row], distance) for _, distance, row in picked]


def snapshot_records(chunks: Sequence[ChunkRecord]) -> list[dict[str, Any]]:
    return [chunk.model_dump(mode="json", include=set(RECORD_FIELDS)) for chunk in chunks]


def _split(blob: bytes) -> list[bytes]:
    return [
        blob[start : start + SNAPSHOT_PART_BYTES]
        for start in range(0, len(blob), SNAPSHOT_PART_BYTES)
    ] or [b""]


def pack_snapshot(
    chunks: Sequence[ChunkRecord], dimensions: int
) -> tuple[dict[str, Any], list[bytes], list[bytes]]:
    """Records as gzip JSON and embeddings as one little-endian float32 matrix, both split
    into parts small enough for Firestore documents, with hashes for validation on load."""
    if not chunks:
        raise ValueError("Cannot pack an empty generation")
    if any(chunk.embedding is None or len(chunk.embedding) != dimensions for chunk in chunks):
        raise ValueError("Every packed chunk needs an embedding of the generation's dimensions")
    records = snapshot_records(chunks)
    records_blob = gzip.compress(
        json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0
    )
    matrix = np.asarray([chunk.embedding for chunk in chunks], dtype=SNAPSHOT_DTYPE)
    vectors_blob = matrix.tobytes(order="C")
    record_parts = _split(records_blob)
    vector_parts = _split(vectors_blob)
    manifest = {
        "format": SNAPSHOT_FORMAT,
        "chunk_count": len(chunks),
        "dimensions": dimensions,
        "dtype": SNAPSHOT_DTYPE,
        "record_parts": len(record_parts),
        "vector_parts": len(vector_parts),
        "records_sha256": hashlib.sha256(records_blob).hexdigest(),
        "vectors_sha256": hashlib.sha256(vectors_blob).hexdigest(),
    }
    return manifest, record_parts, vector_parts


def unpack_snapshot(
    generation_id: str,
    manifest: dict[str, Any],
    record_parts: Sequence[bytes],
    vector_parts: Sequence[bytes],
) -> SearchIndex:
    if manifest.get("format") != SNAPSHOT_FORMAT or manifest.get("dtype") != SNAPSHOT_DTYPE:
        raise ValueError("Unsupported search snapshot format")
    count = manifest.get("chunk_count")
    dimensions = manifest.get("dimensions")
    if (
        not isinstance(count, int)
        or count <= 0
        or not isinstance(dimensions, int)
        or dimensions <= 0
    ):
        raise ValueError("Search snapshot manifest has invalid counts")
    if len(record_parts) != manifest.get("record_parts") or len(vector_parts) != manifest.get(
        "vector_parts"
    ):
        raise ValueError("Search snapshot is missing parts")
    records_blob = b"".join(record_parts)
    vectors_blob = b"".join(vector_parts)
    if hashlib.sha256(records_blob).hexdigest() != manifest.get("records_sha256"):
        raise ValueError("Search snapshot records failed their hash check")
    if hashlib.sha256(vectors_blob).hexdigest() != manifest.get("vectors_sha256"):
        raise ValueError("Search snapshot vectors failed their hash check")
    records = json.loads(gzip.decompress(records_blob).decode("utf-8"))
    if not isinstance(records, list) or len(records) != count:
        raise ValueError("Search snapshot record count does not match its manifest")
    if len(vectors_blob) != count * dimensions * 4:
        raise ValueError("Search snapshot vector size does not match its manifest")
    vectors = np.frombuffer(vectors_blob, dtype=SNAPSHOT_DTYPE).reshape(count, dimensions)
    return SearchIndex(generation_id, records, vectors)
