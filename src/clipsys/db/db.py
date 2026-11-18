"""SQLite connection and schema bootstrap."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from clipsys._env import settings

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()

# ALTER TABLE statements applied after CREATE TABLE so existing databases
# gain new columns without data loss.  Each statement is attempted and
# silently skipped when the column already exists.
_MIGRATIONS = [
    "ALTER TABLE variants ADD COLUMN telegram_sent_at TEXT",
    "ALTER TABLE variants ADD COLUMN telegram_status  TEXT",
]


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_variants_held_status(conn: sqlite3.Connection) -> None:
    """Add 'held' to variants.status CHECK constraint on existing databases.

    SQLite cannot ALTER a CHECK constraint, so we recreate the table.
    Skips when 'held' is already present (fresh install or prior migration).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='variants'"
    ).fetchone()
    if not row or "'held'" in row["sql"]:
        return

    # Disable FK checks for the duration of the table swap.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("""
        CREATE TABLE variants_v2 (
            id              TEXT PRIMARY KEY,
            clip_id         TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
            account_id      TEXT NOT NULL,
            file_path       TEXT,
            caption_json    TEXT,
            status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending','rendered','captioned','approved',
                                'rejected','published','error','held'
                            )),
            published_at    TEXT,
            platform_post_id TEXT,
            telegram_sent_at TEXT,
            telegram_status  TEXT,
            created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
    """)
    conn.execute("""
        INSERT INTO variants_v2
        SELECT id, clip_id, account_id, file_path, caption_json, status,
               published_at, platform_post_id, telegram_sent_at, telegram_status,
               created_at, updated_at
        FROM variants
    """)
    conn.execute("DROP TABLE variants")
    conn.execute("ALTER TABLE variants_v2 RENAME TO variants")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_variants_clip   ON variants(clip_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_variants_status ON variants(status)")
    conn.execute("PRAGMA foreign_keys=ON")


def init_db(db_path: Path | None = None) -> None:
    path = db_path or settings.db_path
    conn = _connect(path)
    conn.executescript(_SCHEMA)
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists on fresh or previously-migrated DB
    _migrate_variants_held_status(conn)
    conn.commit()
    conn.close()


@contextmanager
def get_conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    path = db_path or settings.db_path
    conn = _connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
