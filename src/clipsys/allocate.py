"""Allocate clips to accounts deterministically."""

from __future__ import annotations

import uuid
from datetime import date

from clipsys._log import get_logger
from clipsys.config import AccountsConfig, ThresholdsConfig, load_accounts, load_thresholds
from clipsys.db.db import get_conn

log = get_logger(__name__)


def _clips_published_today(account_id: str, today: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM variants
               WHERE account_id=? AND date(published_at)=? AND status='published'""",
            (account_id, today),
        ).fetchone()
    return row["cnt"] if row else 0


def _clips_allocated_today(account_id: str, today: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM variants
               WHERE account_id=? AND date(created_at)=?
               AND status NOT IN ('rejected','error')""",
            (account_id, today),
        ).fetchone()
    return row["cnt"] if row else 0


def _already_allocated(clip_id: str, account_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM variants WHERE clip_id=? AND account_id=?",
            (clip_id, account_id),
        ).fetchone()
    return row is not None


def run_allocate(
    accts_cfg: AccountsConfig | None = None,
    thr_cfg: ThresholdsConfig | None = None,
    today: str | None = None,
) -> dict[str, int]:
    accts_cfg = accts_cfg or load_accounts()
    thr_cfg = thr_cfg or load_thresholds()
    today_str = today or date.today().isoformat()

    max_per_clip = thr_cfg.allocation.max_accounts_per_clip
    max_clips_day = thr_cfg.allocation.max_clips_per_day
    min_score = thr_cfg.score.min_score

    # Clips sorted by score, only above threshold, not yet maxed out
    with get_conn() as conn:
        clips = conn.execute(
            """SELECT c.id, c.score, c.duration_s, s.program_id
               FROM clips c
               JOIN sources s ON s.id = c.source_id
               WHERE c.score >= ? AND c.status IN ('cut','allocated')
               ORDER BY c.score DESC
               LIMIT ?""",
            (min_score, max_clips_day),
        ).fetchall()

    day_clip_count = 0
    counts = {"allocated": 0, "skipped": 0}

    # Track how many accounts got each clip (across all accounts)
    clip_account_counts: dict[str, int] = {}
    for clip in clips:
        with get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) as cnt FROM variants WHERE clip_id=? AND status NOT IN ('rejected','error')",
                (clip["id"],),
            ).fetchone()["cnt"]
        clip_account_counts[clip["id"]] = n

    if day_clip_count >= max_clips_day:
        return counts

    # Deterministic: iterate accounts sorted by id, then clips sorted by score.
    accounts = sorted(accts_cfg.accounts, key=lambda a: a.id)

    for acct in accounts:
        cap = acct.daily_cap
        allocated_today = _clips_allocated_today(acct.id, today_str)

        for clip in clips:
            if allocated_today >= cap:
                break
            if clip_account_counts.get(clip["id"], 0) >= max_per_clip:
                continue
            if _already_allocated(clip["id"], acct.id):
                continue

            # Check length band compatibility
            band = acct.render_profile.length_band
            dur = clip["duration_s"]
            tc = thr_cfg.clips
            if band == "short" and not (tc.short.min_s <= dur <= tc.short.max_s):
                continue
            if band == "long" and not (tc.long.min_s <= dur <= tc.long.max_s):
                continue

            variant_id = str(uuid.uuid4())
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO variants (id, clip_id, account_id) VALUES (?,?,?)",
                    (variant_id, clip["id"], acct.id),
                )
                conn.execute(
                    "UPDATE clips SET status='allocated' WHERE id=?", (clip["id"],)
                )

            clip_account_counts[clip["id"]] = clip_account_counts.get(clip["id"], 0) + 1
            allocated_today += 1
            counts["allocated"] += 1
            day_clip_count += 1

            log.info(
                "variant_allocated",
                extra={
                    "variant_id": variant_id[:8],
                    "clip_id": clip["id"][:8],
                    "account": acct.id,
                },
            )

    return counts
