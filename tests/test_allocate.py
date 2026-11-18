"""Unit tests for allocation caps and determinism."""

from __future__ import annotations

import uuid

import pytest

from clipsys.allocate import run_allocate
from clipsys.config import Account, AccountsConfig, RenderProfile, ThresholdsConfig
from clipsys.db.db import get_conn, init_db


def _mk_account(aid: str, platform: str = "youtube_shorts", cap: int = 2, band: str = "short") -> Account:
    return Account(
        id=aid,
        platform=platform,  # type: ignore[arg-type]
        handle=f"@{aid}",
        token_ref=f"tok_{aid}",
        stage=0,
        daily_cap=cap,
        posting_windows=["12:00-14:00"],
        jitter_minutes=0,
        live=False,
        render_profile=RenderProfile(
            crop_offset="center", zoom=1.0, length_band=band, persona="personas/acct_01.md"
        ),
    )


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "test.db"
    monkeypatch.setattr("clipsys._env.settings.db_path", p)
    init_db(p)
    # Insert a dummy source, transcript, moment, and clip
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sources (id, file_path, program_id, creator, duration_s, received_at)
               VALUES ('src1', '/tmp/s.mp4', 'prog', 'c', 60, '2024-01-01T00:00:00Z')"""
        )
        conn.execute(
            "INSERT INTO transcripts (source_id, segments_json) VALUES ('src1', '[]')"
        )
        conn.execute(
            "INSERT INTO moments (source_id, t_start, t_end, score, signals_json) VALUES ('src1', 0, 30, 2.5, '{}')"
        )
        clip_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO clips (id, source_id, file_path, t_start, t_end, duration_s, score)
               VALUES (?, 'src1', '/tmp/c.mp4', 0, 30, 30, 2.5)""",
            (clip_id,),
        )
    return clip_id


class TestAllocate:
    def test_basic_allocation(self, db, monkeypatch):
        accts = AccountsConfig(accounts=[
            _mk_account("a1"),
            _mk_account("a2"),
            _mk_account("a3"),
        ])
        thr = ThresholdsConfig()
        counts = run_allocate(accts, thr, today="2024-01-01")
        assert counts["allocated"] >= 1

    def test_max_accounts_per_clip(self, db, monkeypatch):
        clip_id = db
        accts = AccountsConfig(accounts=[
            _mk_account("a1"),
            _mk_account("a2"),
            _mk_account("a3"),
            _mk_account("a4"),
        ])
        thr = ThresholdsConfig()
        # max_accounts_per_clip defaults to 3
        run_allocate(accts, thr, today="2024-01-01")
        with get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) as n FROM variants WHERE clip_id=?", (clip_id,)
            ).fetchone()["n"]
        assert n <= thr.allocation.max_accounts_per_clip

    def test_idempotent(self, db):
        clip_id = db
        accts = AccountsConfig(accounts=[_mk_account("a1")])
        thr = ThresholdsConfig()
        run_allocate(accts, thr, today="2024-01-01")
        run_allocate(accts, thr, today="2024-01-01")
        with get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) as n FROM variants WHERE account_id='a1' AND clip_id=?", (clip_id,)
            ).fetchone()["n"]
        assert n == 1

    def test_daily_cap_respected(self, db, monkeypatch):
        """With cap=1 account should only get 1 variant today."""
        accts = AccountsConfig(accounts=[_mk_account("a1", cap=1)])
        thr = ThresholdsConfig()
        # Insert a second clip so there are 2 candidates
        with get_conn() as conn:
            cid2 = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO clips (id, source_id, file_path, t_start, t_end, duration_s, score)
                   VALUES (?, 'src1', '/tmp/c2.mp4', 31, 61, 30, 2.0)""",
                (cid2,),
            )
        run_allocate(accts, thr, today="2024-01-01")
        with get_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) as n FROM variants WHERE account_id='a1'"
            ).fetchone()["n"]
        assert n <= 1
