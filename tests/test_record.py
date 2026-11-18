"""record(): streamlink is piped into a segmenting ffmpeg, and both are shut down at the end."""

from pathlib import Path

from clipsys import record as rec


class FakeProc:
    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs, self.terminated, self.returncode = cmd, kwargs, False, 0
        self.stdout = self

    def close(self): pass
    def poll(self): return 0
    def wait(self, timeout=None): return 0
    def terminate(self): self.terminated = True


def test_record_pipes_streamlink_into_segmenting_ffmpeg(monkeypatch, tmp_path):
    procs = []
    monkeypatch.setattr(rec, "streamlink_bin", lambda: "streamlink")
    monkeypatch.setattr(rec.subprocess, "Popen", lambda cmd, **kw: procs.append(FakeProc(cmd, **kw)) or procs[-1])
    out = tmp_path / "recordings"

    assert rec.record("https://twitch.tv/x", out, "720p", 600) == 0

    sl, ff = procs
    assert out.is_dir()
    assert sl.cmd == ["streamlink", "--retry-streams", "60", "--stdout", "https://twitch.tv/x", "720p"]
    assert ff.kwargs["stdin"] is sl.stdout
    assert ff.cmd[ff.cmd.index("-segment_time") + 1] == "600"
    assert Path(ff.cmd[-1]).parent == out and ff.cmd[-1].endswith(".mp4")
    assert sl.terminated
