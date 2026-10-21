"""Aggregate widget counters in the writable quota database, never the corpus."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import firestore_v1

from flamingo_bot.config import Settings
from flamingo_bot.errors import ProviderError
from flamingo_bot.metrics import (
    METRICS_COLLECTION,
    RETENTION_DAYS,
    WidgetMetric,
    aggregate_report,
    updated_bucket,
    utc_day,
)
from flamingo_bot.providers.firestore import firestore_credentials


class FirestoreWidgetMetrics:
    def __init__(
        self,
        settings: Settings,
        *,
        client: firestore_v1.Client | None = None,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self.client = (
            client
            if client is not None
            else firestore_v1.Client(
                project=settings.gcp_project_id,
                database=settings.flamingo_quota_database_id,
                credentials=firestore_credentials(settings),
            )
        )
        self.collection = self.client.collection(METRICS_COLLECTION)
        self.daily_limit = settings.flamingo_widget_metrics_daily_limit
        self._time_source = time_source

    def _record_sync(self, metric: WidgetMetric) -> bool:
        day = utc_day(self._time_source())
        reference = self.collection.document(day.date().isoformat())

        @firestore_v1.transactional
        def increment(transaction: firestore_v1.Transaction) -> bool:
            snapshot = reference.get(transaction=transaction, timeout=3)
            previous = snapshot.to_dict() or {} if snapshot.exists else {}
            value = updated_bucket(previous, metric, day, self.daily_limit)
            if value is None:
                return False
            transaction.set(reference, value)
            return True

        result: bool = increment(self.client.transaction(max_attempts=2))
        return result

    async def record(self, metric: WidgetMetric) -> bool:
        try:
            return await asyncio.to_thread(self._record_sync, metric)
        except GoogleAPICallError as exc:
            raise ProviderError("Widget aggregate update failed") from exc

    def _report_sync(self) -> dict[str, Any]:
        # At most 90 documents, including the current UTC day. Read exact day
        # references so expired records cannot leak into reporting or exports.
        now = self._time_source()
        day = utc_day(now)
        references = [
            self.collection.document((day - timedelta(days=offset)).date().isoformat())
            for offset in range(RETENTION_DAYS)
        ]
        buckets = [snapshot.to_dict() or {} for snapshot in self.client.get_all(references)]
        return aggregate_report(buckets, now)

    async def report(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._report_sync)

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)
