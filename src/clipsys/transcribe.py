"""Transcribe source audio with faster-whisper. Idempotent."""

from __future__ import annotations

import json
from pathlib import Path

from clipsys._log import get_logger
from clipsys.db.db import get_conn

log = get_logger(__name__)

TRANSCRIPTS_DIR = Path("data/transcripts")

DEFAULT_MODEL = "medium"
DEFAULT_COMPUTE = "int8_float16"


def _get_model(model_size: str, compute_type: str):
    from faster_whisper import WhisperModel  # type: ignore[import]

    return WhisperModel(model_size, device="auto", compute_type=compute_type)


def transcribe_source(
    source_id: str,
    file_path: str,
    model_size: str = DEFAULT_MODEL,
    compute_type: str = DEFAULT_COMPUTE,
) -> bool:
    """Transcribe one source. Returns True if new transcript was produced."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM transcripts WHERE source_id = ?", (source_id,)
        ).fetchone()

    if existing:
        log.info("transcript_exists", extra={"source_id": source_id[:12]})
        return False

    path = Path(file_path)
    if not path.exists():
        log.error("source_file_missing", extra={"source_id": source_id[:12], "path": file_path})
        return False

    log.info("transcribing", extra={"source_id": source_id[:12], "model": model_size})

    model = _get_model(model_size, compute_type)
    segments_iter, info = model.transcribe(str(path), beam_size=5, word_timestamps=False)

    segments = []
    for seg in segments_iter:
        segments.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text})

    segments_json = json.dumps(segments, ensure_ascii=False)

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO transcripts (source_id, language, segments_json) VALUES (?,?,?)",
            (source_id, info.language, segments_json),
        )
        conn.execute("UPDATE sources SET status='transcribed' WHERE id=?", (source_id,))

    log.info(
        "transcript_saved",
        extra={
            "source_id": source_id[:12],
            "language": info.language,
            "segments": len(segments),
        },
    )
    return True


def run_transcribe(
    model_size: str = DEFAULT_MODEL,
    compute_type: str = DEFAULT_COMPUTE,
) -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, file_path FROM sources WHERE status='ready' OR status='transcribed'"
        ).fetchall()

    # Only process sources that don't yet have a transcript.
    with get_conn() as conn:
        transcribed_ids = {
            r["source_id"]
            for r in conn.execute("SELECT DISTINCT source_id FROM transcripts").fetchall()
        }

    pending = [r for r in rows if r["id"] not in transcribed_ids]
    counts = {"transcribed": 0, "skipped": len(rows) - len(pending), "errors": 0}

    for row in pending:
        try:
            transcribe_source(row["id"], row["file_path"], model_size, compute_type)
            counts["transcribed"] += 1
        except Exception as exc:
            log.error("transcribe_failed", extra={"source_id": row["id"][:12], "err": str(exc)})
            counts["errors"] += 1

    return counts
