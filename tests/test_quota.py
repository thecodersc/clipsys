"""Unit tests for quota ledger, caps, and QuotaExceededError CLI handling."""

from __future__ import annotations

import json

import pytest

from clipsys.db.db import get_conn, init_db
from clipsys.publish.quota import QuotaExceededError, charge_units, check_quota, units_used_today


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr("clipsys._env.settings.db_path", db)
    init_db(db)
    return db


class TestQuotaLedger:
    def test_initial_zero(self, tmp_db):
        assert units_used_today("acct_01", "2024-01-01") == 0

    def test_charge_and_read(self, tmp_db):
        charge_units("acct_01", 1600, "2024-01-01")
        assert units_used_today("acct_01", "2024-01-01") == 1600

    def test_accumulates(self, tmp_db):
        charge_units("acct_01", 1600, "2024-01-01")
        charge_units("acct_01", 1600, "2024-01-01")
        assert units_used_today("acct_01", "2024-01-01") == 3200

    def test_per_account_isolation(self, tmp_db):
        charge_units("acct_01", 1600, "2024-01-01")
        assert units_used_today("acct_02", "2024-01-01") == 0

    def test_per_day_isolation(self, tmp_db):
        charge_units("acct_01", 1600, "2024-01-01")
        assert units_used_today("acct_01", "2024-01-02") == 0

    def test_check_passes_under_budget(self, tmp_db):
        charge_units("acct_01", 1600, "2024-01-01")
        assert check_quota("acct_01", 1600, budget=10_000, today="2024-01-01") is True

    def test_check_fails_over_budget(self, tmp_db):
        charge_units("acct_01", 9000, "2024-01-01")
        assert check_quota("acct_01", 1600, budget=10_000, today="2024-01-01") is False

    def test_check_exact_budget(self, tmp_db):
        charge_units("acct_01", 8400, "2024-01-01")
        # 8400 + 1600 = 10000 == budget, so allowed
        assert check_quota("acct_01", 1600, budget=10_000, today="2024-01-01") is True


class TestQuotaExceededError:
    def test_is_runtime_error(self):
        exc = QuotaExceededError("over budget")
        assert isinstance(exc, RuntimeError)

    def test_youtube_publisher_raises_on_exceeded_quota(self, tmp_db, monkeypatch):
        import clipsys.publish.youtube as yt_mod
        monkeypatch.setattr(yt_mod, "check_quota", lambda *a, **kw: False)
        from clipsys.publish.youtube import YouTubePublisher
        pub = YouTubePublisher(token_ref="tok", dry_run=True, quota_budget=10_000)
        with pytest.raises(QuotaExceededError):
            pub.publish(
                variant_id="var_x",
                file_path="/tmp/x.mp4",
                title="T",
                description="D",
                tags=[],
                account_id="acct_yt",
            )

    def test_cli_publish_marks_variant_held_on_quota_exceeded(self, monkeypatch):
        """QuotaExceededError from publisher must set variant status to 'held'."""
        from clipsys.config import Account, AccountsConfig, ProgramsConfig, RenderProfile, ThresholdsConfig

        with get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO sources
                   (id, file_path, program_id, creator, duration_s, received_at)
                   VALUES ('src_q', '/tmp/q.mp4', 'prog', 'c', 60, '2025-01-01T00:00:00Z')"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO clips (id, source_id, file_path, t_start, t_end, duration_s, score)
                   VALUES ('clip_q', 'src_q', '/tmp/q.mp4', 0, 30, 30, 1.5)"""
            )
            conn.execute(
                """INSERT INTO variants (id, clip_id, account_id, status, caption_json, file_path)
                   VALUES ('var_q', 'clip_q', 'acct_q', 'approved', ?, '/tmp/q_out.mp4')""",
                (json.dumps({"title": "Q", "description": "d", "hashtags": []}),),
            )

        acct = Account(
            id="acct_q",
            platform="youtube_shorts",
            handle="@q",
            token_ref="tok",
            stage=0,
            daily_cap=10,
            posting_windows=[],
            jitter_minutes=0,
            live=False,
            api_public=True,
            render_profile=RenderProfile(
                crop_offset="center", zoom=1.0, length_band="short", persona="p.md"
            ),
        )

        import clipsys.config as cfg
        monkeypatch.setattr(cfg, "load_accounts", lambda *a, **kw: AccountsConfig(accounts=[acct]))
        monkeypatch.setattr(cfg, "load_programs", lambda *a, **kw: ProgramsConfig(programs=[]))
        monkeypatch.setattr(cfg, "load_thresholds", lambda *a, **kw: ThresholdsConfig())
        import clipsys.publish.caps as caps
        monkeypatch.setattr(caps, "recently_published_to_other_account", lambda *a, **kw: False)
        import clipsys.telegram as tg
        monkeypatch.setattr(tg, "send_clip", lambda **kw: None)
        monkeypatch.setattr(tg, "send_alert", lambda *a, **kw: None)

        # Make YouTubePublisher.publish raise QuotaExceededError
        def _raise_quota(self, **kw):
            raise QuotaExceededError("quota")

        import clipsys.publish.youtube as yt_mod
        monkeypatch.setattr(yt_mod.YouTubePublisher, "publish", _raise_quota)

        from typer.testing import CliRunner
        from clipsys.cli import app
        result = CliRunner().invoke(app, ["publish"])
        assert result.exit_code == 0

        with get_conn() as conn:
            row = conn.execute(
                "SELECT status FROM variants WHERE id='var_q'"
            ).fetchone()
        assert row["status"] == "held"
