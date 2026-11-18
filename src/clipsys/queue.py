"""Simple SQLite job queue with retries and dead-letter handling."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from clipsys._log import get_logger
from clipsys.db.db import get_conn

log = get_logger(__name__)

MAX_ATTEMPTS = 3
DEAD_AFTER = MAX_ATTEMPTS


def enqueue(job_type: str, payload: dict[str, Any], run_after: str | None = None) -> int:
    run_after = run_after or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (type, payload, run_after) VALUES (?,?,?)",
            (job_type, json.dumps(payload), run_after),
        )
        return cur.lastrowid  # type: ignore[return-value]


def _claim_next(job_type: str | None = None) -> dict | None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        query = """SELECT * FROM jobs
                   WHERE status='pending' AND run_after <= ?
                   AND attempts < ?"""
        params: list[Any] = [now, DEAD_AFTER]
        if job_type:
            query += " AND type=?"
            params.append(job_type)
        query += " ORDER BY created_at LIMIT 1"
        row = conn.execute(query, params).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
            (now, row["id"]),
        )
        return dict(row)


def _complete(job_id: int) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', updated_at=? WHERE id=?",
            (now, job_id),
        )


def _fail(job_id: int, error: str, attempts: int) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = "dead" if attempts >= DEAD_AFTER else "pending"
    # Exponential backoff: 2^attempts minutes
    delay_s = min(60 * (2 ** attempts), 3600)
    from datetime import timedelta
    run_after_dt = datetime.now(UTC) + timedelta(seconds=delay_s)
    run_after = run_after_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, last_error=?, run_after=?, updated_at=? WHERE id=?",
            (status, error[:2000], run_after, now, job_id),
        )
    if status == "dead":
        log.error("job_dead", extra={"job_id": job_id, "err": error[:200]})


def run_worker(handler: Callable[[str, dict], None], job_type: str | None = None, once: bool = True) -> None:
    """Process pending jobs. If once=True, process all current pending jobs then return."""
    while True:
        job = _claim_next(job_type)
        if not job:
            if once:
                break
            time.sleep(5)
            continue

        log.info("job_start", extra={"job_id": job["id"], "type": job["type"]})
        try:
            handler(job["type"], json.loads(job["payload"]))
            _complete(job["id"])
            log.info("job_done", extra={"job_id": job["id"]})
        except Exception as exc:
            _fail(job["id"], str(exc), job["attempts"])
            log.error("job_failed", extra={"job_id": job["id"], "err": str(exc)})
        if once:
            # Keep draining
            continue
