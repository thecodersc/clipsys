"""Generate captions via local LLM (Ollama). M2."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, field_validator

from clipsys._env import settings
from clipsys._log import get_logger
from clipsys.db.db import get_conn

log = get_logger(__name__)

MAX_RETRIES = 3


class CaptionSchema(BaseModel):
    title: str
    hook: str
    description: str
    hashtags: list[str]

    @field_validator("title")
    @classmethod
    def _title_len(cls, v: str) -> str:
        if len(v) > 100:
            raise ValueError("title too long (>100 chars)")
        return v

    @field_validator("hashtags")
    @classmethod
    def _hashtag_format(cls, v: list[str]) -> list[str]:
        return [t if t.startswith("#") else f"#{t}" for t in v]


def _load_persona(persona_path: str) -> dict[str, Any]:
    """Parse a persona .md file, extract banned_phrases."""
    path = Path("config") / persona_path
    if not path.exists():
        path = Path(persona_path)
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    banned: list[str] = []
    in_banned = False
    for line in text.splitlines():
        if "banned phrases" in line.lower():
            in_banned = True
            continue
        if in_banned:
            m = re.match(r"^\s*[-*]\s+(.+)", line)
            if m:
                phrase = m.group(1).strip().strip('"')
                banned.append(phrase.lower())
            elif line.startswith("#"):
                in_banned = False

    return {"raw": text, "banned_phrases": banned}


def _call_ollama(prompt: str, model: str) -> str:
    """Call Ollama /api/chat. Sets think:false for qwen3 models to suppress CoT output."""
    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    is_thinking_model = "qwen3" in model.lower()
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 600},
    }
    if is_thinking_model:
        payload["think"] = False  # disable CoT — Ollama qwen3 feature

    resp = httpx.post(url, json=payload, timeout=180)
    resp.raise_for_status()
    content = resp.json()["message"]["content"]
    return _strip_think_tags(content)


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks that qwen3 may emit when think:false is ignored."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _extract_json(text: str) -> dict:
    # 1. Try JSON inside a code fence: ```json { ... } ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    # 2. First bare {...} block
    bare = re.search(r"\{.*\}", text, re.DOTALL)
    if bare:
        return json.loads(bare.group(0))
    raise ValueError("No JSON object found in LLM response")


def generate_caption(
    variant_id: str,
    transcript_excerpt: str,
    persona_path: str,
    model: str | None = None,
) -> CaptionSchema:
    model = model or settings.ollama_model
    persona = _load_persona(persona_path)
    banned = persona["banned_phrases"]

    system_instructions = (
        "You are a social media caption writer.\n"
        f"Persona instructions:\n{persona['raw']}\n\n"
        "Return ONLY a single JSON object with these exact keys — no other text, "
        "no markdown fences, no explanation:\n"
        '  "title"       — string, ≤60 chars\n'
        '  "hook"        — string, ≤120 chars\n'
        '  "description" — string\n'
        '  "hashtags"    — array of 3-5 strings, each starting with #\n'
    )
    user_prompt = (
        f"Transcript excerpt:\n{transcript_excerpt}\n\n"
        "Write the caption JSON now. Output only the JSON object."
    )
    full_prompt = system_instructions + "\n" + user_prompt

    last_error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        attempt_prompt = full_prompt
        if last_error:
            attempt_prompt += (
                f"\n\nYour previous attempt failed: {last_error}\n"
                "Fix the error and return only the corrected JSON object."
            )

        try:
            raw = _call_ollama(attempt_prompt, model)
            data = _extract_json(raw)
            caption = CaptionSchema(**data)

            combined = f"{caption.title} {caption.hook} {caption.description}".lower()
            hits = [bp for bp in banned if bp in combined]
            if hits:
                last_error = f"banned phrases present: {hits}"
                log.warning("banned_phrase_hit", extra={"variant_id": variant_id[:8], "hits": hits})
                continue

            return caption

        except Exception as exc:
            last_error = str(exc)
            log.warning(
                "caption_retry",
                extra={"variant_id": variant_id[:8], "attempt": attempt, "err": last_error},
            )

    raise RuntimeError(f"Caption generation failed after {MAX_RETRIES} attempts: {last_error}")


def run_caption(model: str | None = None) -> dict[str, int]:
    with get_conn() as conn:
        variants = conn.execute(
            """SELECT v.id, v.account_id, c.t_start, c.t_end, t.segments_json
               FROM variants v
               JOIN clips c ON c.id = v.clip_id
               JOIN sources s ON s.id = c.source_id
               JOIN transcripts t ON t.source_id = s.id
               WHERE v.status = 'rendered'"""
        ).fetchall()

    from clipsys.config import load_accounts
    accts = load_accounts()
    counts = {"captioned": 0, "errors": 0}

    for row in variants:
        try:
            account = accts.by_id(row["account_id"])
        except KeyError:
            log.error("unknown_account", extra={"account_id": row["account_id"]})
            counts["errors"] += 1
            continue

        segs = json.loads(row["segments_json"])
        clip_segs = [s for s in segs if s["end"] >= row["t_start"] and s["start"] <= row["t_end"]]
        excerpt = " ".join(s["text"] for s in clip_segs)[:1500]

        try:
            caption = generate_caption(row["id"], excerpt, account.render_profile.persona, model)
            with get_conn() as conn:
                conn.execute(
                    """UPDATE variants
                       SET caption_json=?, status='captioned',
                           updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                       WHERE id=?""",
                    (caption.model_dump_json(), row["id"]),
                )
            counts["captioned"] += 1
            log.info("captioned", extra={"variant_id": row["id"][:8], "title": caption.title[:40]})
        except Exception as exc:
            log.error("caption_failed", extra={"variant_id": row["id"][:8], "err": str(exc)})
            with get_conn() as conn:
                conn.execute(
                    "UPDATE variants SET status='error', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (row["id"],),
                )
            counts["errors"] += 1

    return counts
