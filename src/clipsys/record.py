"""Record a live stream into fixed-length chunks that `clipsys watch` can pick up.

streamlink fetches the stream and pipes it to ffmpeg, which copies it (no re-encode) into
files of `segment_seconds` each. A chunk is complete the moment the next one starts, so
clips can be made while the stream is still running.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from clipsys._ff import _ff_bin


def streamlink_bin() -> str:
    """Find streamlink on PATH, or next to the Python that runs clipsys (pip install)."""
    found = shutil.which("streamlink")
    if found:
        return found
    sibling = Path(sys.executable).parent / ("streamlink.exe" if sys.platform == "win32" else "streamlink")
    if sibling.exists():
        return str(sibling)
    raise FileNotFoundError("streamlink not found. Install it: winget install Streamlink.Streamlink")


def ffmpeg_args(out_dir: Path, segment_seconds: int) -> list[str]:
    return [
        _ff_bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
        "-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1",
        "-strftime", "1", str(out_dir / "%Y%m%d-%H%M%S.mp4"),
    ]


def record(url: str, out_dir: Path, quality: str = "best", segment_seconds: int = 600) -> int:
    """Record until the stream ends or Ctrl+C. Waits if the stream is not live yet."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # --retry-streams: if the channel is offline, check again every 60 s instead of exiting.
    sl = subprocess.Popen(
        [streamlink_bin(), "--retry-streams", "60", "--stdout", url, quality],
        stdout=subprocess.PIPE,
    )
    ff = subprocess.Popen(ffmpeg_args(out_dir, segment_seconds), stdin=sl.stdout)
    sl.stdout.close()  # ffmpeg owns the pipe now; it sees end-of-file when streamlink stops
    try:
        # Poll rather than ff.wait(): a blocking wait on Windows cannot be interrupted by Ctrl+C.
        while ff.poll() is None:
            time.sleep(0.5)
        return ff.returncode
    except KeyboardInterrupt:
        return 0
    finally:
        # Stop the download first so ffmpeg reaches end-of-file and closes the last chunk cleanly.
        sl.terminate()
        try:
            ff.wait(timeout=20)
        except subprocess.TimeoutExpired:
            ff.kill()
