import hashlib
import re
import sqlite3

from ytcm.config import DB_PATH

DDL = """
CREATE TABLE IF NOT EXISTS videos (
    video_id        TEXT PRIMARY KEY,
    title           TEXT,
    description     TEXT,
    published_at    TEXT,
    channel_id      TEXT,
    channel_name    TEXT,
    duration        TEXT,
    likes           INTEGER,
    dislikes        INTEGER,
    views           INTEGER,
    download_time   TEXT,
    manual_review   TEXT,
    comments_status TEXT,
    position        INTEGER
);

CREATE TABLE IF NOT EXISTS video_corpora (
    video_id TEXT NOT NULL,
    corpus   TEXT NOT NULL,
    position INTEGER,
    PRIMARY KEY (video_id, corpus)
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id  TEXT PRIMARY KEY,
    name        TEXT,
    subscribers INTEGER,
    anon_token  TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id          TEXT PRIMARY KEY,
    video_id            TEXT NOT NULL,
    parent_id           TEXT,
    kind                TEXT NOT NULL,
    position            INTEGER,
    text                TEXT,
    date                TEXT,
    likes               INTEGER,
    author_channel_id   TEXT,
    author_name         TEXT,
    author_subscribers  TEXT,
    language            TEXT,
    language_source     TEXT,
    blob_sentiment      TEXT,
    blob_sentiment_source TEXT,
    vader_sentiment     TEXT,
    vader_sentiment_source TEXT
);

CREATE TABLE IF NOT EXISTS annotations (
    message_id TEXT NOT NULL,
    dimension  TEXT NOT NULL,
    value      TEXT,
    source     TEXT NOT NULL DEFAULT 'import',
    confidence REAL,
    model      TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (message_id, dimension, source)
);

CREATE TABLE IF NOT EXISTS video_annotations (
    video_id   TEXT NOT NULL,
    dimension  TEXT NOT NULL,
    value      TEXT,
    source     TEXT NOT NULL DEFAULT 'import',
    confidence REAL,
    model      TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (video_id, dimension, source)
);

CREATE TABLE IF NOT EXISTS download_state (
    video_id   TEXT PRIMARY KEY,
    corpus     TEXT,
    status     TEXT DEFAULT 'pending',
    attempts   INTEGER DEFAULT 0,
    last_error TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS mention_map (
    name_hash    TEXT NOT NULL,
    person_token TEXT NOT NULL,
    first_seen   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (name_hash, person_token)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS embeddings (
    message_id TEXT NOT NULL,
    model      TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    text_hash  TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (message_id, model)
);

CREATE TABLE IF NOT EXISTS embedding_state (
    model       TEXT PRIMARY KEY,
    provider    TEXT,
    dimensions  INTEGER,
    records     INTEGER DEFAULT 0,
    started_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS provenance (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pass            TEXT NOT NULL,
    tool_version    TEXT,
    provider        TEXT,
    model           TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    records_written INTEGER,
    corpus          TEXT,
    parameters      TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_vcorpora_corpus  ON video_corpora(corpus, position);
CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published_at);
CREATE INDEX IF NOT EXISTS idx_msg_video        ON messages(video_id, position);
CREATE INDEX IF NOT EXISTS idx_msg_parent       ON messages(parent_id, position);
CREATE INDEX IF NOT EXISTS idx_msg_author       ON messages(author_channel_id);
CREATE INDEX IF NOT EXISTS idx_msg_date         ON messages(date);
CREATE INDEX IF NOT EXISTS idx_msg_language     ON messages(language);
CREATE INDEX IF NOT EXISTS idx_ann_dimension    ON annotations(dimension, value);
CREATE INDEX IF NOT EXISTS idx_vann_dimension   ON video_annotations(dimension, value);
CREATE INDEX IF NOT EXISTS idx_state_status     ON download_state(status);
CREATE INDEX IF NOT EXISTS idx_emb_model        ON embeddings(model);
CREATE INDEX IF NOT EXISTS idx_mention_person   ON mention_map(person_token);
"""

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA cache_size = -262144",
    "PRAGMA mmap_size = 1073741824",
    "PRAGMA temp_store = MEMORY",
)


class NotADatabase(sqlite3.DatabaseError):
    pass


class ReadOnlyDatabase(sqlite3.DatabaseError):
    pass


def text_hash(text):
    return hashlib.blake2s((text or "").encode("utf-8"), digest_size=8).hexdigest()


def fold(text):
    return text.casefold() if isinstance(text, str) else text


def get_conn(path=None):

    path = path or DB_PATH
    conn = sqlite3.connect(path)
    try:
        for pragma in PRAGMAS:
            conn.execute(pragma)
        conn.executescript(DDL)
        conn.create_function("PYHASH", 1, text_hash, deterministic=True)
        conn.create_function("PYFOLD", 1, fold, deterministic=True)
        if "text_hash" not in {row[1] for row in conn.execute("PRAGMA table_info(embeddings)")}:
            conn.execute("ALTER TABLE embeddings ADD COLUMN text_hash TEXT")
            conn.execute("UPDATE embeddings SET text_hash = "
                         "(SELECT PYHASH(m.text) FROM messages m "
                         "WHERE m.message_id = embeddings.message_id) "
                         "WHERE EXISTS (SELECT 1 FROM messages m "
                         "WHERE m.message_id = embeddings.message_id)")
        conn.commit()
    except sqlite3.DatabaseError as e:
        conn.close()
        if getattr(e, "sqlite_errorname", "").startswith("SQLITE_READONLY"):
            raise ReadOnlyDatabase(
                f"Cannot write to '{path}'. The file or its folder is read-only. "
                f"Move the corpus somewhere else.") from e
        raise NotADatabase(
            f"'{path}' is not a usable database ({e}). Restore it from a copy, "
            f"or make a new one.") from e
    return conn


def create_indexes(conn):

    conn.executescript(INDEXES)
    conn.commit()


def missing_indexes(conn):

    expected = set(re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", INDEXES))
    present = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    return sorted(expected - present)


def drop_indexes(conn):

    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")]
    for name in names:
        conn.execute(f"DROP INDEX IF EXISTS {name}")


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()


def check_fingerprint(conn, current):

    stored = get_meta(conn, "key_fingerprint")
    if stored is None:
        set_meta(conn, "key_fingerprint", current)
        return current
    if stored != current:
        raise ValueError(
            f"This corpus was pseudonymized with key {stored}, but the key in use "
            f"is {current}. Use the original key, or start a separate database.")
    return stored


def mark_pending(conn, video_ids, corpus):

    conn.executemany(
        "INSERT INTO download_state (video_id, corpus, status, updated_at) "
        "VALUES (?,?,'pending',datetime('now')) "
        "ON CONFLICT(video_id) DO UPDATE SET corpus = excluded.corpus",
        [(v, corpus) for v in video_ids])
    conn.commit()


def requeue(conn, video_ids, corpus):

    conn.executemany(
        "INSERT INTO download_state (video_id, corpus, status, updated_at) "
        "VALUES (?,?,'pending',datetime('now')) "
        "ON CONFLICT(video_id) DO UPDATE SET status = 'pending', "
        "corpus = excluded.corpus, "
        "updated_at = excluded.updated_at", [(v, corpus) for v in video_ids])
    conn.commit()
    return len(video_ids)


def mark_status(conn, video_id, status, error=None):

    from ytcm import sanitize
    error = sanitize.scrub(error)
    conn.execute(
        "INSERT INTO download_state (video_id, status, attempts, last_error, updated_at) "
        "VALUES (?,?,1,?,datetime('now')) "
        "ON CONFLICT(video_id) DO UPDATE SET status = excluded.status, "
        "attempts = download_state.attempts + 1, last_error = excluded.last_error, "
        "updated_at = excluded.updated_at", (video_id, status, error))
    conn.commit()


def pending_videos(conn, corpus=None):

    if corpus:
        rows = conn.execute("SELECT video_id FROM download_state WHERE status = 'pending' "
                            "AND corpus = ? ORDER BY rowid", (corpus,))
    else:
        rows = conn.execute("SELECT video_id FROM download_state WHERE status = 'pending' "
                            "ORDER BY rowid")
    return [row[0] for row in rows]


def record_provenance(conn, pass_name, started_at, finished_at, records_written,
                      corpus=None, provider=None, model=None, parameters=None):

    from ytcm.config import VERSION
    cursor = conn.execute(
        "INSERT INTO provenance (pass, tool_version, provider, model, started_at, "
        "finished_at, records_written, corpus, parameters) VALUES (?,?,?,?,?,?,?,?,?)",
        (pass_name, VERSION, provider, model, started_at, finished_at,
         records_written, corpus, parameters))
    conn.commit()
    return cursor.lastrowid
