"""Score source video windows using four signals and NMS."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from clipsys._ff import ff_run
from clipsys._log import get_logger
from clipsys.config import ThresholdsConfig, load_thresholds
from clipsys.db.db import get_conn

log = get_logger(__name__)


class _Window(NamedTuple):
    t_center: float
    comment_density: float
    loudness_peak: float
    scene_density: float
    keyword_hits: float


# ─── Signal extractors ────────────────────────────────────────────────────────

def _comment_density(youtube_video_id: str | None, duration_s: float, bin_s: float = 10.0) -> dict[float, float]:
    """Return {t_center: weighted_count} from YouTube timestamp comments."""
    if not youtube_video_id:
        return {}

    from clipsys._env import settings  # lazy import to allow env override in tests

    api_key = settings.youtube_data_api_key
    if not api_key:
        log.warning("no_yt_api_key_for_comment_density")
        return {}

    try:
        from googleapiclient.discovery import build  # type: ignore[import]

        yt = build("youtube", "v3", developerKey=api_key)
        buckets: dict[float, float] = defaultdict(float)
        page_token = None

        for _ in range(10):  # max 10 pages ≈ 2000 comments
            req = yt.commentThreads().list(
                part="snippet",
                videoId=youtube_video_id,
                maxResults=200,
                pageToken=page_token,
                textFormat="plainText",
            )
            resp = req.execute()

            for item in resp.get("items", []):
                snip = item["snippet"]["topLevelComment"]["snippet"]
                text = snip.get("textDisplay", "")
                likes = int(snip.get("likeCount", 0))

                # Match mm:ss or h:mm:ss
                for m in re.finditer(r"\b(?:(\d+):)?(\d{1,2}):(\d{2})\b", text):
                    h = int(m.group(1) or 0)
                    minutes = int(m.group(2))
                    secs = int(m.group(3))
                    t = h * 3600 + minutes * 60 + secs
                    if 0 <= t <= duration_s:
                        center = (t // bin_s) * bin_s + bin_s / 2
                        buckets[center] += math.log1p(likes) + 1.0

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return dict(buckets)

    except Exception as exc:
        log.warning("comment_density_failed", extra={"err": str(exc)})
        return {}


def _loudness_signal(file_path: str, duration_s: float, window_s: float = 10.0) -> dict[float, float]:
    """Return {t_center: short_term_loudness_max_per_window}."""
    result = ff_run(
        # framelog must stay at the default (info): at "verbose" nothing is printed at the default loglevel.
        ["-vn", "-i", file_path, "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True, timeout=max(60, int(duration_s * 2)),
    )
    output = result.stderr

    # Parse lines: "t: X.XXX  [TARGET:-23 LUFS]  M: Y.YY S: Z.ZZ  I: W.WW LRA: ..."
    pattern = re.compile(r"t:\s*([\d.]+)\s+(?:TARGET:\S+\s+LUFS\s+)?M:\s*([-\d.]+)\s+S:\s*([-\d.]+)")
    buckets: dict[float, list[float]] = defaultdict(list)

    for m in pattern.finditer(output):
        t = float(m.group(1))
        s_loudness = float(m.group(3))
        if s_loudness > -70:  # filter silence
            center = (t // window_s) * window_s + window_s / 2
            buckets[center].append(s_loudness)

    return {center: max(vals) for center, vals in buckets.items()}


def _scene_density(file_path: str, duration_s: float, window_s: float = 10.0) -> dict[float, float]:
    """Return {t_center: scene_change_count} using ffmpeg scdet."""
    result = ff_run(
        ["-an", "-i", file_path, "-vf", "scdet=threshold=10", "-f", "null", "-"],
        capture_output=True, text=True, timeout=max(60, int(duration_s * 2)),
    )
    output = result.stderr

    re.compile(r"pts_time:([\d.]+).*?score:([\d.]+)", re.DOTALL)
    buckets: dict[float, float] = defaultdict(float)

    for line in output.splitlines():
        # older ffmpeg prints "pts_time:12.3"; newer prints "lavfi.scd.time: 12.3"
        m = re.search(r"Parsed_scdet.*(?:pts_time:|lavfi\.scd\.time:\s*)([\d.]+)", line)
        if m:
            t = float(m.group(1))
            center = (t // window_s) * window_s + window_s / 2
            buckets[center] += 1.0

    return dict(buckets)


def _keyword_hits(segments_json: str, keywords: list[str], window_s: float = 10.0) -> dict[float, float]:
    """Return {t_center: hit_count} from transcript segments."""
    segments = json.loads(segments_json)
    lower_kw = [k.lower() for k in keywords]
    buckets: dict[float, float] = defaultdict(float)

    for seg in segments:
        text = seg["text"].lower()
        hits = sum(1 for kw in lower_kw if kw in text)
        if hits:
            t = (seg["start"] + seg["end"]) / 2
            center = (t // window_s) * window_s + window_s / 2
            buckets[center] += hits

    return dict(buckets)


# ─── Z-score and NMS ──────────────────────────────────────────────────────────

def _zscore(values: list[float]) -> list[float]:
    if len(values) < 2:
        return [0.0] * len(values)
    mu = sum(values) / len(values)
    var = sum((v - mu) ** 2 for v in values) / len(values)
    std = math.sqrt(var) if var > 0 else 1.0
    return [(v - mu) / std for v in values]


def _nms(moments: list[tuple[float, float]], window_s: float) -> list[tuple[float, float]]:
    """Non-maximum suppression. Returns kept (t, score) sorted descending."""
    if not moments:
        return []
    sorted_m = sorted(moments, key=lambda x: x[1], reverse=True)
    kept: list[tuple[float, float]] = []
    suppressed: set[int] = set()

    for i, (t, score) in enumerate(sorted_m):
        if i in suppressed:
            continue
        kept.append((t, score))
        for j, (t2, _) in enumerate(sorted_m):
            if j != i and j not in suppressed and abs(t2 - t) < window_s:
                suppressed.add(j)

    return kept


# ─── Main scoring function ────────────────────────────────────────────────────

def score_source(
    source_id: str,
    file_path: str,
    duration_s: float,
    youtube_video_id: str | None,
    segments_json: str,
    cfg: ThresholdsConfig | None = None,
) -> int:
    """Score a source, write moments to DB. Returns number of moments above min_score."""
    cfg = cfg or load_thresholds()
    w = cfg.score.weights
    keywords = cfg.score.keywords
    window_s = 10.0

    # Delete any stale moments for this source before re-scoring.
    with get_conn() as conn:
        conn.execute("DELETE FROM moments WHERE source_id=?", (source_id,))

    log.info("scoring", extra={"source_id": source_id[:12]})

    # Collect raw signal maps {t_center: value}
    cd_map = _comment_density(youtube_video_id, duration_s)
    lp_map = _loudness_signal(file_path, duration_s, window_s)
    sd_map = _scene_density(file_path, duration_s, window_s)
    kh_map = _keyword_hits(segments_json, keywords, window_s)

    # Build union of all time centers
    all_centers = sorted(
        set(cd_map) | set(lp_map) | set(sd_map) | set(kh_map)
    )

    if not all_centers:
        log.warning("no_signal_data", extra={"source_id": source_id[:12]})
        return 0

    # Build signal lists (fill 0 where absent)
    cd_vals = [cd_map.get(t, 0.0) for t in all_centers]
    lp_vals = [lp_map.get(t, 0.0) for t in all_centers]
    sd_vals = [sd_map.get(t, 0.0) for t in all_centers]
    kh_vals = [kh_map.get(t, 0.0) for t in all_centers]

    # Z-score each signal within this source
    cd_z = _zscore(cd_vals)
    lp_z = _zscore(lp_vals)
    sd_z = _zscore(sd_vals)
    kh_z = _zscore(kh_vals)

    moments_raw: list[tuple[float, float, dict]] = []
    for i, t in enumerate(all_centers):
        composite = (
            w.comment_density * cd_z[i]
            + w.loudness_peak * lp_z[i]
            + w.scene_density * sd_z[i]
            + w.keyword_hits * kh_z[i]
        )
        signals = {
            "comment_density": round(cd_z[i], 4),
            "loudness_peak": round(lp_z[i], 4),
            "scene_density": round(sd_z[i], 4),
            "keyword_hits": round(kh_z[i], 4),
            "raw_comment_density": round(cd_vals[i], 4),
            "raw_loudness_peak": round(lp_vals[i], 4),
            "raw_scene_density": round(sd_vals[i], 4),
            "raw_keyword_hits": round(kh_vals[i], 4),
        }
        moments_raw.append((t, composite, signals))

    # NMS
    nms_input = [(t, score) for t, score, _ in moments_raw]
    kept_set = {t for t, _ in _nms(nms_input, cfg.score.nms_window_seconds)}

    above = 0
    with get_conn() as conn:
        for t, composite, signals in moments_raw:
            if t not in kept_set:
                continue
            t_start = max(0.0, t - window_s / 2)
            t_end = min(duration_s, t + window_s / 2)
            conn.execute(
                """INSERT INTO moments (source_id, t_start, t_end, score, signals_json)
                   VALUES (?,?,?,?,?)""",
                (source_id, t_start, t_end, round(composite, 6), json.dumps(signals)),
            )
            if composite >= cfg.score.min_score:
                above += 1

        conn.execute("UPDATE sources SET status='scored' WHERE id=?", (source_id,))

    log.info(
        "scoring_done",
        extra={
            "source_id": source_id[:12],
            "moments": len(kept_set),
            "above_threshold": above,
        },
    )
    return above


def run_score(cfg: ThresholdsConfig | None = None) -> dict[str, int]:
    cfg = cfg or load_thresholds()

    with get_conn() as conn:
        sources = conn.execute(
            """SELECT s.id, s.file_path, s.duration_s, s.youtube_video_id,
                      t.segments_json
               FROM sources s
               JOIN transcripts t ON t.source_id = s.id
               WHERE s.status = 'transcribed'"""  # scored sources are done; re-scoring breaks existing clips
        ).fetchall()

    counts = {"scored": 0, "errors": 0}
    for row in sources:
        try:
            score_source(
                row["id"],
                row["file_path"],
                row["duration_s"],
                row["youtube_video_id"],
                row["segments_json"],
                cfg,
            )
            counts["scored"] += 1
        except Exception as exc:
            log.error("score_failed", extra={"source_id": row["id"][:12], "err": str(exc)})
            counts["errors"] += 1

    return counts


def print_top_moments(source_id: str | None = None, top_n: int = 10) -> None:
    """Print top scored moments with signal breakdown. Used by CLI."""
    from rich.console import Console
    from rich.table import Table

    with get_conn() as conn:
        query = """
            SELECT m.id, m.source_id, m.t_start, m.t_end, m.score, m.signals_json,
                   s.file_path
            FROM moments m
            JOIN sources s ON s.id = m.source_id
        """
        params: tuple = ()
        if source_id:
            query += " WHERE m.source_id = ?"
            params = (source_id,)
        query += " ORDER BY m.score DESC LIMIT ?"
        params = (*params, top_n)
        rows = conn.execute(query, params).fetchall()

    console = Console()
    table = Table(title=f"Top {top_n} Moments", show_lines=True)
    table.add_column("Score", style="bold yellow", justify="right")
    table.add_column("t_start", justify="right")
    table.add_column("t_end", justify="right")
    table.add_column("cd_z", justify="right")
    table.add_column("lp_z", justify="right")
    table.add_column("sd_z", justify="right")
    table.add_column("kh_z", justify="right")
    table.add_column("source", overflow="fold")

    for row in rows:
        sig = json.loads(row["signals_json"])
        table.add_row(
            f"{row['score']:.3f}",
            f"{row['t_start']:.1f}s",
            f"{row['t_end']:.1f}s",
            f"{sig.get('comment_density', 0):.2f}",
            f"{sig.get('loudness_peak', 0):.2f}",
            f"{sig.get('scene_density', 0):.2f}",
            f"{sig.get('keyword_hits', 0):.2f}",
            Path(row["file_path"]).name,
        )

    console.print(table)
