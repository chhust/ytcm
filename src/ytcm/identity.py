import logging
logger = logging.getLogger(__name__)

import hmac
import os
import secrets
import sqlite3
from hashlib import sha256

from ytcm import config
from ytcm import platform_utils as platform

TOKEN_PREFIX = "U"
TOKEN_LENGTH = 20
LOOKUP_FILE = "identities.db"

_key = None

LOOKUP_DDL = """
CREATE TABLE IF NOT EXISTS identities (
    token      TEXT NOT NULL,
    value      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'channel',
    first_seen TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (token, kind, value)
);

CREATE INDEX IF NOT EXISTS idx_identities_kind ON identities(kind);

CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS consultations (
    consulted_at TEXT DEFAULT (datetime('now')),
    token        TEXT,
    direction    TEXT
);
"""


class NoKey(Exception):
    pass


def key_path():
    return config.HASH_KEY_FILE


def have_key():
    return os.path.isfile(key_path())


def generate_key(path=None, replace=False):

    path = path or key_path()
    if os.path.exists(path) and not replace:
        raise NoKey(
            f"A key already exists at '{path}'. Replacing it gives every channel "
            f"a different pseudonym. Pass replace=True, or use 'deletehash' first.")
    key = secrets.token_bytes(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
    os.chmod(path, 0o600)
    global _key
    _key = key
    return fingerprint()


def delete_key(path=None):
    path = path or key_path()
    global _key
    _key = None
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def load_key(path=None):
    global _key
    if _key is None:
        path = path or key_path()
        if not os.path.exists(path):
            raise NoKey(
                f"No pseudonymization key at '{path}'. Run 'generatehash' first.")
        if not os.path.isfile(path):
            raise NoKey(f"'{path}' is not a file, so it cannot hold the key.")
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as e:
            raise NoKey(f"Could not read the key at '{path}': {e.strerror or e}.") from e
        if len(raw) < 16:
            raise NoKey(f"The key at '{path}' is too short to be one.")
        _key = raw
    return _key


def forget_key():
    global _key
    _key = None


def token(value, path=None):

    if not value:
        return value
    digest = hmac.new(load_key(path), str(value).encode("utf-8"), sha256).hexdigest()
    return TOKEN_PREFIX + digest[:TOKEN_LENGTH]


def fingerprint(path=None):

    return hmac.new(load_key(path), b"fingerprint", sha256).hexdigest()[:12]


def is_token(value):
    value = str(value or "")
    return (len(value) == len(TOKEN_PREFIX) + TOKEN_LENGTH
            and value.startswith(TOKEN_PREFIX)
            and all(c in "0123456789abcdef" for c in value[len(TOKEN_PREFIX):]))



def lookup_path():
    directory = config.LOOKUP_DIR
    return os.path.join(directory, LOOKUP_FILE) if directory else None


def lookup_available():
    path = lookup_path()
    return bool(path) and os.path.exists(path)


def open_lookup(create=False):

    path = lookup_path()
    if not path:
        return None
    if not os.path.exists(path) and not create:
        return None
    directory = os.path.dirname(path)
    if create and directory and not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with open(os.path.join(directory, "README.txt"), "w", encoding="utf-8") as handle:
            handle.write(
                "This directory maps pseudonyms back to YouTube channel ids.\n"
                "It is the additional information that makes the corpus pseudonymous\n"
                "instead of anonymous. Keep it secret, keep it safe!\n"
                "'deletelookup' removes it and leaves every analysis working.\n")
    if not os.path.exists(path):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    connection = sqlite3.connect(path)
    connection.executescript(LOOKUP_DDL)
    connection.commit()
    os.chmod(path, 0o600)
    _check_store_key(connection, path)
    return connection


def _check_store_key(connection, path):

    current = fingerprint()
    row = connection.execute(
        "SELECT value FROM store_meta WHERE key = 'key_fingerprint'").fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO store_meta (key, value) VALUES ('key_fingerprint', ?)", (current,))
        connection.commit()
        return
    if row[0] != current:
        connection.close()
        raise NoKey(
            f"The store at '{path}' was written with key {row[0]}, but the key "
            f"in use is {current}. Point LOOKUP_DIR elsewhere, or use the original key.")


def remember(values, kind="channel"):

    connection = open_lookup(create=True)
    if connection is None:
        return 0
    try:
        rows = [(token(v), str(v), kind) for v in dict.fromkeys(values) if v]
        return _write(connection, rows)
    finally:
        connection.close()


def remember_as(pairs, kind):

    connection = open_lookup(create=True)
    if connection is None:
        return 0
    try:
        rows = [(t, str(v), kind) for t, v in pairs if t and v]
        return _write(connection, rows)
    finally:
        connection.close()


def _write(connection, rows):
    if rows:
        connection.executemany(
            "INSERT OR IGNORE INTO identities (token, value, kind) VALUES (?,?,?)", rows)
        connection.commit()
    return len(rows)


def resolve(value, kind=None):

    connection = open_lookup()
    if connection is None:
        return None
    try:
        if kind:
            rows = connection.execute(
                "SELECT value, kind FROM identities WHERE token = ? AND kind = ?",
                (value, kind)).fetchall()
        else:
            rows = connection.execute(
                "SELECT value, kind FROM identities WHERE token = ?", (value,)).fetchall()
        connection.execute("INSERT INTO consultations (token, direction) VALUES (?, 'forward')",
                           (value,))
        connection.commit()
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0][0]
        grouped = {}
        for found, found_kind in rows:
            grouped.setdefault(found_kind, []).append(found)
        return {k: sorted(v) for k, v in grouped.items()}
    finally:
        connection.close()


def delete_lookup():

    path = lookup_path()
    if not path:
        return False
    gone, blocked = platform.remove_files(
        [path, path + "-journal", path + "-wal", path + "-shm"])
    for candidate in blocked:
        logger.error(f"Could not delete '{candidate}': it is still open. "
                     f"Close the shell and try again.")
    return path in gone
