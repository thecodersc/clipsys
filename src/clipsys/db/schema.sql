-- clipsys SQLite schema
-- Run via: clipsys doctor (auto-applied) or python -c "from clipsys.db.db import init_db; init_db()"

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ─── Sources ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,          -- sha256 hex of file content
    file_path   TEXT NOT NULL,             -- absolute path under data/sources/
    program_id  TEXT NOT NULL,
    creator     TEXT NOT NULL,
    source_url  TEXT,                      -- optional YouTube video URL for comment density
    youtube_video_id TEXT,                 -- extracted from source_url when present
    duration_s  REAL NOT NULL,
    has_video   INTEGER NOT NULL DEFAULT 1,
    has_audio   INTEGER NOT NULL DEFAULT 1,
    received_at TEXT NOT NULL,             -- ISO-8601
    status      TEXT NOT NULL DEFAULT 'ready'
                CHECK(status IN ('ready','transcribed','scored','error')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ─── Transcripts ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    language    TEXT,
    segments_json TEXT NOT NULL,           -- JSON array of {start,end,text}
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ─── Moments (scored windows) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS moments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    t_start     REAL NOT NULL,             -- seconds
    t_end       REAL NOT NULL,
    score       REAL NOT NULL,
    signals_json TEXT NOT NULL,            -- {comment_density,loudness_peak,scene_density,keyword_hits}
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ─── Master Clips ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clips (
    id          TEXT PRIMARY KEY,          -- uuid4
    source_id   TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    moment_id   INTEGER REFERENCES moments(id),
    file_path   TEXT NOT NULL,
    t_start     REAL NOT NULL,
    t_end       REAL NOT NULL,
    duration_s  REAL NOT NULL,
    score       REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'cut'
                CHECK(status IN ('cut','allocated','error')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ─── Variants (per-account rendered short) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS variants (
    id              TEXT PRIMARY KEY,      -- uuid4
    clip_id         TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    account_id      TEXT NOT NULL,
    file_path       TEXT,
    caption_json    TEXT,                  -- {title,hook,description,hashtags}
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','rendered','captioned','approved','rejected','published','error','held')),
    published_at    TEXT,
    platform_post_id TEXT,
    telegram_sent_at TEXT,
    telegram_status  TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ─── Jobs queue ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,             -- ingest|transcribe|score|cut|render|caption|publish
    payload     TEXT NOT NULL,             -- JSON
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','running','done','failed','dead')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    run_after   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_error  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ─── Quota ledger ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS quota_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT NOT NULL,
    date        TEXT NOT NULL,             -- YYYY-MM-DD
    units_used  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS quota_account_date ON quota_ledger(account_id, date);

-- ─── Indexes ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_moments_source ON moments(source_id);
CREATE INDEX IF NOT EXISTS idx_moments_score  ON moments(score DESC);
CREATE INDEX IF NOT EXISTS idx_clips_source   ON clips(source_id);
CREATE INDEX IF NOT EXISTS idx_variants_clip  ON variants(clip_id);
CREATE INDEX IF NOT EXISTS idx_variants_status ON variants(status);
CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(status, run_after);
