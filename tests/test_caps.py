"""Unit tests for per-account caps, windows, and stage rules."""

from __future__ import annotations

from datetime import time

from clipsys.config import Account, RenderProfile
from clipsys.publish.caps import can_publish, time_in_window


def _make_account(stage: int = 0, daily_cap: int = 2, live: bool = False) -> Account:
    return Account(
        id="acct_test",
        platform="youtube_shorts",
        handle="@test",
        token_ref="tok",
        stage=stage,
        daily_cap=daily_cap,
        posting_windows=["12:00-14:00"],
        jitter_minutes=0,
        live=live,
        render_profile=RenderProfile(
            crop_offset="center",
            zoom=1.0,
            length_band="short",
            persona="personas/acct_01.md",
        ),
    )


class TestTimeInWindow:
    def test_in_window(self):
        assert time_in_window(time(13, 0), time(12, 0), time(14, 0))

    def test_at_start(self):
        assert time_in_window(time(12, 0), time(12, 0), time(14, 0))

    def test_at_end(self):
        assert time_in_window(time(14, 0), time(12, 0), time(14, 0))

    def test_outside_window(self):
        assert not time_in_window(time(11, 59), time(12, 0), time(14, 0))


class TestCanPublish:
    def test_stage0_approved_ok(self, tmp_path, monkeypatch):
        from clipsys.db.db import init_db
        monkeypatch.setattr("clipsys._env.settings.db_path", tmp_path / "test.db")
        init_db(tmp_path / "test.db")
        acct = _make_account(stage=0)
        allowed, reason = can_publish(acct, "approved", 0.9, 30, today="2024-01-01")
        assert allowed
        assert reason == "ok"

    def test_stage0_captioned_blocked(self, tmp_path, monkeypatch):
        from clipsys.db.db import init_db
        monkeypatch.setattr("clipsys._env.settings.db_path", tmp_path / "test.db")
        init_db(tmp_path / "test.db")
        acct = _make_account(stage=0)
        allowed, reason = can_publish(acct, "captioned", 0.9, 30, today="2024-01-01")
        assert not allowed
        assert "stage_0" in reason

    def test_stage0_cap_reached(self, tmp_path, monkeypatch):
        from clipsys.db.db import get_conn, init_db
        db = tmp_path / "test.db"
        monkeypatch.setattr("clipsys._env.settings.db_path", db)
        init_db(db)
        # Insert FK-ordered rows first, then 2 published variants today
        with get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO sources
                   (id, file_path, program_id, creator, duration_s, received_at)
                   VALUES ('src_x', '/tmp/x.mp4', 'prog', 'c', 60, '2024-01-01T00:00:00Z')"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO clips (id, source_id, file_path, t_start, t_end, duration_s, score)
                   VALUES ('clip_x', 'src_x', '/tmp/x.mp4', 0, 30, 30, 1.5)"""
            )
            for i in range(2):
                conn.execute(
                    """INSERT INTO variants (id, clip_id, account_id, status, published_at)
                       VALUES (?,?,?,?,?)""",
                    (f"vid_{i}", "clip_x", "acct_test", "published", "2024-01-01T12:00:00Z"),
                )
        acct = _make_account(stage=0, daily_cap=2)
        allowed, reason = can_publish(acct, "approved", 0.9, 30, today="2024-01-01")
        assert not allowed
        assert "daily_cap" in reason

    def test_stage2_always_allowed(self, tmp_path, monkeypatch):
        from clipsys.db.db import init_db
        monkeypatch.setattr("clipsys._env.settings.db_path", tmp_path / "test.db")
        init_db(tmp_path / "test.db")
        acct = _make_account(stage=2)
        allowed, _ = can_publish(acct, "captioned", 0.9, 30, today="2024-01-01")
        assert allowed
