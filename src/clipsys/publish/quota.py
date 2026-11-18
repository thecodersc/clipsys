"""Daily YouTube quota ledger. Defaults to 10,000 units/day."""

from __future__ import annotations

from datetime import date

from clipsys._log import get_logger
from clipsys.db.db import get_conn

log = get_logger(__name__)

DEFAULT_BUDGET = 10_000
YT_UPLOAD_UNITS = 1_600


class QuotaExceededError(RuntimeError):
    """Raised when a publish attempt would exceed the daily quota budget."""


def units_used_today(account_id: str, today: str | None = None) -> int:
    today = today or date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT units_used FROM quota_ledger WHERE account_id=? AND date=?",
            (account_id, today),
        ).fetchone()
    return row["units_used"] if row else 0


def charge_units(account_id: str, units: int, today: str | None = None) -> None:
    today = today or date.today().isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO quota_ledger (account_id, date, units_used)
               VALUES (?,?,?)
               ON CONFLICT(account_id, date) DO UPDATE SET units_used = units_used + excluded.units_used""",
            (account_id, today, units),
        )
    log.info("quota_charged", extra={"account_id": account_id, "units": units, "date": today})


def check_quota(account_id: str, units: int, budget: int = DEFAULT_BUDGET, today: str | None = None) -> bool:
    """Return True if the account can afford `units` today."""
    used = units_used_today(account_id, today)
    if used + units > budget:
        log.warning(
            "quota_exceeded",
            extra={"account_id": account_id, "used": used, "requested": units, "budget": budget},
        )
        return False
    return True
