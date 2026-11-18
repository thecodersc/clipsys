"""Render per-account variants: crop, zoom, scale to 1080x1920, loudnorm, captions."""

from __future__ import annotations

import json
from pathlib import Path

from clipsys._ff import ff_run, ffprobe_run
from clipsys._log import get_logger
from clipsys.config import Account, AccountsConfig, ThresholdsConfig, load_accounts, load_thresholds
from clipsys.db.db import get_conn

log = get_logger(__name__)

VARIANTS_DIR = Path("data/variants")
TARGET_W, TARGET_H = 1080, 1920


def _has_nvenc() -> bool:
    result = ff_run(
        ["-hide_banner", "-encoders"],
        capture_output=True, text=True, timeout=10,
    )
    return "h264_nvenc" in result.stdout


def _build_ass_captions(segments: list[dict], t_offset: float) -> str:
    """Build a minimal ASS subtitle string from transcript segments."""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, "
        "Alignment, MarginL, MarginR, MarginV, Outline, Shadow",
        "Style: Default,Arial,52,&H00FFFFFF,&H00000000,-1,2,80,80,120,2,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Text",
    ]

    def _fmt_time(t: float) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    for seg in segments:
        start = max(0.0, seg["start"] - t_offset)
        end = max(0.0, seg["end"] - t_offset)
        text = seg["text"].strip().replace("\n", " ")  # Text is the last ASS field: commas need no escaping
        lines.append(f"Dialogue: 0,{_fmt_time(start)},{_fmt_time(end)},Default,{text}")

    return "\n".join(lines)


def _probe_duration(path: str) -> float:
    result = ffprobe_run(
        ["-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True, timeout=30,
    )
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def render_variant(
    variant_id: str,
    clip_id: str,
    clip_path: str,
    clip_t_start: float,
    account: Account,
    segments_json: str,
    thr: ThresholdsConfig,
    use_nvenc: bool | None = None,
) -> str:
    """Render one variant. Returns output file path."""
    VARIANTS_DIR.mkdir(parents=True, exist_ok=True)

    if use_nvenc is None:
        use_nvenc = _has_nvenc()
    encoder = "h264_nvenc" if use_nvenc else "libx264"
    if not use_nvenc:
        log.warning("nvenc_unavailable_fallback_libx264")

    rp = account.render_profile
    zoom = rp.zoom
    crop_offset = rp.crop_offset

    # Build ASS subtitles
    segments = json.loads(segments_json)
    # Filter segments within clip window
    clip_duration = _probe_duration(clip_path)
    t_clip_end = clip_t_start + clip_duration
    clip_segs = [
        s for s in segments
        if s["end"] >= clip_t_start and s["start"] <= t_clip_end
    ]
    ass_content = _build_ass_captions(clip_segs, clip_t_start)
    ass_path = VARIANTS_DIR / f"{variant_id}.ass"
    ass_path.write_text(ass_content, encoding="utf-8")

    out_path = VARIANTS_DIR / f"{variant_id}.mp4"

    # Crop: source is arbitrary aspect ratio → crop to 9:16 centered/left/right
    # We scale to fit height=1920, then crop width=1080
    # zoom is applied before crop
    # crop_x offset: left=0, center=(iw-1080)/2, right=iw-1080
    crop_x = {
        "left": "0",
        "center": "(iw-1080)/2",
        "right": "iw-1080",
    }[crop_offset]

    ass = f"ass={ass_path.as_posix()}"  # forward slashes: backslashes are escapes in ffmpeg filter args
    if rp.layout == "blur":
        # Whole frame stays visible: the source is fitted to the 1080 width (times zoom, so
        # zoom > 1 trims the sides) and centred over a blurred, screen-filling copy of itself.
        fg_w = int(TARGET_W * zoom) // 2 * 2
        vf = (
            f"split[a][b];"
            f"[a]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
            f"crop={TARGET_W}:{TARGET_H},boxblur=20:5[bg];"
            f"[b]scale={fg_w}:-2:flags=lanczos,crop={TARGET_W}:ih:{crop_x}:0[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{ass}"
        )
    else:
        vf = ",".join([
            f"scale=-1:{int(TARGET_H * zoom)}:flags=lanczos",
            f"crop={TARGET_W}:{TARGET_H}:{crop_x}:(ih-{TARGET_H})/2",
            "scale=1080:1920:flags=lanczos",
            ass,
        ])

    af = "loudnorm=I=-14:TP=-2:LRA=11"

    encode_opts = ["-preset", "p4", "-cq", "28"] if use_nvenc else ["-preset", "medium", "-crf", "23"]

    log.info("rendering", extra={"variant_id": variant_id[:8], "encoder": encoder})
    result = ff_run(
        ["-y", "-i", clip_path, "-vf", vf, "-af", af, "-c:v", encoder,
         *encode_opts, "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", "-pix_fmt", "yuv420p", str(out_path)],
        capture_output=True, text=True, timeout=600,
    )

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg render failed: {result.stderr[-800:]}")

    actual_dur = _probe_duration(str(out_path))

    # Verify length band
    band = rp.length_band
    if band == "short":
        ok = thr.clips.short.min_s <= actual_dur <= thr.clips.short.max_s
    else:
        ok = thr.clips.long.min_s <= actual_dur <= thr.clips.long.max_s

    if not ok:
        log.warning(
            "variant_outside_length_band",
            extra={"variant_id": variant_id[:8], "duration": round(actual_dur, 1), "band": band},
        )

    with get_conn() as conn:
        conn.execute(
            "UPDATE variants SET file_path=?, status='rendered', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
            (str(out_path), variant_id),
        )

    # Clean up ASS file
    ass_path.unlink(missing_ok=True)

    log.info(
        "rendered",
        extra={
            "variant_id": variant_id[:8],
            "duration": round(actual_dur, 1),
            "out": out_path.name,
        },
    )
    return str(out_path)


def run_render(
    accts_cfg: AccountsConfig | None = None,
    thr: ThresholdsConfig | None = None,
) -> dict[str, int]:
    accts_cfg = accts_cfg or load_accounts()
    thr = thr or load_thresholds()
    use_nvenc = _has_nvenc()

    with get_conn() as conn:
        variants = conn.execute(
            """SELECT v.id, v.clip_id, v.account_id,
                      c.file_path as clip_path, c.t_start,
                      t.segments_json
               FROM variants v
               JOIN clips c ON c.id = v.clip_id
               JOIN sources s ON s.id = c.source_id
               JOIN transcripts t ON t.source_id = s.id
               WHERE v.status = 'pending'"""
        ).fetchall()

    counts = {"rendered": 0, "errors": 0}
    for row in variants:
        try:
            account = accts_cfg.by_id(row["account_id"])
        except KeyError:
            log.error("unknown_account", extra={"account_id": row["account_id"]})
            counts["errors"] += 1
            continue

        try:
            render_variant(
                row["id"],
                row["clip_id"],
                row["clip_path"],
                row["t_start"],
                account,
                row["segments_json"],
                thr,
                use_nvenc=use_nvenc,
            )
            counts["rendered"] += 1
        except Exception as exc:
            log.error("render_failed", extra={"variant_id": row["id"][:8], "err": str(exc)})
            with get_conn() as conn:
                conn.execute(
                    "UPDATE variants SET status='error', updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                    (row["id"],),
                )
            counts["errors"] += 1

    return counts
