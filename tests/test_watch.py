"""Watcher: only files that stopped growing are registered, once, with absolute paths."""

import csv

from clipsys import watch


def test_finished_files_and_manifest(tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    rec.mkdir()
    manifest = tmp_path / "inbox" / "manifest.csv"
    monkeypatch.setattr(watch, "MANIFEST", manifest)

    chunk = rec / "stream_01.mkv"
    chunk.write_bytes(b"a" * 10)
    (rec / "notes.txt").write_text("ignore me")
    (rec / "empty.mp4").write_bytes(b"")

    sizes: dict = {}
    assert watch.finished_files(rec, sizes, watch.manifest_files()) == []  # first sighting
    chunk.write_bytes(b"a" * 20)
    assert watch.finished_files(rec, sizes, watch.manifest_files()) == []  # still growing
    assert watch.finished_files(rec, sizes, watch.manifest_files()) == [chunk]  # stable

    watch.add_to_manifest([chunk], "prog", "creator")
    assert watch.finished_files(rec, sizes, watch.manifest_files()) == []  # not twice

    # Appending to a manifest without a trailing newline must not glue rows together.
    manifest.write_text(manifest.read_text().rstrip("\n"))
    other = rec / "stream_02.mp4"
    watch.add_to_manifest([other], "prog", "creator")
    rows = list(csv.DictReader(manifest.open(newline="", encoding="utf-8")))
    assert [r["file"] for r in rows] == [str(chunk), str(other)]
    assert rows[0]["program_id"] == "prog" and rows[0]["creator"] == "creator"
