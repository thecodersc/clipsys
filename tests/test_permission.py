"""Tests for the permission gate in config loading and ingest."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from clipsys.config import Program, ProgramsConfig, RenderProfile, load_programs
from clipsys.db.db import get_conn


def _make_programs(*ids: str, permission: bool = True) -> ProgramsConfig:
    """Return a ProgramsConfig with the given program IDs."""
    return ProgramsConfig(programs=[
        Program(
            id=pid,
            name=pid,
            credit_line=f"@{pid}",
            require_link_to_creator=False,
            allowed_platforms=["youtube_shorts"],
            clip_permission=permission,
            permission_ref=f"ref-{pid}" if permission else "",
        )
        for pid in ids
    ])


# ─── load_programs filtering ──────────────────────────────────────────────────

def _write_programs(tmp_path: Path, programs: list[dict]) -> Path:
    import yaml
    p = tmp_path / "programs.yaml"
    p.write_text(yaml.dump({"programs": programs}))
    return p


class TestLoadProgramsFilter:
    def test_permitted_program_included(self, tmp_path):
        p = _write_programs(tmp_path, [{
            "id": "prog_ok",
            "name": "OK",
            "credit_line": "@ok",
            "require_link_to_creator": False,
            "allowed_platforms": ["youtube_shorts"],
            "clip_permission": True,
            "permission_ref": "agreement-2024",
        }])
        cfg = load_programs(p)
        assert len(cfg.programs) == 1
        assert cfg.programs[0].id == "prog_ok"

    def test_missing_clip_permission_dropped(self, tmp_path):
        p = _write_programs(tmp_path, [{
            "id": "prog_no_perm",
            "name": "NoPerm",
            "credit_line": "@x",
            "require_link_to_creator": False,
            "allowed_platforms": ["youtube_shorts"],
            # clip_permission omitted → defaults False
            "permission_ref": "agreement-2024",
        }])
        cfg = load_programs(p)
        assert len(cfg.programs) == 0

    def test_clip_permission_false_dropped(self, tmp_path):
        p = _write_programs(tmp_path, [{
            "id": "prog_false",
            "name": "False",
            "credit_line": "@x",
            "require_link_to_creator": False,
            "allowed_platforms": ["youtube_shorts"],
            "clip_permission": False,
            "permission_ref": "agreement-2024",
        }])
        cfg = load_programs(p)
        assert len(cfg.programs) == 0

    def test_empty_permission_ref_dropped(self, tmp_path):
        p = _write_programs(tmp_path, [{
            "id": "prog_no_ref",
            "name": "NoRef",
            "credit_line": "@x",
            "require_link_to_creator": False,
            "allowed_platforms": ["youtube_shorts"],
            "clip_permission": True,
            "permission_ref": "",
        }])
        cfg = load_programs(p)
        assert len(cfg.programs) == 0

    def test_mixed_list_only_permitted_pass(self, tmp_path):
        p = _write_programs(tmp_path, [
            {
                "id": "good",
                "name": "Good",
                "credit_line": "@g",
                "require_link_to_creator": False,
                "allowed_platforms": ["youtube_shorts"],
                "clip_permission": True,
                "permission_ref": "ref-001",
            },
            {
                "id": "bad",
                "name": "Bad",
                "credit_line": "@b",
                "require_link_to_creator": False,
                "allowed_platforms": ["youtube_shorts"],
                "clip_permission": False,
                "permission_ref": "ref-002",
            },
        ])
        cfg = load_programs(p)
        assert len(cfg.programs) == 1
        assert cfg.programs[0].id == "good"

    def test_pipeline_still_starts_with_some_permitted(self, tmp_path):
        """Dropping one program does not crash load_programs; others are returned."""
        p = _write_programs(tmp_path, [
            {
                "id": "p1",
                "name": "P1",
                "credit_line": "@p1",
                "require_link_to_creator": False,
                "allowed_platforms": ["youtube_shorts"],
                "clip_permission": True,
                "permission_ref": "ref-p1",
            },
            {
                "id": "p2",
                "name": "P2",
                "credit_line": "@p2",
                "require_link_to_creator": False,
                "allowed_platforms": ["youtube_shorts"],
                # missing clip_permission
            },
        ])
        cfg = load_programs(p)
        assert len(cfg.programs) == 1
        assert cfg.programs[0].id == "p1"


# ─── ingest permission gate ───────────────────────────────────────────────────

class TestIngestPermissionGate:
    def _setup_inbox(self, base: Path) -> None:
        (base / "inbox").mkdir(parents=True, exist_ok=True)
        (base / "data" / "sources").mkdir(parents=True, exist_ok=True)
        (base / "inbox" / "clip.mp4").write_bytes(b"\x00" * 16)

    def _write_manifest(self, inbox: Path, program_id: str) -> None:
        mf = inbox / "manifest.csv"
        with mf.open("w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["file", "program_id", "creator", "source_url_or_empty", "received_at"],
            )
            writer.writeheader()
            writer.writerow({
                "file": "clip.mp4",
                "program_id": program_id,
                "creator": "@creator",
                "source_url_or_empty": "",
                "received_at": "2024-01-01T00:00:00",
            })

    def _patch_paths(self, monkeypatch, base: Path) -> None:
        import clipsys.ingest as ingest_mod
        monkeypatch.setattr(ingest_mod, "INBOX", base / "inbox")
        monkeypatch.setattr(ingest_mod, "SOURCES_DIR", base / "data/sources")
        monkeypatch.setattr(ingest_mod, "MANIFEST", base / "inbox" / "manifest.csv")

    def test_unpermitted_program_id_never_reaches_probe(self, tmp_path, monkeypatch):
        """A file with an unpermitted program_id is skipped before ffprobe is called."""
        import clipsys.ingest as ingest_mod

        self._setup_inbox(tmp_path)
        self._write_manifest(tmp_path / "inbox", "not_permitted")
        self._patch_paths(monkeypatch, tmp_path)

        # load_programs returns only "permitted" — "not_permitted" won't be found
        monkeypatch.setattr(ingest_mod, "load_programs", lambda: _make_programs("permitted"))

        probe_called = []
        monkeypatch.setattr(ingest_mod, "_probe_validate", lambda p: probe_called.append(p) or (60.0, True, True))

        from clipsys.ingest import run_ingest
        counts = run_ingest()

        assert len(probe_called) == 0, "ffprobe must not be called for unpermitted program"
        assert counts["skipped"] >= 1
        assert counts["added"] == 0

    def test_permitted_program_id_proceeds_to_probe(self, tmp_path, monkeypatch):
        """A file with a permitted program_id reaches ffprobe."""
        import clipsys.ingest as ingest_mod

        self._setup_inbox(tmp_path)
        self._write_manifest(tmp_path / "inbox", "permitted")
        self._patch_paths(monkeypatch, tmp_path)

        # load_programs returns "permitted" with clip_permission=True
        monkeypatch.setattr(ingest_mod, "load_programs", lambda: _make_programs("permitted"))

        probe_called = []
        monkeypatch.setattr(ingest_mod, "_probe_validate", lambda p: probe_called.append(p) or (60.0, True, True))

        from clipsys.ingest import run_ingest
        run_ingest()

        assert len(probe_called) == 1, "ffprobe must be called for a permitted program"

    def test_no_permitted_programs_blocks_all_ingests(self, tmp_path, monkeypatch):
        """When no program is permitted, nothing is registered regardless of manifest."""
        import clipsys.ingest as ingest_mod

        self._setup_inbox(tmp_path)
        self._write_manifest(tmp_path / "inbox", "prog_a")
        self._patch_paths(monkeypatch, tmp_path)

        # load_programs returns empty list — all programs filtered out
        monkeypatch.setattr(ingest_mod, "load_programs", lambda: ProgramsConfig(programs=[]))

        from clipsys.ingest import run_ingest
        counts = run_ingest()

        assert counts["added"] == 0
        with get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) as n FROM sources").fetchone()["n"]
        assert n == 0
