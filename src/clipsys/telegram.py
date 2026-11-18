"""Telegram notification helper.

Sends finished clips via sendVideo (falls back to sendMessage when the file
exceeds the 50 MB Bot API limit), posts failure/quota/kill-switch alerts,
and polls getUpdates for inline-button presses (Posted / Skip / Snooze 1h).

Configuration (via .env):
    TELEGRAM_BOT_TOKEN — from @BotFather
    TELEGRAM_CHAT_ID   — target chat or channel ID

Both must be non-empty for any network calls to happen.  When either is
absent every function is a silent no-op so the pipeline runs without
Telegram configured.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from clipsys._env import settings
from clipsys._log import get_logger

log = get_logger(__name__)

MAX_VIDEO_BYTES = 50 * 1024 * 1024  # Telegram Bot API hard limit


def _is_configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _url(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


def _inline_buttons(variant_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Posted",    "callback_data": f"posted:{variant_id}"},
            {"text": "⏭ Skip",      "callback_data": f"skip:{variant_id}"},
            {"text": "⏰ Snooze 1h", "callback_data": f"snooze:{variant_id}"},
        ]]
    }


def _build_caption(
    title: str,
    description: str,
    permission_ref: str,
    platform: str,
    account_id: str,
    file_path: str,
) -> str:
    desc_snippet = description[:200] + ("…" if len(description) > 200 else "")
    return (
        f"*{title}*\n"
        f"{desc_snippet}\n\n"
        f"Platform: `{platform}` | Account: `{account_id}`\n"
        f"Rights ref: {permission_ref}\n"
        f"Path: `{file_path}`"
    )


def send_clip(
    variant_id: str,
    file_path: str,
    title: str,
    description: str,
    permission_ref: str,
    account_id: str,
    platform: str,
) -> str | None:
    """Send a clip notification to Telegram.

    Returns the Telegram message_id string on success, None on failure or
    when Telegram is not configured.

    If the file is larger than MAX_VIDEO_BYTES (50 MB) or does not exist,
    sends a text message with the file path instead of uploading the video.
    """
    if not _is_configured():
        log.debug("telegram_not_configured_skip_send")
        return None

    chat_id = settings.telegram_chat_id
    caption = _build_caption(title, description, permission_ref, platform, account_id, file_path)
    buttons = _inline_buttons(variant_id)

    path = Path(file_path)
    file_size = path.stat().st_size if path.exists() else 0

    try:
        if not path.exists() or file_size > MAX_VIDEO_BYTES:
            size_mb = round(file_size / 1_048_576, 1)
            log.warning(
                "telegram_video_too_large_sending_path",
                extra={"variant_id": variant_id[:8], "size_mb": size_mb},
            )
            resp = httpx.post(
                _url("sendMessage"),
                json={
                    "chat_id": chat_id,
                    "text": (
                        caption
                        + f"\n\n⚠️ File too large ({size_mb} MB) — upload manually."
                    ),
                    "parse_mode": "Markdown",
                    "reply_markup": buttons,
                },
                timeout=30,
            )
        else:
            with path.open("rb") as fh:
                resp = httpx.post(
                    _url("sendVideo"),
                    data={
                        "chat_id": chat_id,
                        "caption": caption,
                        "parse_mode": "Markdown",
                        "reply_markup": json.dumps(buttons),
                    },
                    files={"video": (path.name, fh, "video/mp4")},
                    timeout=300,
                )

        resp.raise_for_status()
        message_id = str(resp.json()["result"]["message_id"])
        log.info(
            "telegram_clip_sent",
            extra={"variant_id": variant_id[:8], "message_id": message_id},
        )
        return message_id

    except Exception as exc:
        log.error(
            "telegram_send_failed",
            extra={"variant_id": variant_id[:8], "err": str(exc)},
        )
        return None


def send_alert(text: str) -> None:
    """Send a plain-text alert (upload failure, quota exhausted, kill switch)."""
    if not _is_configured():
        return

    try:
        resp = httpx.post(
            _url("sendMessage"),
            json={"chat_id": settings.telegram_chat_id, "text": text},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.error("telegram_alert_failed", extra={"err": str(exc)})


def poll_updates(offset: int = 0, long_poll_timeout: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Poll getUpdates for callback_query events (inline-button presses).

    Returns (updates, next_offset).  Pass the returned next_offset on the
    next call to avoid re-processing the same updates.
    """
    if not _is_configured():
        return [], offset

    try:
        resp = httpx.get(
            _url("getUpdates"),
            params={
                "offset": offset,
                "timeout": long_poll_timeout,
                "allowed_updates": json.dumps(["callback_query"]),
            },
            timeout=long_poll_timeout + 10,
        )
        resp.raise_for_status()
        updates: list[dict[str, Any]] = resp.json().get("result", [])
        next_offset = updates[-1]["update_id"] + 1 if updates else offset
        return updates, next_offset
    except Exception as exc:
        log.error("telegram_poll_failed", extra={"err": str(exc)})
        return [], offset
