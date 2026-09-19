import logging
logger = logging.getLogger(__name__)

import json
import re
from collections import Counter, defaultdict

import pandas as pd

from ytcm.bridge import JSON_COLUMNS, MESSAGE_COLUMNS, VIDEO_COLUMNS, iter_videos
from ytcm.config import TUBESCOPE_MODE
from ytcm.db import fold, get_conn

CORE_COLUMNS = ("message_id", "video_id", "parent_id", "kind") + MESSAGE_COLUMNS


VALID_MODES = ("c", "r", "cr")


def resolve_mode(mode=None):

    mode = str(mode or TUBESCOPE_MODE or "cr").strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown mode {mode!r}. TUBESCOPE_MODE and every mode "
                         f"argument must be one of: {', '.join(VALID_MODES)}.")
    return mode


def _kinds(mode):
    mode = resolve_mode(mode)
    return [k for k, letter in (("comment", "c"), ("reply", "r")) if letter in mode]


GROUPABLE_COLUMNS = frozenset(MESSAGE_COLUMNS) | {
    "message_id", "video_id", "parent_id", "kind", "position"}


def filters(mode=None, corpus=None, years=None, language=None, video_id=None):
    joins, where, params = [], [], []

    kinds = _kinds(mode)
    if len(kinds) == 1:
        where.append("m.kind = ?")
        params.append(kinds[0])

    if corpus:
        joins.append("JOIN video_corpora vc ON vc.video_id = m.video_id")
        where.append("vc.corpus = ?")
        params.append(corpus)

    if years:
        joins.append("JOIN videos v ON v.video_id = m.video_id")
        where.append("v.published_at >= ? AND v.published_at < ?")
        params += [f"{years[0]}-01-01", f"{years[1] + 1}-01-01"]

    if language:
        where.append("m.language = ?")
        params.append(language)

    if video_id:
        where.append("m.video_id = ?")
        params.append(video_id)

    return " ".join(dict.fromkeys(joins)), (f"WHERE {' AND '.join(where)}" if where else ""), params


def _from_json(value):
    return json.loads(value) if isinstance(value, str) else None


def _decode_frame(frame):
    for column in frame.columns:
        if column in JSON_COLUMNS:
            frame[column] = frame[column].map(_from_json)
    return frame


def records_df(mode=None, corpus=None, years=None, language=None, fields=None,
               limit=None, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        joins, where, params = filters(mode, corpus, years, language)
        selected = ",".join(f"m.{c}" for c in CORE_COLUMNS)
        sql = (f"SELECT {selected} FROM messages m {joins} {where} "
               f"ORDER BY m.video_id, m.kind, m.position, m.message_id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        frame = _decode_frame(pd.read_sql_query(sql, conn, params=params))

        for name in fields or ():
            lookup = pd.read_sql_query(
                "SELECT message_id, value FROM annotations WHERE dimension = ? "
                "ORDER BY (source = 'import'), source",
                conn, params=[name]).drop_duplicates(
                    subset="message_id", keep="first").set_index("message_id")["value"]
            frame[name] = frame["message_id"].map(lookup).map(_from_json)
        return frame
    finally:
        if close:
            conn.close()


def videos_df(corpus=None, years=None, fields=None, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        columns = ("video_id",) + VIDEO_COLUMNS
        selected = ",".join(f"v.{c}" for c in columns)
        join, where, params = "", "", []
        order = "v.position"
        if corpus:
            join = "JOIN video_corpora vc ON vc.video_id = v.video_id"
            where = "WHERE vc.corpus = ?"
            params = [corpus]
            order = "vc.position"
        if years:
            where = (where + " AND " if where else "WHERE ") + \
                    "v.published_at >= ? AND v.published_at < ?"
            params += [f"{years[0]}-01-01", f"{years[1] + 1}-01-01"]
        frame = _decode_frame(pd.read_sql_query(
            f"SELECT {selected} FROM videos v {join} {where} ORDER BY {order}", conn, params=params))

        for name in fields or ():
            lookup = pd.read_sql_query(
                "SELECT video_id, value FROM video_annotations WHERE dimension = ? "
                "ORDER BY (source = 'import'), source",
                conn, params=[name]).drop_duplicates(
                    subset="video_id", keep="first").set_index("video_id")["value"]
            frame[name] = frame["video_id"].map(lookup).map(_from_json)
        return frame
    finally:
        if close:
            conn.close()


def video_stats_df(corpus=None, years=None, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        join, where, params = "", "", []
        order = "v.position"
        if corpus:
            join = "JOIN video_corpora vc ON vc.video_id = v.video_id"
            where = "WHERE vc.corpus = ?"
            params = [corpus]
            order = "vc.position"
        if years:
            where = (where + " AND " if where else "WHERE ") + \
                    "v.published_at >= ? AND v.published_at < ?"
            params += [f"{years[0]}-01-01", f"{years[1] + 1}-01-01"]

        sql = f"""
            SELECT v.video_id, v.title, v.published_at, v.duration, v.likes, v.views,
                   v.channel_id, v.channel_name,
                   COALESCE(SUM(CASE WHEN m.kind = 'comment' THEN 1 ELSE 0 END), 0) AS n_comments,
                   COALESCE(SUM(CASE WHEN m.kind = 'reply' THEN 1 ELSE 0 END), 0) AS n_replies,
                   COUNT(DISTINCT CASE WHEN m.kind = 'reply' THEN m.parent_id END)
                       AS comments_with_replies
            FROM videos v {join}
            LEFT JOIN messages m ON m.video_id = v.video_id
            {where}
            GROUP BY v.video_id
            ORDER BY {order}"""
        return _decode_frame(pd.read_sql_query(sql, conn, params=params))
    finally:
        if close:
            conn.close()


def iter_texts(mode=None, corpus=None, years=None, language=None, batch=20000, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        joins, where, params = filters(mode, corpus, years, language)
        clause = where or "WHERE 1=1"
        last = ""
        while True:
            rows = conn.execute(
                f"SELECT m.message_id, m.text FROM messages m {joins} {clause} "
                f"AND m.message_id > ? ORDER BY m.message_id LIMIT {int(batch)}",
                params + [last]).fetchall()
            if not rows:
                return
            for message_id, text in rows:
                yield message_id, text
            last = rows[-1][0]
    finally:
        if close:
            conn.close()


def participation_events(corpus=None, years=None, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        joins, where, params = filters("cr", corpus, years)
        clause = where or "WHERE 1=1"
        events = list(conn.execute(
            f"SELECT m.date, m.author_channel_id FROM messages m {joins} {clause} "
            f"AND m.date IS NOT NULL AND m.date != '' "
            f"AND m.author_channel_id IS NOT NULL AND m.author_channel_id != ''", params))

        vjoin, vwhere, vparams = "", "", []
        if corpus:
            vjoin = "JOIN video_corpora vc ON vc.video_id = v.video_id"
            vwhere = "WHERE vc.corpus = ?"
            vparams = [corpus]
        if years:
            vwhere = (vwhere + " AND " if vwhere else "WHERE ") + \
                     "v.published_at >= ? AND v.published_at < ?"
            vparams += [f"{years[0]}-01-01", f"{years[1] + 1}-01-01"]
        uploads = list(conn.execute(
            f"SELECT v.published_at, v.channel_id FROM videos v {vjoin} {vwhere}", vparams))
        return [(d, a) for d, a in uploads if d and a] + events
    finally:
        if close:
            conn.close()


def dates_by_kind(corpus=None, years=None, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        joins, where, params = filters("cr", corpus, years)
        clause = where or "WHERE 1=1"
        comments, replies = [], []
        for kind, date in conn.execute(
                f"SELECT m.kind, m.date FROM messages m {joins} {clause} "
                f"AND m.date IS NOT NULL AND m.date != ''", params):
            (comments if kind == "comment" else replies).append(date)
        stats = video_stats_df(corpus, years, conn=conn)
        uploads = [d for d in stats["published_at"].tolist() if d]
        return uploads, comments, replies
    finally:
        if close:
            conn.close()


def count_by(column, mode=None, corpus=None, years=None, language=None, conn=None):

    if column not in GROUPABLE_COLUMNS:
        raise ValueError(f"'{column}' is not a column of messages. "
                         f"One of: {', '.join(sorted(GROUPABLE_COLUMNS))}.")

    close = conn is None
    conn = conn or get_conn()
    try:
        joins, where, params = filters(mode, corpus, years, language)
        rows = conn.execute(f"SELECT m.{column}, COUNT(*) FROM messages m {joins} {where} "
                            f"GROUP BY m.{column} ORDER BY COUNT(*) DESC", params)
        return pd.Series({k: v for k, v in rows}, dtype="int64")
    finally:
        if close:
            conn.close()


def count_over_time(unit="month", mode=None, corpus=None, years=None, language=None, conn=None):

    width = {"day": 10, "month": 7, "year": 4}[unit]
    close = conn is None
    conn = conn or get_conn()
    try:
        joins, where, params = filters(mode, corpus, years, language)
        clause = "WHERE m.date IS NOT NULL AND m.date != ''" if not where else \
                 f"{where} AND m.date IS NOT NULL AND m.date != ''"
        rows = conn.execute(f"SELECT SUBSTR(m.date, 1, {width}) AS bucket, COUNT(*) "
                            f"FROM messages m {joins} {clause} GROUP BY bucket ORDER BY bucket", params)
        return pd.Series({k: v for k, v in rows}, dtype="int64")
    finally:
        if close:
            conn.close()


EFFECTIVE_LANGUAGE = ("COALESCE(NULLIF(json_extract(a.value, '$'), 'unknown'), "
                      "NULLIF(m.language, 'unknown'))")

LANGUAGE_JOIN = ("LEFT JOIN annotations a ON a.rowid = ("
                 "SELECT one.rowid FROM annotations one "
                 "WHERE one.message_id = m.message_id AND one.dimension = 'llm_language' "
                 "ORDER BY (one.source = 'import'), one.source LIMIT 1)")


def effective_language_counts(mode=None, corpus=None, years=None, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        joins, where, params = filters(mode, corpus, years)
        rows = conn.execute(
            f"SELECT {EFFECTIVE_LANGUAGE} AS lang, COUNT(*) FROM messages m "
            f"{joins} {LANGUAGE_JOIN} {where} GROUP BY lang", params)
        return Counter({k: v for k, v in rows if k})
    finally:
        if close:
            conn.close()


def replies_per_comment(corpus=None, years=None, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        joins, where, params = filters("c", corpus, years)
        rows = conn.execute(
            f"SELECT m.message_id, (SELECT COUNT(*) FROM messages r "
            f"WHERE r.parent_id = m.message_id) FROM messages m {joins} {where}", params)
        return [count for _, count in rows]
    finally:
        if close:
            conn.close()



def filter_messages(terms, mode="or", corpus=None, years=None, conn=None, words=False):

    if isinstance(terms, str):
        terms = [terms]
    terms = [t.casefold() for t in terms if t]
    if not terms:
        return pd.DataFrame(columns=list(CORE_COLUMNS))

    joiner = " OR " if mode == "or" else " AND "
    if words:
        tests = joiner.join(["ytcm_word_match(m.text, ?) = 1"] * len(terms))
    else:
        tests = joiner.join(["INSTR(PYFOLD(m.text), ?) > 0"] * len(terms))

    close = conn is None
    conn = conn or get_conn()
    if words:
        _install_word_match(conn)
    else:
        _install_fold(conn)
    try:
        joins, where, params = filters(None, corpus, years)
        clause = f"{where} AND ({tests})" if where else f"WHERE ({tests})"
        selected = ",".join(f"m.{c}" for c in CORE_COLUMNS)
        return _decode_frame(pd.read_sql_query(
            f"SELECT {selected} FROM messages m {joins} {clause} "
            f"ORDER BY m.video_id, m.kind, m.position", conn, params=params + terms))
    finally:
        if close:
            conn.close()


_NO_WORD_BREAKS = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
_HAS_WORD_BREAKS = re.compile(r"[0-9A-Za-z\u00c0-\u024f\u0370-\u03ff\u0400-\u04ff]")


def word_boundaries_apply(term):

    return bool(_HAS_WORD_BREAKS.search(term)) or not _NO_WORD_BREAKS.search(term)


def word_pattern(term):

    if word_boundaries_apply(term):
        return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
    return re.compile(re.escape(term), re.IGNORECASE)


def terms_without_word_boundaries(terms):

    return [term for term in terms if not word_boundaries_apply(term)]


def _install_word_match(conn):

    cache = {}

    def matches(text, term):
        if not text or not term:
            return 0
        if term not in cache:
            cache[term] = word_pattern(term)
        return 1 if cache[term].search(text) else 0

    conn.create_function("ytcm_word_match", 2, matches, deterministic=True)


def _install_fold(conn):

    conn.create_function("PYFOLD", 1, fold, deterministic=True)


def filter_videos_by_description(terms, mode="or", conn=None, words=False):

    if isinstance(terms, str):
        terms = [terms]
    terms = [t.casefold() for t in terms if t]
    if not terms:
        return []
    joiner = " OR " if mode == "or" else " AND "
    if words:
        tests = joiner.join(["ytcm_word_match(description, ?) = 1"] * len(terms))
    else:
        tests = joiner.join(["INSTR(PYFOLD(description), ?) > 0"] * len(terms))
    close = conn is None
    conn = conn or get_conn()
    try:
        if words:
            _install_word_match(conn)
        else:
            _install_fold(conn)
        return [row[0] for row in conn.execute(
            f"SELECT video_id FROM videos WHERE {tests}", terms)]
    finally:
        if close:
            conn.close()


def filter_as_nested(terms, mode="or", corpus=None, years=None, db_path=None, words=False):

    conn = get_conn(db_path)
    try:
        matches = filter_messages(terms, mode, corpus, years, conn=conn, words=words)
        wanted = set(matches["video_id"]) | set(
            filter_videos_by_description(terms, mode, conn=conn, words=words))
        keep = set(matches["message_id"])
        result = {}
        for video_id, entry in iter_videos(conn, corpus, years):
            if video_id not in wanted:
                continue
            comments = []
            for comment in entry["comments"]:
                replies = [r for r in comment["replies"]
                           if r.get("youtube_reply_id") in keep]
                if comment.get("youtube_comment_id") in keep or replies:
                    kept = dict(comment)
                    kept["replies"] = replies
                    comments.append(kept)
            result[video_id] = {"video_info": entry["video_info"], "comments": comments}
        return result
    finally:
        conn.close()


ALL_ROLES = ("uploader", "commenter", "replier")


def participation_by_video(roles=ALL_ROLES, corpus=None, years=None, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        join, where, params = "", "", []
        if corpus:
            join = "JOIN video_corpora vc ON vc.video_id = v.video_id"
            where = "WHERE vc.corpus = ?"
            params = [corpus]
        if years:
            where = (where + " AND " if where else "WHERE ") + \
                    "v.published_at >= ? AND v.published_at < ?"
            params += [f"{years[0]}-01-01", f"{years[1] + 1}-01-01"]

        uploaders = {row[0]: row[1] for row in conn.execute(
            f"SELECT v.video_id, v.channel_id FROM videos v {join} {where}", params)}

        participation = defaultdict(set)
        for video_id, uploader in uploaders.items():
            if "uploader" in roles and uploader:
                participation[video_id].add(uploader)

        kinds = [k for k, role in (("comment", "commenter"), ("reply", "replier"))
                 if role in roles]
        for kind in kinds:
            mjoin, mwhere, mparams = filters("c" if kind == "comment" else "r",
                                              corpus, years)
            clause = mwhere or "WHERE 1=1"
            for video_id, channel_id in conn.execute(
                    f"SELECT m.video_id, m.author_channel_id FROM messages m {mjoin} "
                    f"{clause} AND m.author_channel_id IS NOT NULL "
                    f"AND m.author_channel_id != ''", mparams):
                participation[video_id].add(channel_id)

        return uploaders, participation
    finally:
        if close:
            conn.close()


def channel_role_counts(corpus=None, years=None, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        stats = defaultdict(lambda: {"as_uploader": 0, "as_commenter": 0,
                                     "as_replier": 0, "unique_videos": set()})
        uploaders, _ = participation_by_video(("uploader",), corpus, years, conn=conn)
        for video_id, uploader in uploaders.items():
            if uploader:
                stats[uploader]["as_uploader"] += 1
                stats[uploader]["unique_videos"].add(video_id)

        for kind, field in (("comment", "as_commenter"), ("reply", "as_replier")):
            joins, where, params = filters("c" if kind == "comment" else "r", corpus, years)
            clause = where or "WHERE 1=1"
            for video_id, channel_id, count in conn.execute(
                    f"SELECT m.video_id, m.author_channel_id, COUNT(*) FROM messages m "
                    f"{joins} {clause} AND m.author_channel_id IS NOT NULL "
                    f"AND m.author_channel_id != '' "
                    f"GROUP BY m.video_id, m.author_channel_id", params):
                stats[channel_id][field] += count
                stats[channel_id]["unique_videos"].add(video_id)
        return stats
    finally:
        if close:
            conn.close()


def reply_author_pairs(include_self=False, corpus=None, years=None, conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        joins, where, params = filters("r", corpus, years)
        clause = where or "WHERE 1=1"
        rows = conn.execute(
            f"SELECT p.author_channel_id, m.author_channel_id FROM messages m "
            f"JOIN messages p ON p.message_id = m.parent_id {joins} {clause} "
            f"AND m.author_channel_id IS NOT NULL AND m.author_channel_id != '' "
            f"AND p.author_channel_id IS NOT NULL AND p.author_channel_id != ''", params)
        if include_self:
            return list(rows)
        return [(a, b) for a, b in rows if a != b]
    finally:
        if close:
            conn.close()


def corpus_summary(conn=None):

    close = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute(
            "SELECT vc.corpus, COUNT(DISTINCT vc.video_id), COUNT(m.message_id) "
            "FROM video_corpora vc LEFT JOIN messages m ON m.video_id = vc.video_id "
            "GROUP BY vc.corpus ORDER BY 2 DESC")
        return pd.DataFrame(rows, columns=["corpus", "videos", "messages"])
    finally:
        if close:
            conn.close()
