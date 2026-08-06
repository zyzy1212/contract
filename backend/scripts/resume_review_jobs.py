"""Re-enqueue review jobs that are stuck in ``running``.

Run from the backend directory:

    uv run --locked --extra dev python scripts/resume_review_jobs.py --minutes 15
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db import async_session_factory
from app.tasks.review_contract import review_contract_task


DEFAULT_STALE_MINUTES = 15


async def resume_stale_review_jobs(stale_minutes: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    factory = async_session_factory()
    async with factory() as session:
        result = await session.execute(
            text(
                """
                SELECT id::text AS id, tenant_id::text AS tenant_id
                FROM review_jobs
                WHERE status = 'running'
                  AND updated_at < :cutoff
                ORDER BY updated_at
                """
            ),
            {"cutoff": cutoff},
        )
        rows = result.mappings().all()
    for row in rows:
        review_contract_task.delay(row["id"], row["tenant_id"])
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--minutes",
        type=int,
        default=DEFAULT_STALE_MINUTES,
        help="re-enqueue running jobs untouched for this many minutes",
    )
    args = parser.parse_args()
    if args.minutes < 1:
        parser.error("--minutes must be positive")
    count = asyncio.run(resume_stale_review_jobs(args.minutes))
    print(f"re-enqueued {count} stale review job(s)")


if __name__ == "__main__":
    main()
