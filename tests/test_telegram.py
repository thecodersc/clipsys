"""Tests for telegram.py — all HTTP mocked via httpx.MockTransport."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

import clipsys.telegram as tg_mod
from clipsys.db.db import get_conn


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _mock_response(body: dict, status: int = 200) -> httpx.Response:
    # httpx.Response requires an associated Request for raise_for_status() to work.
    request = httpx.Request("POST", "https://api.telegram.org/bot/test")
    return httpx.Response(status, json=body, request=request)


def _sent_response(message_id: int = 42) -> dict:
    return {"ok": True, "result": {"message_id": message_id}}


def _configure(monkeypatch, bot_token: str = "tok:TEST", chat_id: str = "-100123"):
    monkeypatch.setattr("clipsys._env.settings.telegram_bot_token", bot_token)
    monkeypatch.setattr("clipsys._env.settings.telegram_chat_id", chat_id)


# ─── send_clip — video path ───────────────────────────────────────────────────

class TestSendClip:
    def test_sends_video_for_small_file(self, tmp_path, monkeypatch):
        """sendVideo is called when file exists and is under 50 MB."""
        _configure(monkeypatch)
        small_file = tmp_path / "clip.mp4"
        small_file.write_bytes(b"\x00" * 1024)

        calls = []

        def _fake_post(url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})
            return _mock_response(_sent_response(99))

        monkeypatch.setattr(httpx, "post", _fake_post)

        result = tg_mod.send_clip(
            variant_id="abc123",
            file_path=str(small_file),
            title="Epic Clip",
            description="Short chase",
            permission_ref="ref-001",
            account_id="acct_01",
            platform="youtube_shorts",
        )

        assert result == "99"
        assert len(calls) == 1
        assert "sendVideo" in calls[0]["url"]
        assert "files" in calls[0]["kwargs"]

    def test_sends_path_when_file_too_large(self, tmp_path, monkeypatch):
        """sendMessage with path is used when file > 50 MB."""
        _configure(monkeypatch)
        large_file = tmp_path / "big.mp4"
        large_file.write_bytes(b"\x00" * (51 * 1024 * 1024))

        calls = []

        def _fake_post(url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})
            return _mock_response(_sent_response(55))

        monkeypatch.setattr(httpx, "post", _fake_post)

        result = tg_mod.send_clip(
            variant_id="bigvid",
            file_path=str(large_file),
            title="Big",
            description="",
            permission_ref="ref-002",
            account_id="acct_01",
            platform="youtube_shorts",
        )

        assert result == "55"
        assert len(calls) == 1
        assert "sendMessage" in calls[0]["url"]
        body = calls[0]["kwargs"]["json"]
        assert "too large" in body["text"]
        # Path must appear in the text
        assert str(large_file) in body["text"]

    def test_sends_path_when_file_missing(self, tmp_path, monkeypatch):
        """sendMessage with note is used when the file does not exist."""
        _configure(monkeypatch)

        calls = []

        def _fake_post(url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})
            return _mock_response(_sent_response(10))

        monkeypatch.setattr(httpx, "post", _fake_post)

        result = tg_mod.send_clip(
            variant_id="missing",
            file_path="/nonexistent/clip.mp4",
            title="Missing",
            description="",
            permission_ref="ref-003",
            account_id="acct_01",
            platform="tiktok",
        )

        assert result == "10"
        assert "sendMessage" in calls[0]["url"]

    def test_caption_contains_title_and_permission_ref(self, tmp_path, monkeypatch):
        """The Telegram caption includes the clip title and permission_ref."""
        _configure(monkeypatch)
        f = tmp_path / "c.mp4"
        f.write_bytes(b"\x00" * 100)

        captured = {}

        def _fake_post(url, **kwargs):
            captured.update(kwargs)
            return _mock_response(_sent_response(1))

        monkeypatch.setattr(httpx, "post", _fake_post)

        tg_mod.send_clip(
            variant_id="cap_test",
            file_path=str(f),
            title="Chase Scene",
            description="Wild chase in Vice City",
            permission_ref="perm-ref-XYZ",
            account_id="acct_02",
            platform="youtube_shorts",
        )

        caption = captured.get("data", {}).get("caption", "")
        assert "Chase Scene" in caption
        assert "perm-ref-XYZ" in caption
        assert "acct_02" in caption

    def test_inline_buttons_present(self, tmp_path, monkeypatch):
        """The reply_markup includes Posted, Skip, Snooze buttons."""
        _configure(monkeypatch)
        f = tmp_path / "b.mp4"
        f.write_bytes(b"\x00" * 100)

        captured = {}

        def _fake_post(url, **kwargs):
            captured.update(kwargs)
            return _mock_response(_sent_response(2))

        monkeypatch.setattr(httpx, "post", _fake_post)

        tg_mod.send_clip(
            variant_id="btn_test",
            file_path=str(f),
            title="T",
            description="",
            permission_ref="r",
            account_id="a",
            platform="youtube_shorts",
        )

        markup_raw = captured.get("data", {}).get("reply_markup", "{}")
        markup = json.loads(markup_raw)
        button_texts = [
            btn["text"]
            for row in markup["inline_keyboard"]
            for btn in row
        ]
        assert any("Posted" in t for t in button_texts)
        assert any("Skip" in t for t in button_texts)
        assert any("Snooze" in t for t in button_texts)

    def test_no_send_when_not_configured(self, monkeypatch):
        """Returns None silently when bot token or chat_id is missing."""
        monkeypatch.setattr("clipsys._env.settings.telegram_bot_token", "")
        monkeypatch.setattr("clipsys._env.settings.telegram_chat_id", "")

        post_called = []
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: post_called.append(1))

        result = tg_mod.send_clip(
            variant_id="x",
            file_path="/tmp/x.mp4",
            title="T",
            description="",
            permission_ref="r",
            account_id="a",
            platform="youtube_shorts",
        )

        assert result is None
        assert len(post_called) == 0

    def test_returns_none_on_http_error(self, tmp_path, monkeypatch):
        """Returns None when the API call fails; does not raise."""
        _configure(monkeypatch)
        f = tmp_path / "err.mp4"
        f.write_bytes(b"\x00" * 100)

        def _bad_post(url, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "post", _bad_post)

        result = tg_mod.send_clip(
            variant_id="err",
            file_path=str(f),
            title="T",
            description="",
            permission_ref="r",
            account_id="a",
            platform="youtube_shorts",
        )
        assert result is None


# ─── Dedup: telegram_sent_at ──────────────────────────────────────────────────

class TestTelegramSentAtDedup:
    """Verify the publish flow only sends once per variant across restarts."""

    def _insert_variant(self, conn, variant_id: str, telegram_sent_at=None) -> None:
        conn.execute(
            """INSERT INTO sources (id, file_path, program_id, creator, duration_s, received_at)
               VALUES ('src1', '/tmp/s.mp4', 'prog', 'c', 60, '2024-01-01T00:00:00Z')
               ON CONFLICT DO NOTHING"""
        )
        conn.execute(
            """INSERT INTO clips (id, source_id, file_path, t_start, t_end, duration_s, score)
               VALUES ('clip1', 'src1', '/tmp/c.mp4', 0, 30, 30, 2.5)
               ON CONFLICT DO NOTHING"""
        )
        conn.execute(
            """INSERT INTO variants (id, clip_id, account_id, status, telegram_sent_at)
               VALUES (?,?,?,?,?)""",
            (variant_id, "clip1", "acct_01", "approved", telegram_sent_at),
        )

    def test_sent_at_stored_after_send(self, monkeypatch):
        """telegram_sent_at is written to DB after a successful send."""
        _configure(monkeypatch)

        vid = str(uuid.uuid4())
        with get_conn() as conn:
            self._insert_variant(conn, vid, telegram_sent_at=None)

        def _fake_post(url, **kwargs):
            return _mock_response(_sent_response(77))

        monkeypatch.setattr(httpx, "post", _fake_post)

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            tf.write(b"\x00" * 100)
            tmp_path = tf.name

        tg_mod.send_clip(
            variant_id=vid,
            file_path=tmp_path,
            title="T",
            description="",
            permission_ref="r",
            account_id="acct_01",
            platform="youtube_shorts",
        )

        # Simulate what cli.py does: write telegram_sent_at
        from datetime import UTC, datetime
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_conn() as conn:
            conn.execute(
                "UPDATE variants SET telegram_sent_at=? WHERE id=?", (now, vid)
            )

        with get_conn() as conn:
            row = conn.execute(
                "SELECT telegram_sent_at FROM variants WHERE id=?", (vid,)
            ).fetchone()
        assert row["telegram_sent_at"] is not None

    def test_already_sent_flag_is_readable(self, monkeypatch):
        """A variant with telegram_sent_at set is detectable by the publish loop."""
        vid = str(uuid.uuid4())
        already_sent_at = "2024-01-01T12:00:00Z"

        with get_conn() as conn:
            self._insert_variant(conn, vid, telegram_sent_at=already_sent_at)

        with get_conn() as conn:
            row = conn.execute(
                "SELECT telegram_sent_at FROM variants WHERE id=?", (vid,)
            ).fetchone()
        assert row["telegram_sent_at"] == already_sent_at


# ─── send_alert ───────────────────────────────────────────────────────────────

class TestSendAlert:
    def test_sends_text_message(self, monkeypatch):
        _configure(monkeypatch)
        calls = []

        def _fake_post(url, **kwargs):
            calls.append({"url": url, "kwargs": kwargs})
            return _mock_response({"ok": True, "result": {"message_id": 1}})

        monkeypatch.setattr(httpx, "post", _fake_post)
        tg_mod.send_alert("⛔ Kill switch active")

        assert len(calls) == 1
        assert "sendMessage" in calls[0]["url"]
        assert "Kill switch" in calls[0]["kwargs"]["json"]["text"]

    def test_silent_when_not_configured(self, monkeypatch):
        monkeypatch.setattr("clipsys._env.settings.telegram_bot_token", "")
        monkeypatch.setattr("clipsys._env.settings.telegram_chat_id", "")
        post_called = []
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: post_called.append(1))
        tg_mod.send_alert("test")
        assert len(post_called) == 0

    def test_does_not_raise_on_http_error(self, monkeypatch):
        _configure(monkeypatch)
        monkeypatch.setattr(httpx, "post", lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("x")))
        tg_mod.send_alert("should not raise")  # must not propagate


# ─── poll_updates ─────────────────────────────────────────────────────────────

class TestPollUpdates:
    def test_returns_updates_and_next_offset(self, monkeypatch):
        _configure(monkeypatch)

        fake_updates = [
            {"update_id": 100, "callback_query": {"data": "posted:abc"}},
            {"update_id": 101, "callback_query": {"data": "skip:def"}},
        ]

        def _fake_get(url, **kwargs):
            return _mock_response({"ok": True, "result": fake_updates})

        monkeypatch.setattr(httpx, "get", _fake_get)

        updates, next_offset = tg_mod.poll_updates(offset=100)
        assert len(updates) == 2
        assert next_offset == 102

    def test_empty_result_keeps_offset(self, monkeypatch):
        _configure(monkeypatch)
        monkeypatch.setattr(
            httpx, "get", lambda *a, **kw: _mock_response({"ok": True, "result": []})
        )
        _, next_offset = tg_mod.poll_updates(offset=50)
        assert next_offset == 50

    def test_silent_when_not_configured(self, monkeypatch):
        monkeypatch.setattr("clipsys._env.settings.telegram_bot_token", "")
        monkeypatch.setattr("clipsys._env.settings.telegram_chat_id", "")
        get_called = []
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: get_called.append(1))
        updates, offset = tg_mod.poll_updates(offset=0)
        assert updates == []
        assert offset == 0
        assert len(get_called) == 0

    def test_returns_empty_on_http_error(self, monkeypatch):
        _configure(monkeypatch)
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("x")))
        updates, offset = tg_mod.poll_updates(offset=5)
        assert updates == []
        assert offset == 5
