"""Watch a folder of stream recordings and register finished files in inbox/manifest.csv.

The recorder (`clipsys record`, OBS with automatic file splitting, or a creator's shared
recordings folder) writes chunks into the folder; this module only registers finished files.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from clipsys.ingest import MANIFEST

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".ts", ".flv", ".webm"}
HEADER = ["file", "program_id", "creator", "source_url_or_empty", "received_at"]


def finished_files(folder: Path, sizes: dict[Path, int], known: set[str]) -> list[Path]:
    """Return video files that are not in `known` and have stopped growing.

    ponytail: "finished" = same non-zero size on two consecutive polls. A recorder that
    stalls for a whole poll interval mid-chunk would be picked up early; if that happens,
    raise --interval or switch to checking for an exclusive-open lock.
    """
    done = []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in VIDEO_EXTS or not path.is_file() or str(path) in known:
            continue
        size = path.stat().st_size
        if size and sizes.get(path) == size:
            done.append(path)
        sizes[path] = size
    return done


def manifest_files() -> set[str]:
    if not MANIFEST.exists():
        return set()
    with MANIFEST.open(newline="", encoding="utf-8") as fh:
        return {row["file"].strip() for row in csv.DictReader(fh)}


def add_to_manifest(paths: list[Path], program_id: str, creator: str) -> None:
    """Append rows using absolute paths (ingest resolves them as-is and copies the file)."""
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    existing = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
    with MANIFEST.open("a", newline="", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        writer = csv.writer(fh)
        if not existing.strip():
            writer.writerow(HEADER)
        for path in paths:
            writer.writerow([str(path), program_id, creator, "", date.today().isoformat()])
