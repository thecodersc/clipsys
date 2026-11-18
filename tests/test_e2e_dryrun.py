"""End-to-end dry-run tests.

Verifies the full publish path with zero real network calls:
 - api_public=False → Telegram queue, no platform API call
 - KILL switch → alert sent, no publish attempt
 - QuotaExceededError → variant held, alert sent
All httpx / google-api calls must stay mocked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipsys.config import Account, AccountsConfig, ProgramsConfig, RenderProfile, ThresholdsConfig
from clipsys.db.db import get_conn


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _make_acct(account_id: str, api_public: bool, stage: int = 0) -> Account:
    return Account(
        id=account_id,
        platform="youtube_shorts",
        handle="@e2e",
        token_ref="tok",
        stage=stage,
        daily_cap=10,
        posting_windows=[],
        jitter_minutes=0,
        live=False,
        api_public=api_public,
        render_profile=RenderProfile(
            crop_offset="center", zoom=1.0, length_band="short", persona="p.md"
        ),
    )


def _seed_db(account_id: str, variant_id: str = "var_e2e"):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO sources
               (id, file_path, program_id, creator, duration_s, received_at)
               VALUES ('src_e2e', '/tmp/e2e.mp4', 'prog', 'c', 60, '2025-01-01T00:00:00Z')"""
        )
        conn.execute(
            """INSERT OR IGNORE INTO clips (id, source_id, file_path, t_start, t_end, duration_s, score)
               VALUES ('clip_e2e', 'src_e2e', '/tmp/e2e.mp4', 0, 30, 30, 1.5)"""
        )
        conn.execute(
            """INSERT OR REPLACE INTO variants
               (id, clip_id, account_id, status, caption_json, file_path)
               VALUES (?,?,?,?,?,?)""",
            (
                variant_id, "clip_e2e", account_id, "approved",
                json.dumps({"title": "E2E", "description": "d", "hashtags": []}),
                "/tmp/e2e_out.mp4",
            ),
        )


def _patch_config(monkeypatch, acct: Account):
    import clipsys.config as cfg
    monkeypatch.setattr(cfg, "load_accounts", lambda *a, **kw: AccountsConfig(accounts=[acct]))
    monkeypatch.setattr(cfg, "load_programs", lambda *a, **kw: ProgramsConfig(programs=[]))
    monkeypatch.setattr(cfg, "load_thresholds", lambda *a, **kw: ThresholdsConfig())
    import clipsys.publish.caps as caps
    monkeypatch.setattr(caps, "recently_published_to_other_account", lambda *a, **kw: False)


def _invoke_publish(tmp_path: Path | None = None, extra_args: list[str] | None = None):
    from typer.testing import CliRunner
    from clipsys.cli import app
    return CliRunner().invoke(app, ["publish"] + (extra_args or []))


# ─── KILL switch ──────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_kill_switch_exits_nonzero(self, tmp_path, monkeypatch):
        kill_file = tmp_path / "data" / "KILL"
        kill_file.parent.mkdir(parents=True)
        kill_file.touch()

        alerts = []
        import clipsys.telegram as tg
        monkeypatch.setattr(tg, "send_alert", lambda msg, **kw: alerts.append(msg))

        # Patch Path.exists only for the KILL file by overriding at cli module level
        import clipsys.cli as cli_mod
        original_path_cls = cli_mod.Path

        class FakePath(original_path_cls):
            def exists(self):
                if self.name == "KILL":
                    return True
                return super().exists()

        monkeypatch.setattr(cli_mod, "Path", FakePath)

        result = _invoke_publish()
        assert result.exit_code == 1
        assert any("KILL" in a for a in alerts)

    def test_kill_switch_does_not_publish(self, tmp_path, monkeypatch):
        """With KILL present, no variants should be published."""
        _seed_db("acct_kill", "var_kill")
        acct = _make_acct("acct_kill", api_public=True, stage=0)
        _patch_config(monkeypatch, acct)

        import clipsys.telegram as tg
        monkeypatch.setattr(tg, "send_alert", lambda *a, **kw: None)
        monkeypatch.setattr(tg, "send_clip", lambda **kw: None)

        pub_calls = []

        class FakePublishResult:
            platform_post_id = "fake"
            dry_run = True
            metadata = {}

        class FakeYT:
            def __init__(self, *a, **kw):
                pass

            def publish(self, **kw):
                pub_calls.append(kw)
                return FakePublishResult()

        import clipsys.publish.youtube as yt_mod
        monkeypatch.setattr(yt_mod, "YouTubePublisher", FakeYT)

        import clipsys.cli as cli_mod
        original_path_cls = cli_mod.Path

        class FakePath(original_path_cls):
            def exists(self):
                if self.name == "KILL":
                    return True
                return super().exists()

        monkeypatch.setattr(cli_mod, "Path", FakePath)

        _invoke_publish()
        assert pub_calls == []


# ─── api_public=False → Telegram, no platform call ───────────────────────────

class TestE2eApiPublicFalse:
    def test_no_platform_calls_zero_network(self, monkeypatch):
        """Full path: api_public=False, dry-run → Telegram called, platform API never touched."""
        _seed_db("acct_tg")
        acct = _make_acct("acct_tg", api_public=False, stage=0)
        _patch_config(monkeypatch, acct)

        tg_calls = []
        import clipsys.telegram as tg
        monkeypatch.setattr(tg, "send_clip", lambda **kw: (tg_calls.append(kw), "42")[1])
        monkeypatch.setattr(tg, "send_alert", lambda *a, **kw: None)

        # Confirm no real httpx calls by ensuring httpx.post / httpx.get are not the real ones
        import httpx
        real_post = httpx.post

        def _assert_no_real_call(*a, **kw):
            raise AssertionError("httpx.post must not be called directly in e2e dry-run")

        monkeypatch.setattr(httpx, "post", _assert_no_real_call)

        result = _invoke_publish()
        assert result.exit_code == 0

        # Telegram send_clip was called (via our mock, not httpx directly)
        assert len(tg_calls) == 1

        with get_conn() as conn:
            row = conn.execute(
                "SELECT status FROM variants WHERE id='var_e2e'"
            ).fetchone()
        assert row["status"] == "held"

    def test_variant_status_held_persisted(self, monkeypatch):
        _seed_db("acct_tg2", "var_tg2")
        acct = _make_acct("acct_tg2", api_public=False, stage=0)
        _patch_config(monkeypatch, acct)

        import clipsys.telegram as tg
        monkeypatch.setattr(tg, "send_clip", lambda **kw: "99")
        monkeypatch.setattr(tg, "send_alert", lambda *a, **kw: None)

        _invoke_publish()

        with get_conn() as conn:
            row = conn.execute(
                "SELECT status, telegram_sent_at FROM variants WHERE id='var_tg2'"
            ).fetchone()
        assert row["status"] == "held"
        assert row["telegram_sent_at"] is not None


# ─── Quota exceeded → held, alert sent ───────────────────────────────────────

class TestE2eQuotaExhausted:
    def test_quota_exceeded_marks_held_and_alerts(self, monkeypatch):
        _seed_db("acct_quota")
        acct = _make_acct("acct_quota", api_public=True, stage=0)
        _patch_config(monkeypatch, acct)

        from clipsys.publish.quota import QuotaExceededError
        alerts = []
        import clipsys.telegram as tg
        monkeypatch.setattr(tg, "send_clip", lambda **kw: None)
        monkeypatch.setattr(tg, "send_alert", lambda msg, **kw: alerts.append(msg))

        def _raise_quota(self, **kw):
            raise QuotaExceededError("quota exhausted")

        import clipsys.publish.youtube as yt_mod
        monkeypatch.setattr(yt_mod.YouTubePublisher, "publish", _raise_quota)

        result = _invoke_publish()
        assert result.exit_code == 0

        with get_conn() as conn:
            row = conn.execute(
                "SELECT status FROM variants WHERE id='var_e2e'"
            ).fetchone()
        assert row["status"] == "held"
        assert any("quota" in a.lower() for a in alerts)
