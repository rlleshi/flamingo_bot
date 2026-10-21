"""Private, read-only report using the operator's GCP credentials. No public API."""

from __future__ import annotations

import asyncio
import json

from flamingo_bot.config import Settings
from flamingo_bot.providers.widget_metrics import FirestoreWidgetMetrics


async def report() -> None:
    store = FirestoreWidgetMetrics(Settings())
    try:
        print(json.dumps(await store.report(), indent=2))
    finally:
        await store.close()


def main() -> None:
    asyncio.run(report())


if __name__ == "__main__":
    main()
