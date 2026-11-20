"""In-memory provider fakes for deterministic, non-billable tests."""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import numpy as np

from flamingo_bot.models import RetrievedChunk
from flamingo_bot.planning import (
    MAX_ANCHORS,
    MAX_METADATA_MATCHES,
    BroadSelection,
    FollowUpQueries,
    PassageSelection,
    QueryExpansion,
    QuestionScope,
    SearchPlan,
    cosine_distance,
    deduplicate,
    metadata_filters,
)
from flamingo_bot.search_index import SearchIndex


def _matches_metadata(
    record: RetrievedChunk, conditions: Sequence[tuple[str, str, str | int]]
) -> bool:
    for field, operation, expected in conditions:
        actual = (
            record.metadata.get(field.removeprefix("metadata."))
            if field.startswith("metadata.")
            else record.model_dump().get(field, record.metadata.get(field))
        )
        if operation == "==":
            if actual != expected:
                return False
        elif not isinstance(actual, str) or not isinstance(expected, str):
            return False
        elif operation == ">=" and actual < expected:
            return False
        elif operation == "<" and actual >= expected:
            return False
    return True


class HashEmbedder:
    """Small deterministic embedder intended only for tests."""

    def __init__(self, dimensions: int = 16) -> None:
        self.dimensions = dimensions

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for word in text.lower().split():
                digest = hashlib.sha256(word.encode()).digest()
                vector[digest[0] % self.dimensions] += 1.0
            magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / magnitude for value in vector])
        return vectors


class InMemoryVectorStore:
    def __init__(
        self,
        records: Sequence[tuple[RetrievedChunk, Sequence[float]]] = (),
    ) -> None:
        self.records = [(record, list(vector)) for record, vector in records]

    async def active_generation(self) -> str | None:
        return "test" if self.records else None

    async def status(self) -> dict[str, object]:
        return {
            "ready": bool(self.records),
            "database_id": "in-memory-test-fake",
            "active_generation": "test" if self.records else None,
            "chunk_count": len(self.records),
            "created_at": datetime(2026, 8, 23, tzinfo=UTC),
        }

    async def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int,
        distance_threshold: float,
        source_id: str | None = None,
        generation_id: str | None = None,
    ) -> list[RetrievedChunk]:
        def cosine_distance(vector: Sequence[float]) -> float:
            dot = sum(left * right for left, right in zip(query_vector, vector, strict=True))
            left = math.sqrt(sum(value * value for value in query_vector))
            right = math.sqrt(sum(value * value for value in vector))
            if left == 0 or right == 0:
                return 1.0
            return 1.0 - dot / (left * right)

        ranked = [
            record.model_copy(update={"distance": cosine_distance(vector)})
            for record, vector in self.records
            if source_id is None or record.source_id == source_id
        ]
        return sorted(
            (record for record in ranked if record.distance <= distance_threshold),
            key=lambda record: record.distance,
        )[:limit]

    async def metadata_passages(
        self,
        plan: SearchPlan,
        vectors: Sequence[Sequence[float]],
        *,
        generation_id: str,
    ) -> list[RetrievedChunk]:
        found: dict[str, RetrievedChunk] = {}
        for conditions in metadata_filters(plan):
            matches = 0
            for record, vector in self.records:
                if not _matches_metadata(record, conditions):
                    continue
                matches += 1
                if matches > MAX_METADATA_MATCHES:
                    break
                if (
                    record.metadata.get("section") == "index"
                    and record.metadata.get("chunk_index", 0) != 0
                ):
                    continue
                found[record.chunk_id] = record.model_copy(
                    update={
                        "distance": min(cosine_distance(vector, item) for item in vectors),
                        "metadata": {**record.metadata, "retrieval_method": "metadata-anchor"},
                    }
                )
        return deduplicate(list(found.values()))[:MAX_ANCHORS]

    async def load_index(self, generation_id: str) -> SearchIndex:
        records = [
            {
                "chunk_id": record.chunk_id,
                "source_id": record.source_id,
                "source_label": record.source_label,
                "title": record.title,
                "text": record.text,
                "canonical_url": record.canonical_url,
                "legal_status": record.legal_status,
                "content_type": record.metadata.get("content_type", ""),
                "chunk_index": record.metadata.get("chunk_index", 0),
                "metadata": dict(record.metadata),
            }
            for record, _ in self.records
        ]
        dimensions = len(self.records[0][1]) if self.records else 1
        vectors = np.asarray(
            [vector for _, vector in self.records] or np.zeros((0, dimensions)), dtype=np.float32
        )
        return SearchIndex(generation_id, records, vectors.reshape(len(records), dimensions))


NARROW_SCOPE = QuestionScope(broad=False, period_start="", period_end="")


class ScriptedPlanner:
    """Deterministic stand-in for the small model's typed retrieval calls."""

    def __init__(
        self,
        plan: SearchPlan | None = None,
        *,
        error: Exception | None = None,
        scope: QuestionScope | Exception = NARROW_SCOPE,
        expansion: QueryExpansion | Exception | None = None,
        follow_up: FollowUpQueries | Exception | None = None,
    ) -> None:
        self.plan = plan or SearchPlan.fallback("Revolucioni Flamingo")
        self.error = error
        self.scope = scope
        self.expansion = expansion or QueryExpansion(passage="", queries=[])
        self.follow_up = follow_up or FollowUpQueries(queries=[])
        self.calls: list[dict[str, str | None]] = []
        self.scope_calls: list[str] = []
        self.expansion_calls: list[str] = []
        self.follow_up_calls: list[str] = []

    async def classify_scope(
        self, *, instructions: str, prompt: str, safety_identifier: str | None
    ) -> QuestionScope:
        self.scope_calls.append(prompt)
        if isinstance(self.scope, Exception):
            raise self.scope
        return self.scope

    async def expand_question(
        self, *, instructions: str, prompt: str, safety_identifier: str | None
    ) -> QueryExpansion:
        self.expansion_calls.append(prompt)
        if isinstance(self.expansion, Exception):
            raise self.expansion
        return self.expansion

    async def follow_up_queries(
        self, *, instructions: str, prompt: str, safety_identifier: str | None
    ) -> FollowUpQueries:
        self.follow_up_calls.append(prompt)
        if isinstance(self.follow_up, Exception):
            raise self.follow_up
        return self.follow_up

    async def plan_search(
        self,
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
    ) -> SearchPlan:
        self.calls.append(
            {
                "instructions": instructions,
                "prompt": prompt,
                "safety_identifier": safety_identifier,
            }
        )
        if self.error is not None:
            raise self.error
        return self.plan


class ScriptedRanker:
    def __init__(
        self,
        selected_ids: Sequence[str] = (),
        *,
        error: Exception | None = None,
        broad_ids: Sequence[str] | Exception = (),
    ) -> None:
        self.selected_ids = list(selected_ids)
        self.error = error
        self.broad_ids = broad_ids
        self.calls: list[dict[str, str | None]] = []
        self.broad_calls: list[dict[str, str | None]] = []

    async def select_broad_passages(
        self, *, instructions: str, prompt: str, safety_identifier: str | None
    ) -> BroadSelection:
        self.broad_calls.append({"instructions": instructions, "prompt": prompt})
        if isinstance(self.broad_ids, Exception):
            raise self.broad_ids
        return BroadSelection(selected_ids=list(self.broad_ids))

    async def select_passages(
        self,
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
    ) -> PassageSelection:
        self.calls.append(
            {"instructions": instructions, "prompt": prompt, "safety_identifier": safety_identifier}
        )
        if self.error is not None:
            raise self.error
        return PassageSelection(selected_ids=self.selected_ids)


class ScriptedAnswerGenerator:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict[str, str | None]] = []

    async def stream_answer(
        self,
        *,
        instructions: str,
        prompt: str,
        safety_identifier: str | None,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "instructions": instructions,
                "prompt": prompt,
                "safety_identifier": safety_identifier,
                "max_output_tokens": str(max_output_tokens) if max_output_tokens else None,
            }
        )
        for word in self.answer.split(" "):
            yield word + " "
