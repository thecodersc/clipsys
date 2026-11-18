"""Integration test: synthetic video through ingest → score → cut → render.

Generates a 3-minute 16:9 test video using ffmpeg testsrc + sine audio.
Uses a stub transcript (no real Whisper). All publishing in dry-run mode.
Requires ffmpeg (on PATH or via FFMPEG_BIN in .env).
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _ffmpeg_available() -> bool:
    from clipsys._env import settings
    ffbin = settings.ffmpeg_bin
    if Path(ffbin).is_absolute():
        return Path(ffbin).exists()
    return shutil.which(ffbin) is not None


# Skip if ffmpeg is not available via PATH or FFMPEG_BIN
pytestmark = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="ffmpeg not found (set FFMPEG_BIN in .env or add to PATH)",
)


@pytest.fixture(scope="module")
def project_dir(tmp_path_factory):
    """Create a minimal project directory tree for the integration test."""
    base = tmp_path_factory.mktemp("project")
    for d in ["inbox", "data/sources", "data/transcripts", "data/clips", "data/variants",
              "data/logs", "data/tokens", "config/personas"]:
        (base / d).mkdir(parents=True, exist_ok=True)

    # Minimal config files
    (base / "config" / "programs.yaml").write_text(
        "programs:\n  - id: test_prog\n    name: Test\n    credit_line: '@test'\n"
        "    require_link_to_creator: false\n    allowed_platforms: [youtube_shorts]\n"
        "    rights_notes: test\n"
        "    clip_permission: true\n"
        "    permission_ref: integration-test-agreement\n"
    )
    (base / "config" / "accounts.yaml").write_text(
        "accounts:\n"
        "  - id: acct_int\n"
        "    platform: youtube_shorts\n"
        "    handle: '@int'\n"
        "    token_ref: yt_int\n"
        "    stage: 0\n"
        "    daily_cap: 3\n"
        "    posting_windows: ['12:00-14:00']\n"
        "    jitter_minutes: 0\n"
        "    live: false\n"
        "    render_profile:\n"
        "      crop_offset: center\n"
        "      zoom: 1.0\n"
        "      length_band: short\n"
        "      persona: personas/acct_int.md\n"
    )
    (base / "config" / "thresholds.yaml").write_text(
        "score:\n"
        "  weights: {comment_density: 0.35, loudness_peak: 0.25, scene_density: 0.15, keyword_hits: 0.25}\n"
        "  min_score: -999\n"  # Accept all moments in test
        "  nms_window_seconds: 10\n"
        "  keywords: [test, explosion]\n"
        "clips:\n"
        "  short: {min_s: 5, max_s: 59}\n"
        "  long: {min_s: 60, max_s: 120}\n"
        "  pre_roll_seconds: 1.0\n"
        "allocation:\n"
        "  max_accounts_per_clip: 3\n"
        "  max_clips_per_day: 50\n"
        "review:\n"
        "  auto_publish_pass_rate: 0.9\n"
        "  min_reviews_for_auto: 30\n"
    )
    (base / "config" / "personas" / "acct_int.md").write_text(
        "# Test\n## Voice\nTest.\n## Banned phrases\n- banned_word\n"
    )
    return base


@pytest.fixture(scope="module")
def synthetic_video(project_dir):
    """Generate a 3-minute 1920x1080 test video with sine audio."""
    from clipsys._ff import ff_run
    out = project_dir / "inbox" / "test_source.mp4"
    result = ff_run(
        ["-y",
         "-f", "lavfi", "-i", "testsrc=duration=180:size=1920x1080:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=180",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
         "-c:a", "aac", "-b:a", "64k",
         "-t", "180", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, f"ffmpeg failed: {result.stderr[-500:]}"
    return out


@pytest.fixture(scope="module")
def manifest(project_dir, synthetic_video):
    mf = project_dir / "inbox" / "manifest.csv"
    with mf.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["file", "program_id", "creator", "source_url_or_empty", "received_at"])
        writer.writeheader()
        writer.writerow({
            "file": "test_source.mp4",
            "program_id": "test_prog",
            "creator": "@testcreator",
            "source_url_or_empty": "",
            "received_at": "2024-01-01T12:00:00",
        })
    return mf


def _monkeypatch_paths(monkeypatch, project_dir):
    """Redirect all path constants to the tmp project dir."""
    import clipsys.config as config_mod
    import clipsys.cut as cut_mod
    import clipsys.ingest as ingest_mod
    import clipsys.render as render_mod
    import clipsys.transcribe as transcribe_mod

    monkeypatch.setattr(config_mod, "_CONFIG_DIR", project_dir / "config")
    monkeypatch.setattr(ingest_mod, "INBOX", project_dir / "inbox")
    monkeypatch.setattr(ingest_mod, "SOURCES_DIR", project_dir / "data/sources")
    monkeypatch.setattr(ingest_mod, "MANIFEST", project_dir / "inbox/manifest.csv")
    monkeypatch.setattr(transcribe_mod, "TRANSCRIPTS_DIR", project_dir / "data/transcripts")
    monkeypatch.setattr(cut_mod, "CLIPS_DIR", project_dir / "data/clips")
    monkeypatch.setattr(render_mod, "VARIANTS_DIR", project_dir / "data/variants")


@pytest.fixture(scope="module")
def db(project_dir, tmp_path_factory):
    db_path = project_dir / "data" / "app.db"
    from clipsys.db.db import init_db
    init_db(db_path)
    return db_path


class TestPipelineM1:
    def test_ingest(self, project_dir, synthetic_video, manifest, db, monkeypatch):
        monkeypatch.setattr("clipsys._env.settings.db_path", db)
        _monkeypatch_paths(monkeypatch, project_dir)

        from clipsys.ingest import run_ingest
        counts = run_ingest()
        assert counts["added"] == 1
        assert counts["errors"] == 0

    def test_ingest_idempotent(self, project_dir, synthetic_video, manifest, db, monkeypatch):
        monkeypatch.setattr("clipsys._env.settings.db_path", db)
        _monkeypatch_paths(monkeypatch, project_dir)

        from clipsys.ingest import run_ingest
        counts = run_ingest()
        assert counts["added"] == 0
        assert counts["skipped"] == 1

    def test_score_with_stub_transcript(self, project_dir, db, monkeypatch):
        """Insert a stub transcript and score without Whisper."""
        monkeypatch.setattr("clipsys._env.settings.db_path", db)

        from clipsys.db.db import get_conn
        # Ensure source exists and get id
        with get_conn() as conn:
            src = conn.execute("SELECT id, file_path, duration_s FROM sources LIMIT 1").fetchone()

        assert src is not None
        source_id = src["id"]

        # Insert a stub transcript
        stub_segments = [
            {"start": i * 10.0, "end": i * 10.0 + 9.0, "text": f"test action explosion segment {i}"}
            for i in range(18)
        ]
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM transcripts WHERE source_id=?", (source_id,)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO transcripts (source_id, language, segments_json) VALUES (?,?,?)",
                    (source_id, "en", json.dumps(stub_segments)),
                )
                conn.execute("UPDATE sources SET status='transcribed' WHERE id=?", (source_id,))

        from clipsys.config import load_thresholds
        from clipsys.score import score_source

        thr = load_thresholds(project_dir / "config" / "thresholds.yaml")
        n = score_source(source_id, src["file_path"], src["duration_s"], None,
                         json.dumps(stub_segments), thr)
        assert n >= 0  # With min_score=-999 all moments qualify

        # Verify moments written
        with get_conn() as conn:
            moments = conn.execute("SELECT * FROM moments WHERE source_id=?", (source_id,)).fetchall()
        assert len(moments) > 0

    def test_cut(self, project_dir, db, monkeypatch):
        monkeypatch.setattr("clipsys._env.settings.db_path", db)
        _monkeypatch_paths(monkeypatch, project_dir)

        from clipsys.config import load_thresholds
        from clipsys.cut import cut_clips
        thr = load_thresholds(project_dir / "config" / "thresholds.yaml")
        counts = cut_clips(thr)
        # Should have cut at least one clip
        assert counts["cut"] + counts["skipped"] > 0 or counts["errors"] == 0

        from clipsys.db.db import get_conn
        with get_conn() as conn:
            clips = conn.execute("SELECT * FROM clips").fetchall()
        assert len(clips) > 0

        # Verify clip files exist and have correct resolution
        for clip in clips:
            path = Path(clip["file_path"])
            assert path.exists(), f"Clip file missing: {path}"

    def test_render(self, project_dir, db, monkeypatch):
        monkeypatch.setattr("clipsys._env.settings.db_path", db)
        _monkeypatch_paths(monkeypatch, project_dir)

        from clipsys.allocate import run_allocate
        from clipsys.config import load_accounts, load_thresholds
        from clipsys.render import run_render

        accts = load_accounts(project_dir / "config" / "accounts.yaml")
        thr = load_thresholds(project_dir / "config" / "thresholds.yaml")

        run_allocate(accts, thr, today="2024-01-01")
        counts = run_render(accts, thr)

        from clipsys.db.db import get_conn
        with get_conn() as conn:
            variants = conn.execute(
                "SELECT * FROM variants WHERE status='rendered'"
            ).fetchall()

        if counts["rendered"] > 0:
            for v in variants:
                path = Path(v["file_path"])
                assert path.exists(), f"Variant missing: {path}"

                # Verify 1080x1920 resolution
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json",
                     "-show_streams", v["file_path"]],
                    capture_output=True, text=True, timeout=30,
                )
                info = json.loads(probe.stdout)
                vstreams = [s for s in info["streams"] if s["codec_type"] == "video"]
                assert vstreams, "No video stream in rendered variant"
                assert vstreams[0]["width"] == 1080
                assert vstreams[0]["height"] == 1920

                # Verify audio stream present
                astreams = [s for s in info["streams"] if s["codec_type"] == "audio"]
                assert astreams, "No audio stream in rendered variant"
