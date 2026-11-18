"""System health checks. `clipsys doctor` prints a pass/fail/warn table."""

from __future__ import annotations

import shutil
from pathlib import Path

from rich.console import Console
from rich.table import Table

# ─── Individual checks ────────────────────────────────────────────────────────
# Each returns (passed: bool, warn_only: bool, detail: str)

def _ffmpeg_present() -> tuple[bool, bool, str]:
    from clipsys._env import settings
    ffmpeg = settings.ffmpeg_bin
    ffprobe = settings.ffprobe_bin
    # Check if the bin is a full path that exists, or on PATH
    ff_ok = Path(ffmpeg).exists() if Path(ffmpeg).is_absolute() else bool(shutil.which(ffmpeg))
    fp_ok = Path(ffprobe).exists() if Path(ffprobe).is_absolute() else bool(shutil.which(ffprobe))
    if ff_ok and fp_ok:
        return True, False, f"ffmpeg={ffmpeg} ffprobe={ffprobe}"
    missing = []
    if not ff_ok:
        missing.append(f"ffmpeg ({ffmpeg})")
    if not fp_ok:
        missing.append(f"ffprobe ({ffprobe})")
    return False, False, f"not found: {', '.join(missing)} — set FFMPEG_BIN/FFPROBE_BIN in .env"


def _nvenc_available() -> tuple[bool, bool, str]:
    from clipsys._ff import ff_run
    try:
        result = ff_run(["-hide_banner", "-encoders"], capture_output=True, text=True, timeout=10)
        if "h264_nvenc" in result.stdout:
            return True, False, "h264_nvenc available"
        return True, True, "h264_nvenc NOT available — will use libx264"
    except Exception as exc:
        return True, True, f"could not check encoders: {exc}"


def _cuda_for_whisper() -> tuple[bool, bool, str]:
    try:
        import ctranslate2  # type: ignore[import]
        devs = ctranslate2.get_cuda_device_count()
        if devs > 0:
            return True, False, f"CUDA devices: {devs}"
        return True, True, "No CUDA devices (CPU inference)"
    except ImportError:
        return True, True, "ctranslate2 not installed"


def _ollama_reachable() -> tuple[bool, bool, str]:
    import httpx

    from clipsys._env import settings

    try:
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
        resp.raise_for_status()
    except Exception as exc:
        return False, False, f"Ollama unreachable: {exc}"

    models = [m["name"] for m in resp.json().get("models", [])]
    target = settings.ollama_model
    if target not in models:
        return False, False, f"Model '{target}' not pulled. Available: {models}"
    return True, False, f"model '{target}' ready"


def _disk_space() -> tuple[bool, bool, str]:
    usage = shutil.disk_usage(".")
    free_gb = usage.free / 1e9
    if free_gb < 5:
        return False, False, f"Low disk space: {free_gb:.1f} GB free"
    return True, False, f"{free_gb:.1f} GB free"


def _tokens_present() -> tuple[bool, bool, str]:
    from clipsys.config import load_accounts
    tokens_dir = Path("data/tokens")
    accts = load_accounts()
    missing = [
        acct.token_ref
        for acct in accts.accounts
        if acct.platform == "youtube_shorts"
        and not (tokens_dir / f"{acct.token_ref}.json").exists()
    ]
    if missing:
        return True, True, f"Missing tokens: {missing} (run OAuth flow for live YT publishing)"
    return True, False, "all YT tokens present"


def _quota_remaining() -> tuple[bool, bool, str]:
    from clipsys.config import load_accounts
    from clipsys.publish.quota import DEFAULT_BUDGET, units_used_today

    accts = load_accounts()
    yt_accounts = [a for a in accts.accounts if a.platform == "youtube_shorts"]
    if not yt_accounts:
        return True, True, "no YT accounts configured"
    parts = [f"{a.id}: {units_used_today(a.id)}/{DEFAULT_BUDGET}" for a in yt_accounts[:4]]
    return True, False, ", ".join(parts)


def _kill_absent() -> tuple[bool, bool, str]:
    if Path("data/KILL").exists():
        return False, False, "data/KILL exists — publishing is halted!"
    return True, False, "ok"


def _db_accessible() -> tuple[bool, bool, str]:
    from clipsys.db.db import init_db
    init_db()
    return True, False, "db initialized"


# ─── Runner ───────────────────────────────────────────────────────────────────

def run_doctor(verbose: bool = False) -> bool:
    console = Console()
    table = Table(title="clipsys doctor", show_lines=True)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    checks = [
        ("ffmpeg + ffprobe", _ffmpeg_present),
        ("NVENC", _nvenc_available),
        ("CUDA / faster-whisper", _cuda_for_whisper),
        ("Ollama + model", _ollama_reachable),
        ("Disk space", _disk_space),
        ("YT tokens", _tokens_present),
        ("Quota remaining", _quota_remaining),
        ("KILL switch", _kill_absent),
        ("SQLite DB", _db_accessible),
    ]

    all_pass = True
    for label, fn in checks:
        try:
            passed, warn_only, detail = fn()
        except Exception as exc:
            passed, warn_only, detail = False, False, str(exc)[:120]

        if not passed:
            all_pass = False
            status = "[red]FAIL[/red]"
        elif warn_only:
            status = "[yellow]WARN[/yellow]"
        else:
            status = "[green]PASS[/green]"

        table.add_row(label, status, detail)

    console.print(table)
    if all_pass:
        console.print("[green]All checks passed (warnings are non-blocking).[/green]")
    else:
        console.print("[red]Some checks FAILED. See details above.[/red]")
    return all_pass
