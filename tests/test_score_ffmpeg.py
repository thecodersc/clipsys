"""Loudness and scene parsers must understand real ffmpeg output (both old and new formats)."""

from types import SimpleNamespace

from clipsys import score

EBUR = (
    "[Parsed_ebur128_0 @ 0x1] t: 2.099977   TARGET:-23 LUFS    M: -25.0 S: -19.5     I: -23.3 LUFS\n"
    "[Parsed_ebur128_0 @ 0x1] t: 12.5    M: -20.0    S: -18.0  I: -23.3 LUFS\n"
)
SCDET = (
    "[Parsed_scdet_0 @ 0x1] lavfi.scd.score: 25.395, lavfi.scd.time: 49.516133\n"
    "[Parsed_scdet_0 @ 0x1] lavfi.scd.score: 12.0 pts_time:3.2\n"
)


def test_loudness_parses_new_and_old_lines(monkeypatch):
    monkeypatch.setattr(score, "ff_run", lambda *a, **k: SimpleNamespace(stderr=EBUR))
    assert score._loudness_signal("x.mp4", 60) == {5.0: -19.5, 15.0: -18.0}


def test_scene_parses_new_and_old_lines(monkeypatch):
    monkeypatch.setattr(score, "ff_run", lambda *a, **k: SimpleNamespace(stderr=SCDET))
    assert score._scene_density("x.mp4", 60) == {45.0: 1.0, 5.0: 1.0}
