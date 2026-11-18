"""Cut master clips from scored moments."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from clipsys._ff import ff_run, ffprobe_run
from clipsys._log import get_logger
from clipsys.config import ThresholdsConfig, load_thresholds
from clipsys.db.db import get_conn

log = get_logger(__name__)

CLIPS_DIR = Path("data/clips")


def _find_nearest_scene_cut(
    file_path: str,
    t_target: float,
    search_window: float = 2.0,
    threshold: int = 10,
) -> float:
    """Return the nearest scene-cut timestamp within ±search_window, or t_target."""
    t_start_probe = max(0.0, t_target - search_window)
    duration_probe = search_window * 2

    result = ff_run(
        ["-ss", str(t_start_probe), "-i", file_path, "-t", str(duration_probe),
         "-vf", f"scdet=threshold={threshold}", "-f", "null", "-"],
        capture_output=True, text=True, timeout=30,
    )
    output = result.stderr

    cuts: list[float] = []
    for line in output.splitlines():
        if "Parsed_scdet" in line and "pts_time:" in line:
            import re
            m = re.search(r"pts_time:([\d.]+)", line)
            if m:
                cuts.append(t_start_probe + float(m.group(1)))

    if not cuts:
        return t_target
    return min(cuts, key=lambda c: abs(c - t_target))


def _probe_duration(file_path: str) -> float:
    result = ffprobe_run(
        ["-v", "quiet", "-print_format", "json", "-show_format", file_path],
        capture_output=True, text=True, timeout=30,
    )
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def cut_clips(cfg: ThresholdsConfig | None = None) -> dict[str, int]:
    cfg = cfg or load_thresholds()
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        moments = conn.execute(
            """SELECT m.id, m.source_id, m.t_start, m.t_end, m.score,
                      s.file_path, s.duration_s
               FROM moments m
               JOIN sources s ON s.id = m.source_id
               WHERE m.score >= ?
               ORDER BY m.score DESC""",
            (cfg.score.min_score,),
        ).fetchall()

    counts = {"cut": 0, "skipped": 0, "errors": 0}

    for row in moments:
        moment_id = row["id"]
        source_id = row["source_id"]
        file_path = row["file_path"]
        source_duration = row["duration_s"]

        with get_conn() as conn:
            exists = conn.execute(
                "SELECT id FROM clips WHERE moment_id=?", (moment_id,)
            ).fetchone()
        if exists:
            counts["skipped"] += 1
            continue

        pre_roll = cfg.clips.pre_roll_seconds
        t_start = max(0.0, row["t_start"] - pre_roll)
        raw_end = row["t_end"]

        # Snap end to nearest scene cut
        t_end_snapped = _find_nearest_scene_cut(file_path, raw_end, search_window=2.0)
        t_end = min(t_end_snapped, source_duration)

        duration = t_end - t_start

        # Enforce minimum clip length using short band
        min_s = cfg.clips.short.min_s
        if duration < min_s:
            t_end = min(t_start + min_s, source_duration)
            duration = t_end - t_start

        # Cap at the longest band max
        max_s = cfg.clips.long.max_s
        if duration > max_s:
            t_end = t_start + max_s
            duration = max_s

        if duration < min_s:
            log.warning(
                "clip_too_short",
                extra={"moment_id": moment_id, "duration": duration},
            )
            counts["skipped"] += 1
            continue

        clip_id = str(uuid.uuid4())
        out_path = CLIPS_DIR / f"{clip_id}.mp4"

        try:
            result = ff_run(
                ["-y", "-ss", str(t_start), "-i", file_path, "-t", str(duration),
                 "-c:v", "copy", "-c:a", "copy", str(out_path)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr[-500:])
        except Exception as exc:
            log.error("cut_failed", extra={"moment_id": moment_id, "err": str(exc)})
            counts["errors"] += 1
            continue

        actual_duration = _probe_duration(str(out_path))

        with get_conn() as conn:
            conn.execute(
                """INSERT INTO clips (id, source_id, moment_id, file_path,
                   t_start, t_end, duration_s, score)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    clip_id, source_id, moment_id, str(out_path),
                    t_start, t_end, actual_duration, row["score"],
                ),
            )

        log.info(
            "clip_cut",
            extra={
                "clip_id": clip_id[:8],
                "source_id": source_id[:12],
                "t_start": round(t_start, 1),
                "duration": round(actual_duration, 1),
                "score": round(row["score"], 3),
            },
        )
        counts["cut"] += 1

    return counts
