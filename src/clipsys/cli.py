"""clipsys CLI — typer app."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import typer
from rich.console import Console

# Every path (config/, data/, inbox/, .env) is cwd-relative. If launched from elsewhere,
# fall back to the project root so the command works from any folder.
import os
import sys

# Old Windows consoles and pipes default to a legacy code page that cannot print "→" or emoji.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Packaged exe: the root is the folder holding clipsys.exe. From source: the repo root.
_ROOT = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
if not Path("config").is_dir() and (_ROOT / "config").is_dir():
    os.chdir(_ROOT)

app = typer.Typer(
    name="clipsys",
    help="GTA 6 clip pipeline: score, cut, caption, publish.",
    no_args_is_help=True,
)
console = Console()


def _ensure_db() -> None:
    from clipsys.db.db import init_db
    init_db()


def _with_credit(description: str, program) -> str:
    """Append the program's credit line (e.g. "Clip from @creator") to a description."""
    credit = program.credit_line.strip() if program else ""
    return "\n\n".join(part for part in (description.strip(), credit) if part)


# ─── Commands ─────────────────────────────────────────────────────────────────

@app.command()
def doctor() -> None:
    """Check all system dependencies and configuration."""
    _ensure_db()
    from clipsys.doctor import run_doctor
    ok = run_doctor()
    raise typer.Exit(0 if ok else 1)


@app.command()
def ingest() -> None:
    """Read inbox/manifest.csv and register source videos."""
    _ensure_db()
    from clipsys.ingest import run_ingest
    counts = run_ingest()
    console.print(f"[green]Ingest done:[/green] {counts}")


@app.command()
def transcribe(
    model: str = typer.Option("medium", help="Whisper model size"),
    compute: str = typer.Option("int8_float16", help="Compute type"),
) -> None:
    """Transcribe sources with faster-whisper."""
    _ensure_db()
    from clipsys.transcribe import run_transcribe
    counts = run_transcribe(model, compute)
    console.print(f"[green]Transcribe done:[/green] {counts}")


@app.command()
def score(
    top: int = typer.Option(10, help="Number of top moments to display"),
    source_id: str | None = typer.Option(None, help="Limit to one source ID"),
) -> None:
    """Score sources and print top moments with signal breakdown."""
    _ensure_db()
    from clipsys.score import print_top_moments, run_score
    counts = run_score()
    console.print(f"[green]Scoring done:[/green] {counts}")
    print_top_moments(source_id, top)


@app.command()
def cut() -> None:
    """Cut master clips from top-scored moments."""
    _ensure_db()
    from clipsys.cut import cut_clips
    counts = cut_clips()
    console.print(f"[green]Cut done:[/green] {counts}")


@app.command()
def allocate() -> None:
    """Allocate clips to accounts deterministically."""
    _ensure_db()
    from clipsys.allocate import run_allocate
    counts = run_allocate()
    console.print(f"[green]Allocate done:[/green] {counts}")


@app.command()
def render() -> None:
    """Render 1080x1920 variants with captions for each account."""
    _ensure_db()
    from clipsys.render import run_render
    counts = run_render()
    console.print(f"[green]Render done:[/green] {counts}")


@app.command()
def caption(
    model: str | None = typer.Option(None, help="Ollama model override"),
) -> None:
    """Generate captions via local LLM for rendered variants."""
    _ensure_db()
    from clipsys.caption import run_caption
    counts = run_caption(model)
    console.print(f"[green]Caption done:[/green] {counts}")


@app.command()
def review(
    host: str = typer.Option("127.0.0.1", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
) -> None:
    """Start the review UI at http://localhost:8000."""
    _ensure_db()
    import uvicorn
    console.print(f"[yellow]Review UI:[/yellow] http://{host}:{port}")
    from clipsys.review_app import app as review_app  # object, not import string: works when packaged

    uvicorn.run(review_app, host=host, port=port, reload=False)


@app.command()
def publish(
    live: bool = typer.Option(False, "--live", help="Enable live publishing (requires live:true in accounts.yaml)"),
) -> None:
    """Publish approved variants. Dry-run unless --live."""
    _ensure_db()

    if Path("data/KILL").exists():
        console.print("[red]KILL file present — publishing halted.[/red]")
        from clipsys.telegram import send_alert
        send_alert("⛔ clipsys KILL switch is active — publishing halted.")
        raise typer.Exit(1)

    import json

    from clipsys.config import load_accounts, load_programs, load_thresholds
    from clipsys.db.db import get_conn
    from clipsys.publish.caps import can_publish, recently_published_to_other_account
    from clipsys.publish.facebook import FacebookPublisher
    from clipsys.publish.quota import QuotaExceededError
    from clipsys.publish.tiktok import TikTokPublisher
    from clipsys.publish.youtube import YouTubePublisher
    from clipsys.telegram import send_alert, send_clip

    accts = load_accounts()
    thr = load_thresholds()
    programs = {p.id: p for p in load_programs().programs}

    with get_conn() as conn:
        variants = conn.execute(
            """SELECT v.id, v.clip_id, v.account_id, v.file_path, v.caption_json, v.status,
                      v.telegram_sent_at,
                      s.program_id
               FROM variants v
               JOIN clips c ON c.id = v.clip_id
               JOIN sources s ON s.id = c.source_id
               WHERE v.status IN ('approved','captioned')
               ORDER BY v.updated_at"""
        ).fetchall()

    published = 0
    skipped = 0

    for row in variants:
        try:
            account = accts.by_id(row["account_id"])
        except KeyError:
            skipped += 1
            continue

        allowed, reason = can_publish(
            account,
            row["status"],
            thr.review.auto_publish_pass_rate,
            thr.review.min_reviews_for_auto,
        )
        if not allowed:
            console.print(f"  skip {row['id'][:8]} ({reason})")
            skipped += 1
            continue

        if recently_published_to_other_account(row["clip_id"], account.id):
            console.print(f"  skip {row['id'][:8]} (cross_account_dedup)")
            skipped += 1
            continue

        is_live = live and account.live
        cap_data = {}
        if row["caption_json"]:
            cap_data = json.loads(row["caption_json"])
        description = _with_credit(cap_data.get("description", ""), programs.get(row["program_id"]))

        # Non-public accounts go to Telegram queue instead of platform API.
        if not account.api_public:
            if not row["telegram_sent_at"]:
                prog = programs.get(row["program_id"])
                permission_ref = prog.permission_ref if prog else ""
                msg_id = send_clip(
                    variant_id=row["id"],
                    file_path=row["file_path"] or "",
                    title=cap_data.get("title", "GTA 6 Clip"),
                    description=description,
                    permission_ref=permission_ref,
                    account_id=account.id,
                    platform=account.platform,
                )
                if msg_id:
                    with get_conn() as conn:
                        from datetime import datetime
                        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                        conn.execute(
                            """UPDATE variants SET status='held', telegram_sent_at=?,
                               updated_at=? WHERE id=?""",
                            (now, now, row["id"]),
                        )
                    console.print(f"  [dim]held {row['id'][:8]} → telegram {msg_id}[/dim]")
                    published += 1
                else:
                    console.print(f"  [dim]held {row['id'][:8]} (telegram unconfigured)[/dim]")
                    skipped += 1
            else:
                console.print(f"  [dim]skip {row['id'][:8]} (telegram already sent)[/dim]")
                skipped += 1
            continue

        try:
            if account.platform == "youtube_shorts":
                pub = YouTubePublisher(token_ref=account.token_ref, dry_run=not is_live)
            elif account.platform == "facebook_reels":
                pub = FacebookPublisher(dry_run=not is_live)
            elif account.platform == "tiktok":
                pub = TikTokPublisher(dry_run=not is_live)
            else:
                console.print(f"  [red]unknown platform {account.platform}[/red]")
                skipped += 1
                continue

            result = pub.publish(
                variant_id=row["id"],
                file_path=row["file_path"] or "",
                title=cap_data.get("title", "GTA 6 Clip"),
                description=description,
                tags=cap_data.get("hashtags", []),
                account_id=account.id,
                privacy_status="public",  # only api_public accounts reach this branch
            )
            if result.metadata.get("privacy") == "private":
                send_alert(f"clipsys: {result.platform_post_id} uploaded but still PRIVATE - make it public by hand.")

            with get_conn() as conn:
                from datetime import datetime
                now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    """UPDATE variants SET status='published', platform_post_id=?,
                       published_at=?, updated_at=? WHERE id=?""",
                    (result.platform_post_id, now, now, row["id"]),
                )
            console.print(
                f"  {'[DRY]' if result.dry_run else '[LIVE]'} {row['id'][:8]} → {result.platform_post_id}"
            )
            published += 1

            # Telegram notification — skip if already sent (dedup across restarts)
            if not row["telegram_sent_at"]:
                prog = programs.get(row["program_id"])
                permission_ref = prog.permission_ref if prog else ""
                msg_id = send_clip(
                    variant_id=row["id"],
                    file_path=row["file_path"] or "",
                    title=cap_data.get("title", "GTA 6 Clip"),
                    description=description,
                    permission_ref=permission_ref,
                    account_id=account.id,
                    platform=account.platform,
                )
                if msg_id:
                    with get_conn() as conn:
                        from datetime import datetime
                        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                        conn.execute(
                            "UPDATE variants SET telegram_sent_at=? WHERE id=?",
                            (now, row["id"]),
                        )

        except QuotaExceededError as exc:
            console.print(f"  [yellow]QUOTA {row['id'][:8]}: {exc}[/yellow]")
            send_alert(f"⚠️ clipsys quota exhausted for {account.id} — variant {row['id'][:8]} held.")
            with get_conn() as conn:
                from datetime import datetime
                now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    "UPDATE variants SET status='held', updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
            skipped += 1
        except Exception as exc:
            console.print(f"  [red]FAILED {row['id'][:8]}: {exc}[/red]")
            send_alert(f"❌ clipsys publish failed for {row['id'][:8]}: {exc}")
            skipped += 1

    console.print(f"[green]Publish done:[/green] published={published} skipped={skipped}")


@app.command()
def auth(account_id: str = typer.Argument(..., help="Account id from accounts.yaml, e.g. acct_01")) -> None:
    """Authorize a YouTube account in the browser and save its token."""
    from clipsys.config import load_accounts
    from clipsys.publish.youtube import authorize

    account = load_accounts().by_id(account_id)
    if account.platform != "youtube_shorts":
        console.print(f"[red]{account_id} is {account.platform}, not YouTube.[/red]")
        raise typer.Exit(1)
    console.print(f"Sign in as the channel for [bold]{account.handle}[/bold] in the browser window.")
    path = authorize(account.token_ref)
    console.print(f"[green]Token saved:[/green] {path}")


@app.command(name="run-all")
def run_all(
    live: bool = typer.Option(False, "--live", help="Enable live publishing"),
) -> None:
    """Run the full pipeline: ingest → caption, then publish per stage rules."""
    _ensure_db()

    if Path("data/KILL").exists():
        console.print("[red]KILL file present — aborting.[/red]")
        raise typer.Exit(1)

    _run_stages()

    publish(live=live)


def _run_stages() -> None:
    """Run ingest through caption (everything except publish)."""
    from clipsys.allocate import run_allocate
    from clipsys.caption import run_caption
    from clipsys.cut import cut_clips
    from clipsys.ingest import run_ingest
    from clipsys.render import run_render
    from clipsys.score import run_score
    from clipsys.transcribe import run_transcribe

    stages = [
        ("ingest", lambda: run_ingest()),
        ("transcribe", lambda: run_transcribe()),
        ("score", lambda: run_score()),
        ("cut", lambda: cut_clips()),
        ("allocate", lambda: run_allocate()),
        ("render", lambda: run_render()),
        ("caption", lambda: run_caption()),
    ]

    for name, fn in stages:
        console.print(f"[yellow]→ {name}[/yellow]")
        result = fn()
        console.print(f"  {result}")


@app.command()
def record(
    url: str = typer.Argument(..., help="Stream URL, e.g. https://www.twitch.tv/channel"),
    out: Path = typer.Option(Path("recordings"), help="Folder the chunks are written to"),
    quality: str = typer.Option("best", help="Stream quality, e.g. best, 1080p60, 720p"),
    segment_minutes: int = typer.Option(10, help="Length of each recorded chunk"),
) -> None:
    """Record a live stream into chunks. Run `clipsys watch` on the same folder to clip them."""
    from clipsys.record import record as run_record

    out = out.resolve()
    console.print(f"Recording [bold]{url}[/bold] into {out} in {segment_minutes}-minute chunks (Ctrl+C to stop)")
    console.print("If the channel is offline, this waits and checks every 60 seconds.")
    console.print(f'In a second window run: clipsys watch "{out}" --program <id> --creator <name>')
    try:
        code = run_record(url, out, quality, segment_minutes * 60)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print("[green]Recording finished.[/green]" if code == 0 else f"[red]Recorder exited with code {code}[/red]")
    raise typer.Exit(code)


@app.command()
def watch(
    folder: Path = typer.Argument(..., help="Folder where stream recordings appear"),
    program: str = typer.Option(..., help="Program id from programs.yaml that covers this footage"),
    creator: str = typer.Option(..., help="Creator name recorded with each source"),
    interval: int = typer.Option(30, help="Seconds between folder checks"),
    once: bool = typer.Option(False, "--once", help="Check twice, process what is finished, then exit"),
) -> None:
    """Watch a recordings folder; clip each finished file. Never publishes — review and publish as usual."""
    _ensure_db()
    import time

    from clipsys.config import load_programs
    from clipsys.watch import add_to_manifest, finished_files, manifest_files

    folder = folder.resolve()  # note: a relative path resolves from the project folder; pass a full path
    if not folder.is_dir():
        console.print(f"[red]Not a folder: {folder}[/red]")
        raise typer.Exit(1)
    # Same gate as ingest: no permission record, no clips.
    if program not in {p.id for p in load_programs().programs}:
        console.print(f"[red]Program '{program}' has no permission record in config/programs.yaml.[/red]")
        raise typer.Exit(1)

    console.print(f"Watching [bold]{folder}[/bold] every {interval}s (Ctrl+C to stop)")
    sizes: dict[Path, int] = {}
    checks = 0
    while True:
        new = finished_files(folder, sizes, manifest_files())
        if new:
            console.print(f"[green]Finished recordings:[/green] {[p.name for p in new]}")
            add_to_manifest(new, program, creator)
            _run_stages()
        checks += 1
        if once and checks >= 2:
            break
        time.sleep(interval)


@app.command()
def status() -> None:
    """Show counts by stage and status, quota used, failures."""
    _ensure_db()
    from clipsys.config import load_accounts
    from clipsys.db.db import get_conn
    from clipsys.publish.quota import DEFAULT_BUDGET, units_used_today

    console.print("\n[bold]Sources[/bold]")
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) as n FROM sources GROUP BY status").fetchall()
    for r in rows:
        console.print(f"  {r['status']}: {r['n']}")

    console.print("\n[bold]Variants[/bold]")
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) as n FROM variants GROUP BY status").fetchall()
    for r in rows:
        console.print(f"  {r['status']}: {r['n']}")

    console.print("\n[bold]Jobs[/bold]")
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) as n FROM jobs GROUP BY status").fetchall()
    for r in rows:
        console.print(f"  {r['status']}: {r['n']}")

    console.print("\n[bold]Quota today (YouTube)[/bold]")
    accts = load_accounts()
    for acct in [a for a in accts.accounts if a.platform == "youtube_shorts"]:
        used = units_used_today(acct.id)
        console.print(f"  {acct.id}: {used}/{DEFAULT_BUDGET}")

    with get_conn() as conn:
        dead = conn.execute("SELECT COUNT(*) as n FROM jobs WHERE status='dead'").fetchone()["n"]
    if dead:
        console.print(f"\n[red]Dead-letter jobs: {dead}[/red]")


if __name__ == "__main__":
    app()
