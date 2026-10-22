"""One aggregate document in the writable quota database; no event records."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore_v1
from pydantic import ValidationError

from flamingo_bot.answer_count import (
    ANSWER_COUNT_COLLECTION,
    ANSWER_COUNT_DOCUMENT,
    AnswerCount,
)
from flamingo_bot.config import Settings
from flamingo_bot.errors import ConfigurationError, ProviderError
from flamingo_bot.providers.firestore import firestore_credentials

RPC_TIMEOUT_SECONDS = 1.5


class FirestoreAnswerCounter:
    def __init__(self, settings: Settings, *, client: firestore_v1.Client | None = None) -> None:
        if settings.flamingo_quota_database_id == settings.firestore_database_id:
            raise ConfigurationError("Answer counters must not use the knowledge database")
        self.client = (
            client
            if client is not None
            else firestore_v1.Client(
                project=settings.gcp_project_id,
                database=settings.flamingo_quota_database_id,
                credentials=firestore_credentials(settings),
            )
        )
        self.reference = self.client.collection(ANSWER_COUNT_COLLECTION).document(
            ANSWER_COUNT_DOCUMENT
        )

    def _read_sync(self) -> AnswerCount | None:
        snapshot = self.reference.get(retry=None, timeout=RPC_TIMEOUT_SECONDS)
        if not snapshot.exists:
            return None
        value = snapshot.to_dict() or {}
        return AnswerCount.model_validate({"answered": value.get("answered")})

    def _increment_sync(self) -> AnswerCount:
        # update() requires an existing document. Only the private operator CLI
        # can initialize it; a deployment must never silently reset the baseline.
        # Disable SDK retries too: increments are atomic but not idempotent.
        result = self.reference.update(
            {"answered": firestore_v1.Increment(1)},
            retry=None,
            timeout=RPC_TIMEOUT_SECONDS,
        )
        # Firestore returns the new field value in the transform result. This
        # gives this visitor the authoritative total without a second read.
        if len(result.transform_results) != 1:
            raise ProviderError("Invalid answer counter response")
        transformed = result.transform_results[0]
        if transformed._pb.WhichOneof("value_type") != "integer_value":
            raise ProviderError("Invalid answer counter value")
        return AnswerCount(answered=transformed.integer_value)

    async def read(self) -> AnswerCount | None:
        try:
            return await asyncio.to_thread(self._read_sync)
        except (GoogleAPICallError, ValidationError) as exc:
            raise ProviderError("Answer total read failed") from exc

    async def increment(self) -> AnswerCount:
        try:
            return await asyncio.to_thread(self._increment_sync)
        except (GoogleAPICallError, ValidationError) as exc:
            raise ProviderError("Answer total update failed") from exc

    def initialize(self, baseline: int, baseline_as_of: datetime) -> None:
        value = AnswerCount(answered=baseline)
        if baseline_as_of.tzinfo is None or baseline_as_of.utcoffset() is None:
            raise ValueError("Baseline date must include a timezone")
        if baseline_as_of > datetime.now(UTC):
            raise ValueError("Baseline date cannot be in the future")
        data: dict[str, Any] = {
            "answered": value.answered,
            "baseline": value.answered,
            "baseline_as_of": baseline_as_of.astimezone(UTC),
            "initialized_at": firestore_v1.SERVER_TIMESTAMP,
        }
        # Create-only precondition protects a live counter from repeat execution,
        # deployments, or a stale operator report. No merge/reset operation exists.
        self.reference.create(data, retry=None, timeout=RPC_TIMEOUT_SECONDS)

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)
