"""Thin wrappers around ffmpeg/ffprobe that respect FFMPEG_BIN / FFPROBE_BIN env vars."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence


def _ff_bin(name: str) -> str:
    from clipsys._env import settings
    return settings.ffmpeg_bin if name == "ffmpeg" else settings.ffprobe_bin


def ff_run(args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """Run ffmpeg with the configured binary."""
    return subprocess.run([_ff_bin("ffmpeg"), *args], **kwargs)


def ffprobe_run(args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """Run ffprobe with the configured binary."""
    return subprocess.run([_ff_bin("ffprobe"), *args], **kwargs)
