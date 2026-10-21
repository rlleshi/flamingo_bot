"""Bounded, aggregate-only widget feedback. Never accepts conversation content."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

SuggestionId = Literal[
    "movement",
    "diaspora",
    "pulsi",
    "peak_day",
    "referendum",
    "protest_map",
    "vlora_airport",
    "diaspora_help",
    "dossier",
    "zvernec",
    "why_protesting",
    "protest_day_140",
    "police_response",
    "protest_safety",
    "free_legal_help",
    "arrest_rights",
    "protest_preparation",
    "document_abuses",
    "detained_relative",
]
SUGGESTION_IDS: tuple[str, ...] = get_args(SuggestionId)
METRICS_PATH = "/v1/widget-metrics"
METRICS_COLLECTION = "widget_metrics"
RETENTION_DAYS = 90


class WidgetMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["shown", "clicked", "rated"]
    suggestion_ids: list[SuggestionId] = Field(default_factory=list, max_length=4)
    rating: Literal["helpful", "not_helpful"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        expected = {"shown": 4, "clicked": 1, "rated": 0}[self.kind]
        if len(self.suggestion_ids) != expected or len(set(self.suggestion_ids)) != expected:
            raise ValueError("Invalid suggestion count")
        if (self.kind == "rated") != (self.rating is not None):
            raise ValueError("Rating belongs only to answer feedback")
        return self


def utc_day(now: float) -> datetime:
    return datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def updated_bucket(
    previous: dict[str, Any], metric: WidgetMetric, day: datetime, daily_limit: int
) -> dict[str, Any] | None:
    """Construct only allowlisted aggregate fields, not a copy of a visitor event."""
    reports = int(previous.get("reports", 0))
    if reports >= daily_limit:
        return None
    shown = {key: int(previous.get("shown", {}).get(key, 0)) for key in SUGGESTION_IDS}
    clicked = {key: int(previous.get("clicked", {}).get(key, 0)) for key in SUGGESTION_IDS}
    ratings = {
        key: int(previous.get("ratings", {}).get(key, 0)) for key in ("helpful", "not_helpful")
    }
    if metric.kind == "rated":
        assert metric.rating is not None
        ratings[metric.rating] += 1
    else:
        counters = shown if metric.kind == "shown" else clicked
        for suggestion_id in metric.suggestion_ids:
            counters[suggestion_id] += 1
    return {
        "day": day,
        # A UTC day's totals are retained for AT MOST 90 days. This deadline never
        # slides when new reports arrive. TTL deletion itself is asynchronous.
        "expires_at": day + timedelta(days=RETENTION_DAYS),
        "reports": reports + 1,
        "shown": shown,
        "clicked": clicked,
        "ratings": ratings,
    }


def aggregate_report(buckets: Iterable[dict[str, Any]], now: float) -> dict[str, Any]:
    """Ignore expired buckets even while Firestore TTL deletion is pending."""
    instant = datetime.fromtimestamp(now, UTC)
    shown = dict.fromkeys(SUGGESTION_IDS, 0)
    clicked = dict.fromkeys(SUGGESTION_IDS, 0)
    ratings = dict.fromkeys(("helpful", "not_helpful"), 0)
    weeks: dict[str, dict[str, Any]] = {}
    for bucket in buckets:
        day, expires = bucket.get("day"), bucket.get("expires_at")
        if not isinstance(day, datetime) or not isinstance(expires, datetime):
            continue
        if expires <= instant or day > instant:
            continue
        week = (day - timedelta(days=day.weekday())).date().isoformat()
        summary = weeks.setdefault(week, {"shown": 0, "clicked": 0, **dict.fromkeys(ratings, 0)})
        for key in SUGGESTION_IDS:
            for field, totals in (("shown", shown), ("clicked", clicked)):
                count = int(bucket.get(field, {}).get(key, 0))
                totals[key] += count
                summary[field] += count
        for key in ratings:
            count = int(bucket.get("ratings", {}).get(key, 0))
            ratings[key] += count
            summary[key] += count
    return {
        "retention_days": RETENTION_DAYS,
        "suggestions": [
            {
                "id": key,
                "shown": shown[key],
                "clicked": clicked[key],
                "click_through_rate": clicked[key] / shown[key] if shown[key] else None,
            }
            for key in SUGGESTION_IDS
        ],
        "answer_ratings": ratings,
        "weeks": [{"week_start": key, **weeks[key]} for key in sorted(weeks)],
    }


class InMemoryWidgetMetrics:
    def __init__(
        self, daily_limit: int = 10_000, *, time_source: Callable[[], float] = time.time
    ) -> None:
        self.buckets: dict[str, dict[str, Any]] = {}
        self.daily_limit = daily_limit
        self._time_source = time_source
        self._lock = asyncio.Lock()

    async def record(self, metric: WidgetMetric) -> bool:
        async with self._lock:
            now = self._time_source()
            instant = datetime.fromtimestamp(now, UTC)
            self.buckets = {
                key: value for key, value in self.buckets.items() if value["expires_at"] > instant
            }
            day = utc_day(now)
            key = day.date().isoformat()
            value = updated_bucket(self.buckets.get(key, {}), metric, day, self.daily_limit)
            if value is None:
                return False
            self.buckets[key] = value
            return True

    async def report(self) -> dict[str, Any]:
        return aggregate_report(self.buckets.values(), self._time_source())
