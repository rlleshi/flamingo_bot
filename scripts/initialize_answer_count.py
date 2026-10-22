"""Preview, then create a historical aggregate once. Never reset a live total."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

from google.api_core.exceptions import AlreadyExists, GoogleAPICallError

from flamingo_bot.answer_count import ANSWER_COUNT_COLLECTION, ANSWER_COUNT_DOCUMENT, AnswerCount
from flamingo_bot.config import Settings
from flamingo_bot.errors import FlamingoBotError
from flamingo_bot.providers.answer_count import FirestoreAnswerCounter


def parse_date(value: str) -> datetime:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use an ISO-8601 timestamp with timezone") from exc
    if date.tzinfo is None or date.utcoffset() is None:
        raise argparse.ArgumentTypeError("Baseline timestamp must include a timezone")
    if date > datetime.now(UTC):
        raise argparse.ArgumentTypeError("Baseline timestamp cannot be in the future")
    return date.astimezone(UTC)


async def run(args: argparse.Namespace) -> int:
    baseline = AnswerCount(answered=args.baseline)
    settings = Settings()
    if not settings.gcp_project_id:
        raise ValueError("GCP_PROJECT_ID is required")
    if settings.flamingo_quota_database_id == settings.firestore_database_id:
        raise ValueError("Answer counters must not use the knowledge database")
    store = FirestoreAnswerCounter(settings)
    try:
        current = await store.read()
        preview = {
            "database": settings.flamingo_quota_database_id,
            "document": f"{ANSWER_COUNT_COLLECTION}/{ANSWER_COUNT_DOCUMENT}",
            "baseline": baseline.answered,
            "baseline_as_of": args.baseline_as_of.isoformat(),
            "existing_total": current.answered if current is not None else None,
            "mode": "initialize" if args.initialize else "dry-run",
        }
        if args.initialize:
            # Never use set/merge: a racing deployment/operator must not reset an
            # existing counter. Read is informative; create is the actual fence.
            if current is not None:
                raise ValueError("Counter already exists; it will not be overwritten")
            await asyncio.to_thread(store.initialize, baseline.answered, args.baseline_as_of)
        print(json.dumps(preview, indent=2))
        return 0
    finally:
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=int, required=True)
    parser.add_argument("--baseline-as-of", type=parse_date, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read-only preview (default)")
    mode.add_argument(
        "--initialize", action="store_true", help="Create only; requires release approval"
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except AlreadyExists:
        parser.exit(1, "Counter already exists; nothing overwritten.\n")
    except (FlamingoBotError, GoogleAPICallError, ValueError) as exc:
        # SDK messages may include private project paths; report only the class.
        parser.exit(1, f"Counter initialization failed ({type(exc).__name__}); nothing reset.\n")


if __name__ == "__main__":
    raise SystemExit(main())
