"""Tests for api_public=False routing and 60-min cross-account dedup."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from clipsys.config import Account, AccountsConfig, ProgramsConfig, RenderProfile, ThresholdsConfig
from clipsys.db.db import get_conn


# ─── Fixtures / helpers ───────────────────────────────────────────────────────

def _insert_base(conn, source_id="src_r", clip_id="clip_r"):
    conn.execute(
        """INSERT OR IGNORE INTO sources
           (id, file_path, program_id, creator, duration_s, received_at)
           VALUES (?,?,?,?,?,?)""",
        (source_id, "/tmp/x.mp4", "prog_r", "creator", 60, "2025-01-01T00:00:00Z"),
    )
    conn.execute(
        """INSERT OR IGNORE INTO clips (id, source_id, file_path, t_start, t_end, duration_s, score)
           VALUES (?,?,?,?,?,?,?)""",
        (clip_id, source_id, "/tmp/x.mp4", 0, 30, 30, 1.5),
    )


def _insert_variant(conn, variant_id, clip_id="clip_r", account_id="acct_a",
                    status="approved", published_at=None, telegram_sent_at=None):
    conn.execute(
        """INSERT INTO variants
           (id, clip_id, account_id, status, published_at, telegram_sent_at,
            caption_json, file_path)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            variant_id, clip_id, account_id, status, published_at, telegram_sent_at,
            json.dumps({"title": "Test Clip", "description": "desc", "hashtags": []}),
            "/tmp/out.mp4",
        ),
    )


def _make_account(api_public: bool, account_id: str = "acct_a", stage: int = 0) -> Account:
    return Account(
        id=account_id,
        platform="youtube_shorts",
        handle="@test",
        token_ref="tok",
        stage=stage,
        daily_cap=10,
        posting_windows=[],
        jitter_minutes=0,
        live=False,
        api_public=api_public,
        render_profile=RenderProfile(
            crop_offset="center",
            zoom=1.0,
            length_band="short",
            persona="personas/acct_a.md",
        ),
    )


class _FakeAccounts:
    def __init__(self, *accounts):
        self.accounts = list(accounts)

    def by_id(self, account_id):
        for a in self.accounts:
            if a.id == account_id:
                return a
        raise KeyError(account_id)


def _patch_cli(monkeypatch, account, tg_send_clip=None, tg_send_alert=None):
    """Patch config loaders and telegram helpers for the publish command."""
    import clipsys.config as cfg
    monkeypatch.setattr(cfg, "load_accounts", lambda *a, **kw: _FakeAccounts(account))
    monkeypatch.setattr(cfg, "load_programs", lambda *a, **kw: ProgramsConfig(programs=[]))
    monkeypatch.setattr(cfg, "load_thresholds", lambda *a, **kw: ThresholdsConfig())
    import clipsys.publish.caps as caps
    monkeypatch.setattr(caps, "recently_published_to_other_account", lambda *a, **kw: False)
    import clipsys.telegram as tg
    monkeypatch.setattr(tg, "send_clip", tg_send_clip or (lambda **kw: "9999"))
    monkeypatch.setattr(tg, "send_alert", tg_send_alert or (lambda *a, **kw: None))


def _invoke_publish():
    from typer.testing import CliRunner
    from clipsys.cli import app
    return CliRunner().invoke(app, ["publish"])


# ─── Cross-account dedup unit tests ──────────────────────────────────────────

class TestRecentlyPublishedToOtherAccount:
    def test_no_other_publishes_returns_false(self):
        from clipsys.publish.caps import recently_published_to_other_account
        with get_conn() as conn:
            _insert_base(conn)
        assert recently_published_to_other_account("clip_r", "acct_a") is False

    def test_other_account_within_window_blocked(self):
        from clipsys.publish.caps import recently_published_to_other_account
        now = datetime.now(UTC)
        pub_at = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_conn() as conn:
            _insert_base(conn)
            _insert_variant(conn, "var_b", account_id="acct_b",
                            status="published", published_at=pub_at)
        assert recently_published_to_other_account("clip_r", "acct_a", window_minutes=60, now=now) is True

    def test_other_account_outside_window_allowed(self):
        from clipsys.publish.caps import recently_published_to_other_account
        now = datetime.now(UTC)
        pub_at = (now - timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_conn() as conn:
            _insert_base(conn)
            _insert_variant(conn, "var_b", account_id="acct_b",
                            status="published", published_at=pub_at)
        assert recently_published_to_other_account("clip_r", "acct_a", window_minutes=60, now=now) is False

    def test_same_account_does_not_trigger(self):
        from clipsys.publish.caps import recently_published_to_other_account
        now = datetime.now(UTC)
        pub_at = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_conn() as conn:
            _insert_base(conn)
            _insert_variant(conn, "var_a", account_id="acct_a",
                            status="published", published_at=pub_at)
        assert recently_published_to_other_account("clip_r", "acct_a", window_minutes=60, now=now) is False

    def test_held_status_not_counted(self):
        """'held' variants must not block other accounts (only 'published' counts)."""
        from clipsys.publish.caps import recently_published_to_other_account
        now = datetime.now(UTC)
        pub_at = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_conn() as conn:
            _insert_base(conn)
            _insert_variant(conn, "var_b", account_id="acct_b",
                            status="held", published_at=pub_at)
        assert recently_published_to_other_account("clip_r", "acct_a", window_minutes=60, now=now) is False


# ─── CLI api_public routing tests ────────────────────────────────────────────

class TestApiPublicFalseRouting:
    def _db_setup(self):
        with get_conn() as conn:
            _insert_base(conn)
            _insert_variant(conn, "var_1")

    def test_variant_becomes_held_on_telegram_success(self, monkeypatch):
        self._db_setup()
        acct = _make_account(api_public=False, stage=0)
        # stage=0 normally requires 'approved'; the variant is 'approved' so can_publish returns ok
        _patch_cli(monkeypatch, acct)

        result = _invoke_publish()
        assert result.exit_code == 0

        with get_conn() as conn:
            row = conn.execute(
                "SELECT status, telegram_sent_at FROM variants WHERE id='var_1'"
            ).fetchone()
        assert row["status"] == "held"
        assert row["telegram_sent_at"] is not None

    def test_send_clip_called_exactly_once(self, monkeypatch):
        self._db_setup()
        calls = []

        def _fake_send_clip(**kw):
            calls.append(kw)
            return "9999"

        acct = _make_account(api_public=False, stage=0)
        _patch_cli(monkeypatch, acct, tg_send_clip=_fake_send_clip)
        _invoke_publish()

        assert len(calls) == 1
        assert calls[0]["variant_id"] == "var_1"

    def test_platform_publisher_not_called_when_api_public_false(self, monkeypatch):
        self._db_setup()
        acct = _make_account(api_public=False, stage=0)
        _patch_cli(monkeypatch, acct)

        pub_calls = []

        class FakeYT:
            def __init__(self, *a, **kw):
                pass

            def publish(self, **kw):
                pub_calls.append(kw)

        import clipsys.publish.youtube as yt_mod
        monkeypatch.setattr(yt_mod, "YouTubePublisher", FakeYT)

        _invoke_publish()
        assert pub_calls == []

    def test_telegram_dedup_skips_when_already_sent(self, monkeypatch):
        """Variant with telegram_sent_at already set must not call send_clip again."""
        with get_conn() as conn:
            _insert_base(conn)
            _insert_variant(conn, "var_1", telegram_sent_at="2025-01-01T12:00:00Z")

        calls = []
        acct = _make_account(api_public=False, stage=0)
        _patch_cli(monkeypatch, acct, tg_send_clip=lambda **kw: (calls.append(kw), "9999")[1])

        _invoke_publish()
        assert calls == []

    def test_api_public_true_calls_publisher(self, monkeypatch):
        """Control: api_public=True must route to the platform publisher."""
        self._db_setup()
        acct = _make_account(api_public=True, stage=0)
        _patch_cli(monkeypatch, acct)

        pub_calls = []

        class FakePublishResult:
            platform_post_id = "fake_post_id"
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

        _invoke_publish()
        assert len(pub_calls) == 1
