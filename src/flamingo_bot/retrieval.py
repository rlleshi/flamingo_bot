"""Retrieval over the active generation's in-memory index, and grounded answer streaming.

Narrow questions search every planner query plus the visitor's own wording with vector and
keyword search, fused per record, exact anchors first, and keep at most seven passages. Broad
questions (overviews over time, lists, comparisons, the status of several items) add a sample
passage and extra queries, a follow-up search from what the first pass found, Pulsi day notes
and a pool filled month by month, and keep up to thirty passages for a longer answer.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

from flamingo_bot.config import Settings
from flamingo_bot.conversation import normalize_history
from flamingo_bot.errors import FlamingoBotError, ProviderError
from flamingo_bot.models import Citation, ConversationTurn, RetrievedChunk
from flamingo_bot.planning import (
    ANCHOR_INSTRUCTIONS,
    BROAD_DAY_NOTES,
    BROAD_FALLBACK_SELECTED,
    BROAD_HITS_PER_QUERY,
    BROAD_POOL_SIZE,
    EXPANSION_INSTRUCTIONS,
    FOLLOW_UP_INSTRUCTIONS,
    MAX_CANDIDATES,
    MAX_KEYWORD_HITS,
    PLAN_INSTRUCTIONS,
    RANK_INSTRUCTIONS,
    RECENT_DISTANCE_WINDOW,
    SCOPE_INSTRUCTIONS,
    SOURCE_IDS,
    QueryExpansion,
    QuestionScope,
    SearchPlan,
    broad_rank_instructions,
    build_broad_rank_prompt,
    build_follow_up_prompt,
    build_plan_prompt,
    build_question_prompt,
    build_rank_prompt,
    cap_reference_headlines,
    deduplicate,
    metadata_filters,
    prepare_plan,
    retrieval_queries,
    scope_period,
    selected_broad_passages,
    selected_passages,
)
from flamingo_bot.ports import AnswerGenerator, Embedder, PassageRanker, QueryPlanner, VectorStore
from flamingo_bot.prompting import (
    BROAD_ANSWER_MAX_OUTPUT_TOKENS,
    answer_instructions,
    build_answer_prompt,
)
from flamingo_bot.search_index import SearchIndex, chunk_month, record_key

logger = logging.getLogger("flamingo_bot.retrieval")

#: Deadlines for the small model's broad-question calls; each failure has a defined fallback.
SCOPE_TIMEOUT_SECONDS = 10.0
EXPANSION_TIMEOUT_SECONDS = 10.0
FOLLOW_UP_TIMEOUT_SECONDS = 10.0
#: First-pass passages shown to the follow-up call.
FOLLOW_UP_SNIPPETS = 40
#: Fused candidates whose months weight the broad pool's month quotas.
MONTH_WEIGHT_CANDIDATES = 150
#: After a failed index reload, keep serving the loaded generation this long before retrying.
INDEX_RETRY_SECONDS = 60.0

_ALBANIAN_MARKERS = {
    "çfarë",
    "është",
    "për",
    "dhe",
    "nga",
    "revolucioni",
    "shqipëri",
    "diaspora",
}


def no_evidence_message(question: str) -> str:
    words = {word.strip(".,!?;:()[]\"'").lower() for word in question.split()}
    if any(character in question.lower() for character in "çë") or words & _ALBANIAN_MARKERS:
        return (
            "Nuk gjeta prova mjaftueshëm të rëndësishme në burimet e publikuara të "
            "Flamingos për t'iu përgjigjur me besueshmëri kësaj pyetjeje.\n\n"
            "Mund të të ndihmoj me Revolucionin Flamingo dhe protestat, me skandalet "
            "e dokumentuara në Dosjen Flamingo, dhe me pjesëmarrjen qytetare e "
            "diasporën te Diaspora Zbarkon. Provo një pyetje më të përcaktuar, për "
            "shembull për një çështje, një protestë ose një person të caktuar."
        )
    return (
        "I could not find enough relevant evidence in the published Flamingo "
        "sources to answer that question reliably.\n\n"
        "I can help with the Flamingo Revolution and its protests, the scandals "
        "documented in the Flamingo Dossier, and civic participation and diaspora "
        "mobilisation from Diaspora Zbarkon. Try a more specific question, for "
        "example about a particular case, protest, or person."
    )


def _recency_bonus(chunk: RetrievedChunk, as_of: date) -> float:
    for field in ("event_date", "reported_event_date", "research_date"):
        value = chunk.metadata.get(field)
        if not isinstance(value, str) or not value:
            continue
        try:
            evidence_date = date.fromisoformat(value)
        except ValueError:
            continue
        age = (as_of - evidence_date).days
        # Filing dates affect ranking only, never the date stated for an incident.
        return 0.08 * max(0.0, 1.0 - age / 30) if age >= 0 else 0.0
    return 0.0


def diversify(
    chunks: list[RetrievedChunk], limit: int, *, prefer_recent_as_of: date | None = None
) -> list[RetrievedChunk]:
    selected: list[RetrievedChunk] = []
    source_counts: Counter[str] = Counter()
    seen_documents: set[tuple[str, str]] = set()
    seen_articles: set[str] = set()

    def lapsi_article_keys(item: RetrievedChunk) -> set[str]:
        aliases = item.metadata.get("aliases")
        urls = [item.canonical_url, *(aliases if isinstance(aliases, list) else [])]
        keys: set[str] = set()
        for url in urls:
            if not isinstance(url, str):
                continue
            parsed = urlparse(url)
            if parsed.hostname not in {"lapsi.al", "www.lapsi.al"}:
                continue
            path = parsed.path.rstrip("/")
            match = re.search(r"-i(\d+)$", path)
            keys.add("lapsi:" + match.group(1) if match else "lapsi:" + path)
        return keys

    detailed_article_keys = {
        key
        for chunk in chunks
        if chunk.metadata.get("content_use") != "reference"
        for key in lapsi_article_keys(chunk)
    }

    def rank(item: RetrievedChunk) -> float:
        bonus = _recency_bonus(item, prefer_recent_as_of) if prefer_recent_as_of else 0.0
        return item.distance - bonus

    for chunk in sorted(chunks, key=rank):
        article_keys = lapsi_article_keys(chunk)
        if article_keys & seen_articles:
            continue
        if (
            chunk.metadata.get("content_use") == "reference"
            and article_keys & detailed_article_keys
        ):
            continue
        document_key = (chunk.source_id, str(chunk.metadata.get("record_id") or chunk.title))
        if document_key in seen_documents:
            continue
        if source_counts[chunk.source_id] >= 3:
            continue
        selected.append(chunk)
        seen_documents.add(document_key)
        seen_articles.update(article_keys)
        source_counts[chunk.source_id] += 1
        if len(selected) == limit:
            break
    return selected


def citations_for(chunks: list[RetrievedChunk]) -> list[Citation]:
    citations: list[Citation] = []
    for index, chunk in enumerate(chunks, start=1):
        citations.append(
            Citation(
                id=f"S{index}",
                title=chunk.title,
                url=chunk.canonical_url,
                source=chunk.source_label,
            )
        )
    return citations


def fuse_records(lists: Sequence[Sequence[RetrievedChunk]]) -> list[RetrievedChunk]:
    """Reciprocal-rank fusion per source record; each record keeps its closest passage."""
    scores: dict[tuple[str, str], float] = defaultdict(float)
    best: dict[tuple[str, str], RetrievedChunk] = {}
    for ranked in lists:
        for rank, chunk in enumerate(ranked, 1):
            key = record_key(chunk)
            scores[key] += 1 / (60 + rank)
            if key not in best or chunk.distance < best[key].distance:
                best[key] = chunk
    return sorted(deduplicate(list(best.values())), key=lambda chunk: -scores[record_key(chunk)])


def in_period(chunk: RetrievedChunk, start: str | None, end: str | None) -> bool:
    month = chunk_month(chunk)
    return month == "undated" or (
        (not start or month >= start[:7]) and (not end or month <= end[:7])
    )


def _is_anchor(chunk: RetrievedChunk) -> bool:
    return chunk.metadata.get("retrieval_method") == "metadata-anchor"


def broad_pool(
    *,
    fused: Sequence[RetrievedChunk],
    anchors: Sequence[RetrievedChunk],
    notes: Sequence[RetrievedChunk],
    size: int = BROAD_POOL_SIZE,
) -> list[RetrievedChunk]:
    """Exact anchors and day notes first, then month quotas in proportion to where the relevant
    material is (at least two per month, undated passages capped), then fused order. Anchors
    lead the result; the rest is ordered by month and distance."""
    buckets: dict[str, list[RetrievedChunk]] = defaultdict(list)
    for chunk in fused:
        buckets[chunk_month(chunk)].append(chunk)
    weights = Counter(
        month
        for month in (chunk_month(chunk) for chunk in fused[:MONTH_WEIGHT_CANDIDATES])
        if month != "undated"
    )
    pool: list[RetrievedChunk] = []
    seen: set[tuple[str, str]] = set()
    for chunk in [*anchors, *notes]:
        if record_key(chunk) not in seen:
            seen.add(record_key(chunk))
            pool.append(chunk)
    undated_cap = max(4, size // 6)
    room = max(0, size - len(pool) - undated_cap)
    total = sum(weights.values()) or 1
    quotas = {month: max(2, round(room * count / total)) for month, count in weights.items()}
    quotas["undated"] = undated_cap
    for month, quota in sorted(quotas.items(), key=lambda item: -item[1]):
        taken = 0
        for chunk in buckets.get(month, []):
            if taken >= quota or len(pool) >= size:
                break
            if record_key(chunk) in seen:
                continue
            seen.add(record_key(chunk))
            pool.append(chunk)
            taken += 1
    for chunk in fused:
        if len(pool) >= size:
            break
        if record_key(chunk) not in seen:
            seen.add(record_key(chunk))
            pool.append(chunk)
    anchored = [chunk for chunk in pool if _is_anchor(chunk)]
    rest = sorted(
        (chunk for chunk in pool if not _is_anchor(chunk)),
        key=lambda chunk: (chunk_month(chunk), chunk.distance),
    )
    return anchored + rest


def spread_by_month(pool: Sequence[RetrievedChunk], limit: int) -> list[RetrievedChunk]:
    """Fallback when the broad selector fails: anchors, then the closest remaining passage of
    each month in turn."""
    chosen = [chunk for chunk in pool if _is_anchor(chunk)][:limit]
    by_month: dict[str, list[RetrievedChunk]] = defaultdict(list)
    for chunk in pool:
        if not _is_anchor(chunk):
            by_month[chunk_month(chunk)].append(chunk)
    queues = [
        sorted(items, key=lambda chunk: chunk.distance) for _, items in sorted(by_month.items())
    ]
    while len(chosen) < limit and any(queues):
        for queue in queues:
            if queue and len(chosen) < limit:
                chosen.append(queue.pop(0))
    return chosen


class IndexCache:
    """The active generation's search index, loaded once per instance and reloaded when the
    active-generation pointer moves.

    Each generation has at most one load in flight, shared by every request that needs it and
    shielded from their cancellation, and loads run one at a time so two generations are never
    built at once. After a failed load the cache keeps serving the loaded generation, or fails
    fast when nothing is loaded, until ``INDEX_RETRY_SECONDS`` have passed.
    """

    def __init__(self, store: VectorStore) -> None:
        self._store = store
        self._index: SearchIndex | None = None
        self._loading: dict[str, asyncio.Task[SearchIndex]] = {}
        self._failed: dict[str, float] = {}
        self._build_lock = asyncio.Lock()

    @property
    def loaded(self) -> SearchIndex | None:
        return self._index

    async def for_generation(self, generation_id: str) -> SearchIndex:
        current = self._index
        if current is not None and current.generation_id == generation_id:
            return current
        failed_at = self._failed.get(generation_id)
        if failed_at is not None and time.monotonic() - failed_at < INDEX_RETRY_SECONDS:
            if current is not None:
                return current
            raise ProviderError("The search index could not be loaded")
        task = self._loading.get(generation_id)
        if task is None:
            task = asyncio.create_task(self._load(generation_id))
            # A load whose requests have all gone away must not log "exception never retrieved".
            task.add_done_callback(lambda done: done.cancelled() or done.exception())
            self._loading[generation_id] = task
        try:
            return await asyncio.shield(task)
        except FlamingoBotError:
            if self._index is not None:
                return self._index
            raise

    async def _load(self, generation_id: str) -> SearchIndex:
        try:
            async with self._build_lock:
                started = time.monotonic()
                try:
                    loaded = await self._store.load_index(generation_id)
                except FlamingoBotError as exc:
                    self._failed[generation_id] = time.monotonic()
                    logger.warning(
                        "index_load_failed error=%s generation=%s serving_generation=%s",
                        type(exc).__name__,
                        generation_id,
                        self._index.generation_id if self._index else None,
                    )
                    raise
                self._failed.pop(generation_id, None)
                self._index = loaded
                logger.info(
                    "index_loaded generation=%s chunks=%d duration_ms=%d",
                    generation_id,
                    len(loaded),
                    int((time.monotonic() - started) * 1000),
                )
                return loaded
        finally:
            self._loading.pop(generation_id, None)


@dataclass(frozen=True)
class PreparedQuestion:
    """A question with its plan and scope, before retrieval and the answer."""

    question: str
    turns: list[ConversationTurn]
    as_of: date
    plan: SearchPlan
    scope: QuestionScope | None

    @property
    def broad(self) -> bool:
        return self.scope is not None and self.scope.broad


class ChatService:
    def __init__(
        self,
        *,
        settings: Settings,
        embedder: Embedder,
        vector_store: VectorStore,
        answer_generator: AnswerGenerator,
        query_planner: QueryPlanner | None = None,
        passage_ranker: PassageRanker | None = None,
    ) -> None:
        self.settings = settings
        self.embedder = embedder
        self.vector_store = vector_store
        self.answer_generator = answer_generator
        self.query_planner = query_planner
        self.passage_ranker = passage_ranker
        self.index_cache = IndexCache(vector_store)

    async def warm_index(self) -> None:
        """Load the active generation before the first question; failures only log."""
        try:
            await self._pinned_index()
        except FlamingoBotError as exc:
            logger.warning("index_warmup_failed error=%s", type(exc).__name__)

    async def _pinned_index(self) -> SearchIndex | None:
        generation = await self.vector_store.active_generation()
        if generation is None:
            return None
        return await self.index_cache.for_generation(generation)

    async def index_status(self) -> dict[str, object]:
        """Readiness detail: the generation this instance answers from. Raises when the
        active generation cannot be loaded."""
        index = await self._pinned_index()
        if index is None:
            return {"search_index": None}
        return {"search_index": {"generation": index.generation_id, "chunks": len(index)}}

    @staticmethod
    def _check_dimensions(index: SearchIndex, vectors: Sequence[Sequence[float]]) -> None:
        if vectors and len(vectors[0]) != index.dimensions:
            raise ProviderError("Query and index embeddings have different sizes")

    async def plan_question(
        self,
        question: str,
        history: Sequence[ConversationTurn],
        *,
        as_of: date,
        safety_identifier: str | None = None,
    ) -> SearchPlan:
        if self.query_planner is not None:
            try:
                plan = await self.query_planner.plan_search(
                    instructions=PLAN_INSTRUCTIONS + ANCHOR_INSTRUCTIONS,
                    prompt=build_plan_prompt(question, history, as_of),
                    safety_identifier=safety_identifier,
                )
                return prepare_plan(plan, question)
            except (FlamingoBotError, ValueError) as exc:
                logger.warning("planner_fallback error=%s", type(exc).__name__)
        return SearchPlan.fallback(question)

    async def classify_scope(
        self,
        question: str,
        history: Sequence[ConversationTurn],
        *,
        as_of: date,
        safety_identifier: str | None = None,
    ) -> QuestionScope | None:
        """Broad or narrow; a failure means narrow, which is today's behaviour."""
        if self.query_planner is None:
            return None
        try:
            return await asyncio.wait_for(
                self.query_planner.classify_scope(
                    instructions=SCOPE_INSTRUCTIONS,
                    prompt=build_question_prompt(question, history, as_of),
                    safety_identifier=safety_identifier,
                ),
                timeout=SCOPE_TIMEOUT_SECONDS,
            )
        except (FlamingoBotError, ValueError, TimeoutError) as exc:
            logger.warning("scope_fallback error=%s", type(exc).__name__)
            return None

    async def _expansion(
        self,
        question: str,
        history: Sequence[ConversationTurn],
        *,
        as_of: date,
        safety_identifier: str | None,
    ) -> QueryExpansion | None:
        if self.query_planner is None:
            return None
        try:
            return await asyncio.wait_for(
                self.query_planner.expand_question(
                    instructions=EXPANSION_INSTRUCTIONS,
                    prompt=build_question_prompt(question, history, as_of),
                    safety_identifier=safety_identifier,
                ),
                timeout=EXPANSION_TIMEOUT_SECONDS,
            )
        except (FlamingoBotError, ValueError, TimeoutError) as exc:
            logger.warning("expansion_fallback error=%s", type(exc).__name__)
            return None

    async def _follow_up(
        self,
        question: str,
        snippets: Sequence[str],
        *,
        as_of: date,
        safety_identifier: str | None,
    ) -> list[str]:
        if self.query_planner is None or not snippets:
            return []
        try:
            result = await asyncio.wait_for(
                self.query_planner.follow_up_queries(
                    instructions=FOLLOW_UP_INSTRUCTIONS,
                    prompt=build_follow_up_prompt(question, snippets, as_of),
                    safety_identifier=safety_identifier,
                ),
                timeout=FOLLOW_UP_TIMEOUT_SECONDS,
            )
        except (FlamingoBotError, ValueError, TimeoutError) as exc:
            logger.warning("follow_up_fallback error=%s", type(exc).__name__)
            return []
        return [query for query in result.queries if query.strip()]

    @staticmethod
    def _keyword_hits(
        index: SearchIndex, text: str, limit: int, vectors: Sequence[Sequence[float]]
    ) -> list[RetrievedChunk]:
        return [
            hit.model_copy(update={"distance": index.distance_to(hit.chunk_id, vectors)})
            for hit in index.keyword_search(text, limit)
        ]

    def _anchors(
        self, index: SearchIndex, plan: SearchPlan, vectors: Sequence[Sequence[float]]
    ) -> list[RetrievedChunk]:
        if not metadata_filters(plan):
            return []
        try:
            return index.metadata_passages(plan, vectors)
        except (FlamingoBotError, ValueError) as exc:
            logger.warning("metadata_anchor_fallback error=%s", type(exc).__name__)
            return []

    def _narrow_pool(
        self,
        index: SearchIndex,
        plan: SearchPlan,
        queries: Sequence[str],
        vectors: Sequence[Sequence[float]],
    ) -> list[RetrievedChunk]:
        limit = self.settings.flamingo_retrieval_candidates * (2 if plan.recent else 1)
        threshold = self.settings.flamingo_relevance_distance_threshold
        lists: list[list[RetrievedChunk]] = []
        for vector in vectors:
            hits = [
                hit
                for source in SOURCE_IDS
                for hit in index.search(
                    vector, limit=limit, distance_threshold=threshold, source_id=source
                )
            ]
            if plan.recent and hits:
                best = min(item.distance for item in hits)
                hits = [item for item in hits if item.distance <= best + RECENT_DISTANCE_WINDOW]
            lists.append(deduplicate(hits))
        for text in queries:
            lists.append(self._keyword_hits(index, text, MAX_KEYWORD_HITS, vectors))
        pool = fuse_records(lists)[:MAX_CANDIDATES]
        anchors = self._anchors(index, plan, vectors)
        if anchors:
            keys = {record_key(item) for item in anchors}
            pool = [*anchors, *(item for item in pool if record_key(item) not in keys)]
        return pool[:MAX_CANDIDATES]

    async def retrieve(
        self,
        question: str | SearchPlan,
        *,
        history: Sequence[ConversationTurn] = (),
        as_of: date | None = None,
        safety_identifier: str | None = None,
        original_question: str | None = None,
    ) -> list[RetrievedChunk]:
        """Narrow retrieval: at most the configured number of passages."""
        started = time.monotonic()
        plan = SearchPlan.fallback(question) if isinstance(question, str) else question
        latest = original_question or (question if isinstance(question, str) else plan.queries[-1])
        today = as_of or datetime.now(UTC).date()
        index = await self._pinned_index()
        if index is None:
            return []
        queries = retrieval_queries(plan, latest)
        vectors = await self.embedder.embed_texts(queries)
        self._check_dimensions(index, vectors)
        pool = await asyncio.to_thread(self._narrow_pool, index, plan, queries, vectors)
        if not pool:
            return []
        if self.passage_ranker is not None:
            try:
                selection = await self.passage_ranker.select_passages(
                    instructions=RANK_INSTRUCTIONS,
                    prompt=build_rank_prompt(latest, history, plan, pool, today),
                    safety_identifier=safety_identifier,
                )
                chosen = selected_passages(selection, pool)[
                    : self.settings.flamingo_retrieval_candidates
                ]
                logger.info(
                    "retrieval_completed mode=narrow queries=%d candidates=%d selected=%d "
                    "duration_ms=%d",
                    len(queries),
                    len(pool),
                    len(chosen),
                    int((time.monotonic() - started) * 1000),
                )
                return chosen
            except (FlamingoBotError, ValueError) as exc:
                logger.warning("reranker_fallback error=%s", type(exc).__name__)
        return diversify(pool, self.settings.flamingo_retrieval_candidates)

    def _broad_lists(
        self,
        index: SearchIndex,
        texts: Sequence[str],
        text_vectors: Sequence[Sequence[float]],
        distance_vectors: Sequence[Sequence[float]],
    ) -> list[list[RetrievedChunk]]:
        threshold = self.settings.flamingo_relevance_distance_threshold
        lists: list[list[RetrievedChunk]] = []
        for vector in text_vectors:
            hits = [
                hit
                for source in SOURCE_IDS
                for hit in index.search(
                    vector,
                    limit=BROAD_HITS_PER_QUERY,
                    distance_threshold=threshold,
                    source_id=source,
                )
            ]
            lists.append(deduplicate(hits))
        for text in texts:
            lists.append(self._keyword_hits(index, text, BROAD_HITS_PER_QUERY, distance_vectors))
        return lists

    def _broad_pool(
        self,
        index: SearchIndex,
        plan: SearchPlan,
        fused: Sequence[RetrievedChunk],
        vectors: Sequence[Sequence[float]],
        period: tuple[str | None, str | None],
    ) -> list[RetrievedChunk]:
        start, end = period
        notes = index.day_notes(vectors, start=start, end=end, budget=BROAD_DAY_NOTES)
        return broad_pool(fused=fused, anchors=self._anchors(index, plan, vectors), notes=notes)

    async def _select_broad(
        self,
        question: str,
        history: Sequence[ConversationTurn],
        plan: SearchPlan,
        pool: Sequence[RetrievedChunk],
        today: date,
        safety_identifier: str | None,
    ) -> list[RetrievedChunk]:
        if self.passage_ranker is not None:
            try:
                selection = await self.passage_ranker.select_broad_passages(
                    instructions=broad_rank_instructions(),
                    prompt=build_broad_rank_prompt(question, history, plan, pool, today),
                    safety_identifier=safety_identifier,
                )
                return cap_reference_headlines(selected_broad_passages(selection, pool))
            except (FlamingoBotError, ValueError) as exc:
                logger.warning("broad_selector_fallback error=%s", type(exc).__name__)
        return cap_reference_headlines(spread_by_month(pool, BROAD_FALLBACK_SELECTED))

    async def retrieve_broad(
        self,
        plan: SearchPlan,
        *,
        question: str,
        history: Sequence[ConversationTurn] = (),
        as_of: date | None = None,
        period: tuple[str | None, str | None] = (None, None),
        safety_identifier: str | None = None,
    ) -> list[RetrievedChunk]:
        """Broad retrieval: up to thirty passages spread over the period's months."""
        started = time.monotonic()
        today = as_of or datetime.now(UTC).date()
        index = await self._pinned_index()
        if index is None:
            return []
        expansion = await self._expansion(
            question, history, as_of=today, safety_identifier=safety_identifier
        )
        queries = retrieval_queries(plan, question)
        if expansion is not None:
            queries = [*queries, expansion.passage, *expansion.queries]
        queries = list(dict.fromkeys(query for query in queries if query.strip()))
        start, end = period
        vectors = await self.embedder.embed_texts(queries)
        self._check_dimensions(index, vectors)
        lists = await asyncio.to_thread(self._broad_lists, index, queries, vectors, vectors)
        fused = [chunk for chunk in fuse_records(lists) if in_period(chunk, start, end)]
        follow_ups: list[str] = []
        if fused:
            snippets = [
                f"[{chunk_month(chunk)}] {chunk.title[:110]} :: "
                + chunk.text[:220].replace("\n", " ")
                for chunk in fused[:FOLLOW_UP_SNIPPETS]
                if chunk.metadata.get("content_use") != "reference"
            ]
            written = await self._follow_up(
                question, snippets, as_of=today, safety_identifier=safety_identifier
            )
            follow_ups = [query for query in dict.fromkeys(written) if query not in queries]
        if follow_ups:
            try:
                more_vectors = await self.embedder.embed_texts(follow_ups)
            except FlamingoBotError as exc:  # the first pass still stands
                logger.warning("follow_up_fallback stage=embedding error=%s", type(exc).__name__)
                follow_ups = []
            else:
                vectors = [*vectors, *more_vectors]
                lists += await asyncio.to_thread(
                    self._broad_lists, index, follow_ups, more_vectors, vectors
                )
                fused = [chunk for chunk in fuse_records(lists) if in_period(chunk, start, end)]
        pool = await asyncio.to_thread(self._broad_pool, index, plan, fused, vectors, period)
        if not pool:
            return []
        chosen = await self._select_broad(question, history, plan, pool, today, safety_identifier)
        logger.info(
            "retrieval_completed mode=broad queries=%d follow_up_queries=%d candidates=%d "
            "selected=%d duration_ms=%d",
            len(queries),
            len(follow_ups),
            len(pool),
            len(chosen),
            int((time.monotonic() - started) * 1000),
        )
        return chosen

    async def prepare(
        self,
        question: str,
        *,
        history: Sequence[ConversationTurn] = (),
        safety_identifier: str | None = None,
        as_of: date | None = None,
    ) -> PreparedQuestion:
        """Plan the question and decide whether it is broad; the planner and the scope call
        run together."""
        turns = normalize_history(
            history,
            max_turns=self.settings.flamingo_history_turns,
            max_chars=self.settings.flamingo_history_chars,
        )
        today = as_of or datetime.now(UTC).date()
        plan, scope = await asyncio.gather(
            self.plan_question(question, turns, as_of=today, safety_identifier=safety_identifier),
            self.classify_scope(
                question, turns, as_of=today, safety_identifier=safety_identifier
            ),
        )
        return PreparedQuestion(question=question, turns=turns, as_of=today, plan=plan, scope=scope)

    async def respond(
        self,
        prepared: PreparedQuestion,
        *,
        safety_identifier: str | None = None,
    ) -> tuple[list[Citation], AsyncIterator[str]]:
        """Retrieve for a prepared question and start streaming its answer."""
        question, turns, today, plan = (
            prepared.question,
            prepared.turns,
            prepared.as_of,
            prepared.plan,
        )
        scope = prepared.scope
        broad = prepared.broad
        if scope is not None and scope.broad:
            period = scope_period(scope)
            if period == (None, None) and (scope.period_start.strip() or scope.period_end.strip()):
                logger.warning("scope_period_ignored")
            chunks = await self.retrieve_broad(
                plan,
                question=question,
                history=turns,
                as_of=today,
                period=period,
                safety_identifier=safety_identifier,
            )
        else:
            chunks = await self.retrieve(
                plan,
                history=turns,
                as_of=today,
                safety_identifier=safety_identifier,
                original_question=question,
            )
        if not chunks:

            async def no_evidence() -> AsyncIterator[str]:
                yield no_evidence_message(question)

            return [], no_evidence()
        options: dict[str, Any] = (
            {"max_output_tokens": BROAD_ANSWER_MAX_OUTPUT_TOKENS} if broad else {}
        )
        return citations_for(chunks), self.answer_generator.stream_answer(
            instructions=answer_instructions(broad=broad),
            prompt=build_answer_prompt(question, plan.standalone, chunks, history=turns),
            safety_identifier=safety_identifier,
            **options,
        )

    async def stream(
        self,
        question: str,
        *,
        history: Sequence[ConversationTurn] = (),
        safety_identifier: str | None = None,
        as_of: date | None = None,
    ) -> tuple[list[Citation], AsyncIterator[str]]:
        prepared = await self.prepare(
            question, history=history, safety_identifier=safety_identifier, as_of=as_of
        )
        return await self.respond(prepared, safety_identifier=safety_identifier)
