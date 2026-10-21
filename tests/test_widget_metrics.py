from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from flamingo_bot.config import Settings
from flamingo_bot.metrics import (
    METRICS_COLLECTION,
    RETENTION_DAYS,
    SUGGESTION_IDS,
    InMemoryWidgetMetrics,
    WidgetMetric,
    aggregate_report,
)
from flamingo_bot.providers import widget_metrics as provider


def test_suggestion_ids_match_the_frontend_bank() -> None:
    source = Path("frontend/src/lib/suggestions.ts").read_text()
    assert set(SUGGESTION_IDS) == set(re.findall(r'id: "([a-z_0-9]+)"', source))


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "shown", "suggestion_ids": ["movement"]},
        {"kind": "shown", "suggestion_ids": ["movement"] * 4},
        {"kind": "clicked", "suggestion_ids": ["unknown"]},
        {"kind": "clicked", "suggestion_ids": ["movement", "diaspora"]},
        {"kind": "rated", "rating": "helpful", "suggestion_ids": ["movement"]},
        {"kind": "rated", "rating": "custom text"},
        {"kind": "rated"},
        {"kind": "rated", "rating": "helpful", "answer_id": "abc"},
        {"kind": "clicked", "suggestion_ids": ["movement"], "question": "private"},
        {"kind": "clicked", "suggestion_ids": ["movement"], "visitor_id": "abc"},
    ],
)
def test_metrics_reject_content_identifiers_and_invalid_shapes(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WidgetMetric.model_validate(payload)


@pytest.mark.asyncio
async def test_only_daily_totals_survive_and_ratings_are_not_linked_to_topics() -> None:
    now = datetime(2026, 10, 21, 12, tzinfo=UTC).timestamp()
    store = InMemoryWidgetMetrics(time_source=lambda: now)
    for _ in range(2):
        await store.record(WidgetMetric(kind="shown", suggestion_ids=list(SUGGESTION_IDS[:4])))
    await store.record(WidgetMetric(kind="clicked", suggestion_ids=["movement"]))
    await store.record(WidgetMetric(kind="rated", rating="helpful"))
    await store.record(WidgetMetric(kind="rated", rating="not_helpful"))
    assert len(store.buckets) == 1
    bucket = next(iter(store.buckets.values()))
    assert set(bucket) == {"day", "expires_at", "reports", "shown", "clicked", "ratings"}
    assert bucket["reports"] == 5
    assert bucket["shown"]["movement"] == 2
    assert bucket["clicked"]["movement"] == 1
    assert bucket["ratings"] == {"helpful": 1, "not_helpful": 1}
    report = await store.report()
    movement = report["suggestions"][0]
    assert movement == {"id": "movement", "shown": 2, "clicked": 1, "click_through_rate": 0.5}
    assert report["answer_ratings"] == bucket["ratings"]
    assert report["weeks"][0]["shown"] == 8
    for forbidden in ("visitor", "session", "question", "answer_id", "request_id", "event"):
        assert forbidden not in json.dumps(bucket, default=str)


@pytest.mark.asyncio
async def test_retention_is_rolling_and_never_extended_by_new_reports() -> None:
    day = datetime(2026, 10, 21, tzinfo=UTC)
    now = day.timestamp()
    store = InMemoryWidgetMetrics(time_source=lambda: now)
    metric = WidgetMetric(kind="rated", rating="helpful")
    await store.record(metric)
    expiry = store.buckets["2026-10-21"]["expires_at"]
    now += 86_399
    await store.record(metric)
    assert store.buckets["2026-10-21"]["expires_at"] == expiry
    now += 1
    await store.record(metric)
    now = (day + timedelta(days=RETENTION_DAYS)).timestamp()
    assert (await store.report())["answer_ratings"]["helpful"] == 1
    # Physical cleanup need not have happened for an expired total to be excluded.
    assert "2026-10-21" in store.buckets
    await store.record(metric)
    assert "2026-10-21" not in store.buckets
    assert (await store.report())["answer_ratings"]["helpful"] == 2


@pytest.mark.asyncio
async def test_service_wide_daily_cap_bounds_writes() -> None:
    store = InMemoryWidgetMetrics(daily_limit=1)
    assert await store.record(WidgetMetric(kind="rated", rating="helpful"))
    assert not await store.record(WidgetMetric(kind="rated", rating="not_helpful"))
    assert (await store.report())["answer_ratings"] == {"helpful": 1, "not_helpful": 0}


def test_invalid_or_expired_reporting_documents_are_ignored() -> None:
    assert aggregate_report([{}, {"day": "invalid"}], 0)["weeks"] == []


@pytest.mark.asyncio
async def test_firestore_uses_quota_database_and_one_aggregate_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider.firestore_v1, "transactional", lambda function: function)
    client = MagicMock()
    monkeypatch.setattr(provider.firestore_v1, "Client", lambda **kwargs: client)
    captured: dict[str, object] = {}

    def create_client(**kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return client

    monkeypatch.setattr(provider.firestore_v1, "Client", create_client)
    snapshot = client.collection.return_value.document.return_value.get.return_value
    snapshot.exists = False
    store = provider.FirestoreWidgetMetrics(
        Settings(_env_file=None, gcp_project_id="test-project"),
        time_source=lambda: datetime(2026, 10, 21, tzinfo=UTC).timestamp(),
    )
    assert await store.record(WidgetMetric(kind="rated", rating="helpful"))
    assert captured["database"] == "flamingo-quotas"
    client.collection.assert_called_once_with(METRICS_COLLECTION)
    client.collection.return_value.document.assert_called_once_with("2026-10-21")
    payload = client.transaction.return_value.set.call_args.args[1]
    assert payload["ratings"]["helpful"] == 1
    assert payload["expires_at"] == datetime(2027, 1, 19, tzinfo=UTC)
    await store.close()
    client.close.assert_called_once()
