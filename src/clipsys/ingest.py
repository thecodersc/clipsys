"""Ingest source videos from inbox/ using manifest.csv."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from pathlib import Path

from clipsys._ff import ffprobe_run
from clipsys._log import get_logger
from clipsys.config import load_programs
from clipsys.db.db import get_conn

log = get_logger(__name__)

INBOX = Path("inbox")
SOURCES_DIR = Path("data/sources")
MANIFEST = INBOX / "manifest.csv"


# ─── ffprobe helpers ──────────────────────────────────────────────────────────

def _ffprobe(path: Path) -> dict:
    result = ffprobe_run(
        ["-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _probe_validate(path: Path) -> tuple[float, bool, bool]:
    """Return (duration_s, has_video, has_audio)."""
    info = _ffprobe(path)
    streams = info.get("streams", [])
    has_video = any(s["codec_type"] == "video" for s in streams)
    has_audio = any(s["codec_type"] == "audio" for s in streams)
    duration = float(info.get("format", {}).get("duration", 0))
    if duration <= 0:
        raise ValueError(f"Could not determine duration for {path}")
    return duration, has_video, has_audio


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_yt_id(url: str | None) -> str | None:
    if not url:
        return None
    patterns = [
        r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})",
        r"shorts/([A-Za-z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


# ─── Public API ───────────────────────────────────────────────────────────────

def run_ingest() -> dict[str, int]:
    """Read manifest.csv, validate files, copy to data/sources/, register in DB."""
    if not MANIFEST.exists():
        log.warning("manifest.csv not found", extra={"path": str(MANIFEST)})
        return {"skipped": 0, "added": 0, "errors": 0}

    SOURCES_DIR.mkdir(parents=True, exist_ok=True)

    permitted_programs = {p.id: p for p in load_programs().programs}

    counts = {"added": 0, "skipped": 0, "errors": 0}

    with MANIFEST.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    for row in rows:
        src_file = INBOX / row["file"].strip()
        program_id = row["program_id"].strip()

        if program_id not in permitted_programs:
            log.warning(
                "ingest_blocked_no_permission",
                extra={"file": row["file"].strip(), "program_id": program_id},
            )
            counts["skipped"] += 1
            continue
        creator = row["creator"].strip()
        source_url = row.get("source_url_or_empty", "").strip() or None
        received_at = row.get("received_at", "").strip()

        if not src_file.exists():
            log.error("file_missing", extra={"file": str(src_file)})
            counts["errors"] += 1
            continue

        try:
            sha = _sha256(src_file)
        except Exception as exc:
            log.error("sha_failed", extra={"file": str(src_file), "err": str(exc)})
            counts["errors"] += 1
            continue

        with get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM sources WHERE id = ?", (sha,)
            ).fetchone()

        if existing:
            log.info("duplicate_skipped", extra={"sha": sha[:12], "file": src_file.name})
            counts["skipped"] += 1
            continue

        try:
            duration, has_video, has_audio = _probe_validate(src_file)
        except Exception as exc:
            log.error("probe_failed", extra={"file": str(src_file), "err": str(exc)})
            counts["errors"] += 1
            continue

        dest = SOURCES_DIR / f"{sha[:16]}_{src_file.name}"
        shutil.copy2(src_file, dest)

        yt_id = _extract_yt_id(source_url)

        with get_conn() as conn:
            conn.execute(
                """INSERT INTO sources
                   (id, file_path, program_id, creator, source_url,
                    youtube_video_id, duration_s, has_video, has_audio, received_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    sha, str(dest), program_id, creator, source_url,
                    yt_id, duration, int(has_video), int(has_audio), received_at,
                ),
            )

        log.info(
            "source_registered",
            extra={
                "sha": sha[:16],
                "file": src_file.name,
                "duration_s": round(duration, 1),
                "program_id": program_id,
            },
        )
        counts["added"] += 1

    return counts
