"""Collect licensed Citizens protest articles locally, without cloud writes."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.error import URLError

from flamingo_bot.citizens import run_snapshot
from flamingo_bot.config import PROJECT_ROOT, Settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Collect protest-related Citizens.al full articles since 31 May 2026, "
            "then refresh new/edited posts incrementally. No model calls or cloud writes."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Preview without local writes")
    mode.add_argument("--write", action="store_true", help="Save local articles and checkpoint")
    parser.add_argument(
        "--output", type=Path, help="Override FLAMINGO_CITIZENS_DIR / .local/citizens"
    )
    parser.add_argument("--full-rescan", action="store_true", help="Recheck the entire date range")
    parser.add_argument("--max-articles", type=int, default=5000)
    parser.add_argument("--overlap-hours", type=int, default=48)
    parser.add_argument(
        "--delay", type=float, default=10, help="Request delay, at least 10 seconds"
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    output = args.output or Settings().flamingo_citizens_dir or PROJECT_ROOT / ".local/citizens"
    try:
        report = run_snapshot(
            output.resolve(),
            dry_run=args.dry_run,
            full_rescan=args.full_rescan,
            max_articles=args.max_articles,
            overlap_hours=args.overlap_hours,
            delay=args.delay,
            progress=lambda item: print(
                json.dumps({"progress": item}), file=sys.stderr, flush=True
            ),
        )
    except KeyboardInterrupt:
        print("Interrupted; index and checkpoint were not advanced.", file=sys.stderr)
        return 130
    except (ValueError, OSError, URLError) as exc:
        print(json.dumps({"error": str(exc), "checkpoint_advanced": False}), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
