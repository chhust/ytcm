import logging
logger = logging.getLogger(__name__)

import json
import os
from datetime import datetime, timezone
from dateutil.parser import isoparse

from ytcm import identity as ident
from ytcm import platform_utils as platform
from ytcm.platform_utils import plural
from ytcm import sanitize as sanitize
from ytcm.db import (check_fingerprint, create_indexes, drop_indexes, get_conn, get_meta,
                     record_provenance, set_meta)

VIDEO_COLUMNS = ("title", "description", "published_at", "channel_id", "channel_name",
                 "duration", "likes", "dislikes", "views", "download_time",
                 "manual_review", "comments_status")

MESSAGE_COLUMNS = ("text", "date", "likes", "author_channel_id", "author_name",
                   "author_subscribers", "language", "blob_sentiment", "vader_sentiment")

JSON_COLUMNS = frozenset({"author_subscribers", "blob_sentiment", "vader_sentiment",
                          "manual_review"})

COMMENT_ID_KEY = "youtube_comment_id"
REPLY_ID_KEY = "youtube_reply_id"
PARENT_ID_KEY = "youtube_parent_id"

MODES = ("fill-missing", "merge", "replace")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _encode(value):
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _decode(value):
    return None if value is None else json.loads(value)


def _pack(name, value):
    return _encode(value) if name in JSON_COLUMNS else value


def _unpack(name, value):
    return _decode(value) if name in JSON_COLUMNS else value


def _leftovers(record, known):
    return {k: v for k, v in record.items() if k not in known}


class MalformedCorpus(ValueError):
    pass


def looks_like_jsonl(path):

    with open(path, "r", encoding="utf-8") as handle:
        first = handle.readline().strip()
        if not first.startswith("{") or not first.endswith("}"):
            return False
        second = ""
        for line in handle:
            if line.strip():
                second = line.strip()
                break
    try:
        record = json.loads(first)
    except json.JSONDecodeError:
        return False
    if not second:
        return isinstance(record, dict) and "video_id" in record
    return second.startswith("{")


def read_corpus(path):

    if os.path.getsize(path) == 0:
        return iter(()), 0, True

    if looks_like_jsonl(path):
        with open(path, "r", encoding="utf-8") as handle:
            count = sum(1 for line in handle if line.strip())

        def pairs():
            with open(path, "r", encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as e:
                        raise MalformedCorpus(
                            f"{os.path.basename(path)}, line {number}: not valid JSON ({e}).") from e
                    if not isinstance(record, dict):
                        raise MalformedCorpus(
                            f"{os.path.basename(path)}, line {number}: expected one video "
                            f"per line, found {type(record).__name__}.")
                    if "video_id" in record:
                        entry = {k: v for k, v in record.items() if k != "video_id"}
                        yield record["video_id"], entry
                    elif len(record) == 1:
                        yield next(iter(record.items()))
                    else:
                        raise MalformedCorpus(
                            f"{os.path.basename(path)}, line {number}: a line needs a "
                            f"'video_id' field, or one video id as its only key.")

        return pairs(), count, True

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise MalformedCorpus(
            f"{os.path.basename(path)} is a {type(data).__name__}. A corpus is an "
            f"object keyed by video id.")
    return data.items(), len(data), False


def check_video_shape(video_id, entry, name="the file"):

    where = f"{name}, video '{video_id}'"
    if entry is None or not isinstance(entry, dict):
        raise MalformedCorpus(
            f"{where}: expected an object with 'video_info' and 'comments', "
            f"found {type(entry).__name__}.")

    info = entry.get("video_info")
    if info is not None and not isinstance(info, dict):
        raise MalformedCorpus(
            f"{where}: 'video_info' is a {type(info).__name__}, not an object.")

    comments = entry.get("comments")
    if comments is None:
        return True
    if not isinstance(comments, list):
        raise MalformedCorpus(
            f"{where}: 'comments' is a {type(comments).__name__}, not a list.")

    for position, comment in enumerate(comments):
        if not isinstance(comment, dict):
            raise MalformedCorpus(
                f"{where}, comment {position}: expected an object, found "
                f"{type(comment).__name__}.")
        replies = comment.get("replies")
        if replies is None:
            continue
        if not isinstance(replies, list):
            raise MalformedCorpus(
                f"{where}, comment {position}: 'replies' is a "
                f"{type(replies).__name__}, not a list.")
        for reply_position, reply in enumerate(replies):
            if not isinstance(reply, dict):
                raise MalformedCorpus(
                    f"{where}, comment {position}, reply {reply_position}: "
                    f"expected an object, found {type(reply).__name__}.")

    return False


def unreadable_date(value):

    if value is None or value == "":
        return False
    try:
        isoparse(str(value))
        return False
    except (ValueError, OverflowError, TypeError):
        return True


def datish_fields(video_id, info, comments):

    offenders = []
    if unreadable_date(info.get("published_at")):
        offenders.append((f"{video_id}.published_at", info.get("published_at")))
    for comment in comments:
        if unreadable_date(comment.get("date")):
            offenders.append((comment.get(COMMENT_ID_KEY) or video_id, comment.get("date")))
        for reply in comment.get("replies") or []:
            if unreadable_date(reply.get("date")):
                offenders.append((reply.get(REPLY_ID_KEY) or video_id, reply.get("date")))
    return offenders


def fingerprint_beside(path):

    note = path + ".fingerprint"
    if not os.path.isfile(note):
        return None
    try:
        with open(note, "r", encoding="utf-8") as handle:
            for line in handle:
                name, separator, value = line.partition(":")
                if separator and name.strip() == "key_fingerprint":
                    return value.strip() or None
    except OSError:
        return None
    return None


def import_json(path, corpus, mode="fill-missing", db_path=None, progress=None,
                sanitize_text=False, pseudonymize=False):

    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")

    started = _now()
    sidecar_key = fingerprint_beside(path)
    if sidecar_key and ident.have_key() and sidecar_key != ident.fingerprint():
        raise ValueError(
            f"'{os.path.basename(path)}' was exported under key {sidecar_key}. "
            f"The key currently in use is {ident.fingerprint()}.")

    videos, total_videos, streaming = read_corpus(path)
    missing_comment_lists = 0

    if (sanitize_text or pseudonymize) and not ident.have_key():
        raise ident.NoKey(
            "Sanitizing and pseudonymizing need a key. Run 'generatehash' "
            "first, or import without them.")

    conn = get_conn(db_path)
    if ident.have_key():
        check_fingerprint(conn, ident.fingerprint())
    else:
        stored_fingerprint = get_meta(conn, "key_fingerprint")
        if stored_fingerprint:
            conn.close()
            raise ident.NoKey(
                f"This corpus was pseudonymized with key {stored_fingerprint}."
                f"This key is missing here.")
    written = 0
    seen = set()
    duplicates = []
    unreadable_dates = []

    try:
        conn.execute("BEGIN IMMEDIATE")
        if mode == "replace":
            ids = [row[0] for row in conn.execute(
                "SELECT video_id FROM video_corpora WHERE corpus = ?", (corpus,))]
            only_here = [i for i in ids if conn.execute(
                "SELECT COUNT(*) FROM video_corpora WHERE video_id = ?", (i,)).fetchone()[0] == 1]
            conn.executemany("DELETE FROM messages WHERE video_id = ?", [(i,) for i in only_here])
            conn.executemany("DELETE FROM videos WHERE video_id = ?", [(i,) for i in only_here])
            conn.executemany("DELETE FROM video_annotations WHERE video_id = ?",
                             [(i,) for i in only_here])
            conn.execute("DELETE FROM video_corpora WHERE corpus = ?", (corpus,))
            conn.execute("DELETE FROM annotations WHERE message_id NOT IN "
                         "(SELECT message_id FROM messages)")

        drop_indexes(conn)

        for video_position, (video_id, entry) in enumerate(videos):
            if check_video_shape(video_id, entry, os.path.basename(path)):
                missing_comment_lists += 1
            info = entry.get("video_info", {}) or {}
            comments = entry.get("comments", []) or []
            unreadable_dates.extend(datish_fields(video_id, info, comments))
            if pseudonymize or sanitize_text:
                _pseudonymize_entry(info, comments, pseudonymize, sanitize_text)
            if sanitize_text:
                sanitize.prepare(conn, info, comments)
            written += _write_video(conn, video_id, corpus, info, video_position, mode)
            written += _write_messages(conn, video_id, comments, mode, seen, duplicates)
            if progress and video_position % 2000 == 0:
                progress(video_position, total_videos)

        conn.commit()
        create_indexes(conn)
        record_provenance(conn, f"import:{mode}", started, _now(), written, corpus=corpus,
                          parameters=json.dumps({"file": os.path.basename(path),
                                                 "duplicate_ids_collapsed": len(duplicates)}))
        if missing_comment_lists:
            print(f"Note: {missing_comment_lists:,} of {total_videos:,} "
                  f"{plural(total_videos, 'video')} had no comment list. Stored as "
                  f"videos without comments.")
        if unreadable_dates:
            where, value = unreadable_dates[0]
            logger.warning(f"{len(unreadable_dates):,} records in "
                           f"'{os.path.basename(path)}' do not carry a date; "
                           f"e.g.: {where} = {value!r}.")
            print(f"\nNote: {len(unreadable_dates):,} "
                  f"{plural(len(unreadable_dates), 'record')} carried a date that can't be "
                  f"read; e.g.: {where} = {value!r}. {plural(len(unreadable_dates), 'It is', 'They are')} "
                  f"stored as {plural(len(unreadable_dates), 'it', 'they')} arrived.")
        if duplicates:
            logger.warning(f"{len(duplicates):,} records in '{os.path.basename(path)}' repeat an "
                           f"id from the same file and were folded into the first under "
                           f"'{mode}'; example: {duplicates[0]}.")
            print(f"\nNote: {len(duplicates):,} {plural(len(duplicates), 'record')} "
                  f"repeated an id. Folded into the first under '{mode}': "
                  + ("only the fields it was missing were filled."
                     if mode == "fill-missing" else "the later values won."))
    finally:
        conn.close()

    return written, len(duplicates)


def _pseudonymize_entry(info, comments, pseudonymize, sanitize_text):

    if pseudonymize:
        for field in ("channel_id", "channel_name"):
            value = info.get(field)
            if value and not ident.is_token(value):
                if field == "channel_id":
                    ident.remember([value])
                info[field] = ident.token(value)
        for field in sanitize.IDENTITY_FIELDS:
            value = info.get(field)
            if isinstance(value, str) and value and not ident.is_token(value):
                info[field] = ident.token(value)

    for record in sanitize.texts_of(comments):
        name = record.get("author_name")
        identifier = record.get("author_channel_id")
        fresh = bool(pseudonymize and identifier and not ident.is_token(identifier))
        token = ident.token(identifier) if fresh else identifier
        if sanitize_text:
            sanitize.remember_name(name, token)
        if pseudonymize:
            if fresh:
                ident.remember([identifier])
                record["author_channel_id"] = token
            if name and not ident.is_token(name):
                record["author_name"] = ident.token(name)


def _write_video(conn, video_id, corpus, info, position, mode):
    conn.execute("INSERT OR REPLACE INTO video_corpora (video_id, corpus, position) "
                 "VALUES (?,?,?)", (video_id, corpus, position))

    columns = ("video_id",) + VIDEO_COLUMNS + ("position",)
    values = [video_id] + [_pack(column, info.get(column)) for column in VIDEO_COLUMNS] + [position]

    exists = conn.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if not exists:
        conn.execute(f"INSERT INTO videos ({','.join(columns)}) "
                     f"VALUES ({','.join('?' * len(columns))})", values)
    elif mode == "merge":
        assignments = ",".join(f"{column} = ?" for column in VIDEO_COLUMNS + ("position",))
        conn.execute(f"UPDATE videos SET {assignments} WHERE video_id = ?",
                     values[1:] + [video_id])
    else:
        assignments = ",".join(f"{column} = COALESCE({column}, ?)" for column in VIDEO_COLUMNS)
        conn.execute(f"UPDATE videos SET {assignments} WHERE video_id = ?",
                     [_pack(column, info.get(column)) for column in VIDEO_COLUMNS] + [video_id])

    extra = _leftovers(info, set(VIDEO_COLUMNS))
    if extra:
        _write_annotations(conn, "video_annotations", "video_id", video_id, extra, mode)
    return 1


def _write_messages(conn, video_id, comments, mode, seen, duplicates):

    written = 0
    for position, comment in enumerate(comments):
        message_id = comment.get(COMMENT_ID_KEY) or f"{video_id}:c{position}"
        repeated = message_id in seen
        if repeated:
            duplicates.append(message_id)
        seen.add(message_id)
        stored = _write_message(conn, message_id, video_id, None, "comment",
                                position, comment, {COMMENT_ID_KEY, "replies"}, mode)
        written += 0 if repeated else stored

        for reply_position, reply in enumerate(comment.get("replies", []) or []):
            reply_id = reply.get(REPLY_ID_KEY) or f"{message_id}:r{reply_position}"
            reply_repeated = reply_id in seen
            if reply_repeated:
                duplicates.append(reply_id)
            seen.add(reply_id)
            known = {REPLY_ID_KEY, PARENT_ID_KEY}
            stored = _write_message(conn, reply_id, video_id, message_id, "reply",
                                    reply_position, reply, known, mode)
            written += 0 if reply_repeated else stored
            stated_parent = reply.get(PARENT_ID_KEY)
            if stated_parent and stated_parent != message_id:
                _write_annotations(conn, "annotations", "message_id", reply_id,
                                   {PARENT_ID_KEY: stated_parent}, mode)
    return written


def _write_message(conn, message_id, video_id, parent_id, kind, position, record, skip, mode):
    columns = ("message_id", "video_id", "parent_id", "kind", "position") + MESSAGE_COLUMNS
    values = [message_id, video_id, parent_id, kind, position] + \
             [_pack(c, record.get(c)) for c in MESSAGE_COLUMNS]

    exists = conn.execute("SELECT 1 FROM messages WHERE message_id = ?", (message_id,)).fetchone()
    if not exists:
        conn.execute(f"INSERT INTO messages ({','.join(columns)}) "
                     f"VALUES ({','.join('?' * len(columns))})", values)
    elif mode == "merge":
        assignments = ",".join(f"{c} = ?" for c in columns[1:])
        conn.execute(f"UPDATE messages SET {assignments} WHERE message_id = ?",
                     values[1:] + [message_id])
    else:
        assignments = ",".join(f"{c} = COALESCE({c}, ?)" for c in MESSAGE_COLUMNS)
        conn.execute(f"UPDATE messages SET {assignments} WHERE message_id = ?",
                     [_pack(c, record.get(c)) for c in MESSAGE_COLUMNS] + [message_id])

    author = record.get("author_channel_id")
    if author:
        conn.execute("INSERT OR IGNORE INTO channels "
                     "(channel_id, name, subscribers, anon_token) VALUES (?,?,?,?)",
                     (author, record.get("author_name"),
                      record.get("author_subscribers"),
                      author if ident.is_token(author) else None))

    extra = _leftovers(record, set(MESSAGE_COLUMNS) | skip)
    if extra:
        _write_annotations(conn, "annotations", "message_id", message_id, extra, mode)
    return 1


def _write_annotations(conn, table, key_column, key, fields, mode):
    rows = [(key, dimension, _encode(value)) for dimension, value in fields.items()]
    if mode == "merge":
        conn.executemany(
            f"INSERT INTO {table} ({key_column}, dimension, value, source) "
            f"VALUES (?,?,?,'import') "
            f"ON CONFLICT({key_column}, dimension, source) DO UPDATE SET value = excluded.value",
            rows)
    else:
        conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({key_column}, dimension, value, source) "
            f"VALUES (?,?,?,'import')", rows)


def _video_rows(conn, corpus=None, years=None):
    columns = ("video_id",) + VIDEO_COLUMNS
    selected = ",".join(f"v.{c}" for c in columns)
    where, params = [], []
    join = ""
    order = "v.position, v.video_id"
    if corpus:
        join = "JOIN video_corpora vc ON vc.video_id = v.video_id"
        where.append("vc.corpus = ?")
        params.append(corpus)
        order = "vc.position, v.video_id"
    if years:
        where.append("v.published_at >= ? AND v.published_at < ?")
        params += [f"{years[0]}-01-01", f"{years[1] + 1}-01-01"]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(f"SELECT {selected} FROM videos v {join} {clause} "
                        f"ORDER BY {order}", params), columns


def _annotations_for(conn, table, key_column, keys):
    out = {}
    if not keys:
        return out
    step = 500
    keys = list(keys)
    for start in range(0, len(keys), step):
        chunk = keys[start:start + step]
        rows = conn.execute(
            f"SELECT {key_column}, dimension, value FROM {table} "
            f"WHERE {key_column} IN ({','.join('?' * len(chunk))})", chunk)
        for key, dimension, value in rows:
            out.setdefault(key, {})[dimension] = _decode(value)
    return out


def iter_videos(conn, corpus=None, years=None):

    rows, columns = _video_rows(conn, corpus, years)
    for row in rows:
        video_id, video_values = row[0], row[1:]
        extra = _annotations_for(conn, "video_annotations", "video_id",
                                 [video_id]).get(video_id, {})
        info = {}
        for name, value in zip(columns[1:], video_values):
            if value is not None:
                info[name] = _unpack(name, value)
        for name in sorted(extra):
            info[name] = extra[name]
        yield video_id, {"video_info": info, "comments": _comments_for(conn, video_id)}


def store_video(conn, video_id, corpus, info, comments, position):

    sanitize.prepare(conn, info, comments)
    _write_video(conn, video_id, corpus, info, position, "merge")
    _write_messages(conn, video_id, comments, "merge", set(), [])
    conn.commit()


def renumber_positions(conn, corpus):

    rows = conn.execute(
        "SELECT vc.video_id, v.published_at FROM video_corpora vc "
        "JOIN videos v ON v.video_id = vc.video_id WHERE vc.corpus = ? "
        "ORDER BY vc.position, vc.video_id", (corpus,)).fetchall()
    dated = sorted((r for r in rows if r[1]), key=lambda r: r[1])
    undated = [r for r in rows if not r[1]]
    ordered = list(enumerate(dated + undated))
    conn.executemany("UPDATE video_corpora SET position = ? WHERE video_id = ? AND corpus = ?",
                     [(i, r[0], corpus) for i, r in ordered])
    conn.executemany("UPDATE videos SET position = ? WHERE video_id = ?",
                     [(i, r[0]) for i, r in ordered])
    conn.commit()


def export_json(path, corpus=None, years=None, jsonl=False, db_path=None, progress=None):

    conn = get_conn(db_path)
    if ident.have_key():
        check_fingerprint(conn, ident.fingerprint())
    written = 0

    try:
        with open(path, "w", encoding="utf-8") as handle:
            if not jsonl:
                handle.write("{\n")
            first = True

            for video_id, entry in iter_videos(conn, corpus, years):
                if jsonl:
                    handle.write(json.dumps({"video_id": video_id, **entry},
                                            ensure_ascii=False) + "\n")
                else:
                    if not first:
                        handle.write(",\n")
                    handle.write(f"  {json.dumps(video_id, ensure_ascii=False)}: ")
                    handle.write(json.dumps(entry, ensure_ascii=False, indent=2))
                first = False
                written += 1
                if progress and written % 2000 == 0:
                    progress(written, None)

            if not jsonl:
                handle.write("\n}\n")

        stored = conn.execute("SELECT value FROM meta WHERE key = 'key_fingerprint'").fetchone()
        if stored:
            with open(path + ".fingerprint", "w", encoding="utf-8") as note:
                note.write(f"key_fingerprint: {stored[0]}\n"
                           f"videos: {written}\n"
                           f"Tokens in this export only join corpora made with the same key.\n")
    finally:
        conn.close()

    return written


def export_txt(folder, corpus=None, years=None, db_path=None, progress=None):
    from ytcm.export_utils import without_control_characters

    conn = get_conn(db_path)
    if ident.have_key():
        check_fingerprint(conn, ident.fingerprint())
    os.makedirs(folder, exist_ok=True)
    written = 0

    try:
        for video_id, entry in iter_videos(conn, corpus, years):
            info = entry.get("video_info", {}) or {}
            lines = [f"video_id: {video_id}"]
            for name in VIDEO_COLUMNS:
                value = info.get(name)
                if value is not None and value != "":
                    lines.append(f"{name}: {value}")
            for name in sorted(k for k in info if k not in VIDEO_COLUMNS):
                value = info.get(name)
                if value is not None and value != "":
                    lines.append(f"{name}: {value}")
            lines.append("")

            for comment in entry.get("comments", []) or []:
                lines.append("-" * 72)
                lines.append(f"[comment] {comment.get('author_name', '')} "
                             f"{comment.get('date', '')} "
                             f"({comment.get('youtube_comment_id', '')})")
                lines.append(str(comment.get("text", "")))
                for reply in comment.get("replies", []) or []:
                    lines.append("")
                    lines.append(f"    [reply] {reply.get('author_name', '')} "
                                 f"{reply.get('date', '')} "
                                 f"({reply.get('youtube_reply_id', '')})")
                    for text_line in str(reply.get("text", "")).split("\n"):
                        lines.append("    " + text_line)
                lines.append("")

            name = platform.safe_filename(f"{video_id}.txt")
            readable = without_control_characters("\n".join(lines))
            with open(os.path.join(folder, name), "w", encoding="utf-8") as handle:
                handle.write(readable.rstrip() + "\n")

            written += 1
            if progress and written % 2000 == 0:
                progress(written, None)
    finally:
        conn.close()

    return written


def _comments_for(conn, video_id):
    rows = list(conn.execute(
        f"SELECT message_id, parent_id, kind, {','.join(MESSAGE_COLUMNS)} "
        f"FROM messages WHERE video_id = ? ORDER BY kind, position, message_id",
        (video_id,)))
    if not rows:
        return []

    annotations = _annotations_for(conn, "annotations", "message_id",
                                   [r[0] for r in rows])
    comments, by_id = [], {}
    orphans = 0

    for row in rows:
        message_id, parent_id, kind, values = row[0], row[1], row[2], row[3:]
        extra = dict(annotations.get(message_id, {}))

        if kind == "comment":
            payload = {COMMENT_ID_KEY: message_id}
            for name, value in zip(MESSAGE_COLUMNS, values):
                if value is not None:
                    payload[name] = _unpack(name, value)
            payload["replies"] = []
            for name in sorted(extra):
                payload[name] = extra[name]
            by_id[message_id] = payload
            comments.append(payload)
        else:
            stated_parent = extra.pop(PARENT_ID_KEY, parent_id)
            payload = {REPLY_ID_KEY: message_id, PARENT_ID_KEY: stated_parent}
            for name, value in zip(MESSAGE_COLUMNS, values):
                if value is not None:
                    payload[name] = _unpack(name, value)
            for name in sorted(extra):
                payload[name] = extra[name]
            parent = by_id.get(parent_id)
            if parent is not None:
                parent["replies"].append(payload)
            else:
                orphans += 1

    if orphans:
        logger.warning(f"{orphans:,} replies under video {video_id} name a parent that is "
                       f"not in the corpus and were left out of the export.")
    return comments


def migrate_legacy_map(path, db_path=None):

    if not ident.have_key():
        raise ident.NoKey("No pseudonymization key. Run 'generatehash' first.")

    with open(path, "r", encoding="utf-8") as handle:
        mapping = json.load(handle)
    mapping = {real: token for real, token in mapping.items() if isinstance(token, str)}
    if not mapping:
        raise ValueError(f"'{path}' holds no real-id to token pairs.")

    conn = get_conn(db_path)
    try:
        check_fingerprint(conn, ident.fingerprint())
        conn.execute("ALTER TABLE channels ADD COLUMN legacy_token TEXT"
                     ) if "legacy_token" not in [
            row[1] for row in conn.execute("PRAGMA table_info(channels)")] else None
        rows = [(ident.token(real), legacy, legacy) for real, legacy in mapping.items()]
        conn.executemany(
            "INSERT INTO channels (channel_id, anon_token, legacy_token) VALUES (?,?,?) "
            "ON CONFLICT(channel_id) DO UPDATE SET anon_token = excluded.anon_token, "
            "legacy_token = excluded.legacy_token",
            [(legacy, token, legacy) for token, legacy, _ in rows])
        conn.commit()
        set_meta(conn, "legacy_scheme", "UserN")
        record_provenance(conn, "migrate:legacy-map", _now(), _now(), len(rows),
                          parameters=json.dumps({"file": os.path.basename(path)}))
    finally:
        conn.close()

    ident.remember(mapping.keys())
    return len(mapping)
