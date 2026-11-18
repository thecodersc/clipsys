"""Enforce per-account daily caps, posting windows, jitter, stage rules, and cross-account dedup."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta

from clipsys._log import get_logger
from clipsys.config import Account
from clipsys.db.db import get_conn

log = get_logger(__name__)


def _parse_window(window_str: str) -> tuple[time, time]:
    start_s, end_s = window_str.split("-")
    sh, sm = map(int, start_s.split(":"))
    eh, em = map(int, end_s.split(":"))
    return time(sh, sm), time(eh, em)


def next_publish_time(account: Account, now: datetime | None = None) -> datetime:
    """Return the next allowed publish datetime for this account (UTC)."""
    now = now or datetime.now(UTC)
    today = now.date()

    for window_str in account.posting_windows:
        win_start, win_end = _parse_window(window_str)
        jitter = random.randint(0, account.jitter_minutes)
        candidate = datetime.combine(today, win_start, tzinfo=UTC) + timedelta(minutes=jitter)
        if candidate > now and time_in_window(now.time(), win_start, win_end):
            return candidate
        # Try the full window start if we haven't passed it
        win_dt = datetime.combine(today, win_start, tzinfo=UTC)
        if win_dt > now:
            return win_dt + timedelta(minutes=jitter)

    # No window today; try tomorrow's first window
    if account.posting_windows:
        win_start, _ = _parse_window(account.posting_windows[0])
        tomorrow = today + timedelta(days=1)
        jitter = random.randint(0, account.jitter_minutes)
        return datetime.combine(tomorrow, win_start, tzinfo=UTC) + timedelta(minutes=jitter)

    return now  # no windows configured → allow immediately


def time_in_window(t: time, start: time, end: time) -> bool:
    return start <= t <= end


def _published_today(account_id: str, today: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM variants WHERE account_id=? AND date(published_at)=? AND status='published'",
            (account_id, today),
        ).fetchone()
    return row["cnt"] if row else 0


def _review_pass_rate(account_id: str, min_reviews: int) -> tuple[float, int]:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT
                COUNT(*) FILTER (WHERE status='approved') as approved,
                COUNT(*) FILTER (WHERE status='rejected') as rejected
               FROM variants WHERE account_id=?""",
            (account_id,),
        ).fetchone()
    approved = row["approved"] or 0
    rejected = row["rejected"] or 0
    total = approved + rejected
    if total < min_reviews:
        return 0.0, total
    return approved / total, total


def recently_published_to_other_account(
    clip_id: str,
    account_id: str,
    window_minutes: int = 60,
    now: datetime | None = None,
) -> bool:
    """Return True if any OTHER account published from the same clip within window_minutes.

    Prevents posting the same underlying clip to multiple accounts within the window.
    Only counts variants with status='published' (api_public=False → 'held' is excluded).
    """
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM variants
               WHERE clip_id=? AND account_id!=?
               AND status='published' AND published_at>=?""",
            (clip_id, account_id, cutoff),
        ).fetchone()
    result = row["cnt"] > 0
    if result:
        log.warning(
            "cross_account_dedup_blocked",
            extra={"clip_id": clip_id[:8], "account_id": account_id, "window_minutes": window_minutes},
        )
    return result


def can_publish(
    account: Account,
    variant_status: str,
    auto_pass_rate: float,
    min_reviews: int,
    today: str | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    today = today or date.today().isoformat()

    published = _published_today(account.id, today)
    if published >= account.daily_cap:
        return False, f"daily_cap_reached ({published}/{account.daily_cap})"

    if account.stage == 0:
        if variant_status != "approved":
            return False, "stage_0_requires_approved"
        return True, "ok"

    if account.stage == 1:
        pass_rate, total = _review_pass_rate(account.id, min_reviews)
        if total >= min_reviews and pass_rate >= auto_pass_rate:
            return True, "auto_publish_stage_1"
        if variant_status == "approved":
            return True, "manually_approved"
        return False, f"stage_1_pass_rate_insufficient ({pass_rate:.2f} over {total} reviews)"

    if account.stage == 2:
        return True, "full_cap_stage_2"

    return False, "unknown_stage"
