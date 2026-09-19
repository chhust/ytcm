import logging
logger = logging.getLogger(__name__)

import json

from datetime import datetime, timezone

from tqdm                          import tqdm
from langdetect                    import DetectorFactory, detect_langs, LangDetectException
from textblob                      import TextBlob
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer            # vadersentiment

from ytcm.config import (ANONYMIZATION_MAP_JSON, LANGUAGE_MIN_CHARS,
                                           LANGUAGE_MIN_CONFIDENCE)
from ytcm.db import get_conn, record_provenance
from ytcm.platform_utils import plural

DetectorFactory.seed = 0

_VADER = SentimentIntensityAnalyzer()

BATCH = 5000


def _now():
    return datetime.now(timezone.utc).isoformat()


def _pending(conn, column, force_rebuild, extra=""):
    clause = "" if force_rebuild else f"AND {column} IS NULL"
    return conn.execute(f"SELECT COUNT(*) FROM messages WHERE 1=1 {clause} {extra}").fetchone()[0]


def review(db_path=None):
    """
    Do a manual review of the downloaded videos to purge unwanted content.
    """

    conn = get_conn(db_path)
    try:
        pending = [row[0] for row in conn.execute(
            "SELECT video_id FROM videos WHERE manual_review IS NULL OR manual_review = 'false' "
            "ORDER BY position")]
        if not pending:
            print("All videos already reviewed.")
            return

        print(f"{len(pending)} videos to review. Enter 'd' to delete, anything else to keep, 'q' to stop.")
        deleted = 0
        for video_id in pending:
            title, views = conn.execute(
                "SELECT title, views FROM videos WHERE video_id = ?", (video_id,)).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE video_id = ?", (video_id,)).fetchone()[0]
            print(f"\n{video_id}: {title}\n   {views:,} views, {count:,} messages")
            answer = input("   delete? (d/k/q) ").strip().lower()
            if answer == "q":
                break
            if answer == "d":
                conn.execute("DELETE FROM annotations WHERE message_id IN "
                             "(SELECT message_id FROM messages WHERE video_id = ?)", (video_id,))
                conn.execute("DELETE FROM messages WHERE video_id = ?", (video_id,))
                conn.execute("DELETE FROM video_annotations WHERE video_id = ?", (video_id,))
                conn.execute("DELETE FROM video_corpora WHERE video_id = ?", (video_id,))
                conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
                deleted += 1
            else:
                conn.execute("UPDATE videos SET manual_review = ? WHERE video_id = ?",
                             (json.dumps(True), video_id))
            conn.commit()
        print(f"\nReviewed. {deleted:,} {plural(deleted, 'video')} deleted.")
    finally:
        conn.close()


def detect_languages(db_path=None, force_rebuild=False, min_chars=None, min_confidence=None):

    started = _now()
    min_chars = LANGUAGE_MIN_CHARS if min_chars is None else min_chars
    min_confidence = LANGUAGE_MIN_CONFIDENCE if min_confidence is None else min_confidence
    conn = get_conn(db_path)
    written = 0
    video_written = 0
    messages_done = 0
    too_short = 0
    unsure = 0
    try:
        clause = "" if force_rebuild else "AND language IS NULL"
        total = _pending(conn, "language", force_rebuild)
        print(f"Detecting languages for {total:,} messages.")

        cursor = ""
        with tqdm(total=total, desc="Messages", unit="msg") as bar:
            while True:
                rows = conn.execute(
                    f"SELECT message_id, text FROM messages WHERE message_id > ? {clause} "
                    f"ORDER BY message_id LIMIT {BATCH}", (cursor,)).fetchall()
                if not rows:
                    break
                cursor = rows[-1][0]
                updates = []
                for message_id, text in rows:
                    language = detect_language(text or "", min_chars=min_chars,
                                               min_confidence=min_confidence)
                    if language == "unknown":
                        if len((text or "").strip()) < min_chars:
                            too_short += 1
                        else:
                            unsure += 1
                    updates.append((language, "langdetect", message_id))
                conn.executemany("UPDATE messages SET language = ?, language_source = ? "
                                 "WHERE message_id = ?", updates)
                conn.commit()
                written += len(updates)
                messages_done += len(updates)
                bar.update(len(updates))

        for field in ("title", "description"):
            dimension = f"{field}_language"
            if force_rebuild:
                rows = conn.execute(
                    f"SELECT v.video_id, v.{field} FROM videos v").fetchall()
            else:
                rows = conn.execute(
                    f"SELECT v.video_id, v.{field} FROM videos v WHERE NOT EXISTS "
                    f"(SELECT 1 FROM video_annotations a WHERE a.video_id = v.video_id "
                    f"AND a.dimension = ?)", (dimension,)).fetchall()
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO video_annotations (video_id, dimension, value, source) "
                    "VALUES (?,?,?,'langdetect')",
                    [(video_id, dimension,
                      json.dumps(detect_language(text or "", min_chars=min_chars,
                                                 min_confidence=min_confidence)))
                     for video_id, text in rows])
                conn.commit()
                video_written += len(rows)

        record_provenance(conn, "language", started, _now(), written + video_written,
                          model="langdetect")
    finally:
        conn.close()

    unknown = too_short + unsure
    print(f"{written:,} records given a language.")
    if messages_done:
        print(f"  left unknown: {unknown:,} of {messages_done:,} messages "
              f"({unknown / messages_done * 100:.1f}%) - "
              f"{too_short:,} below {min_chars} characters, "
              f"{unsure:,} below confidence {min_confidence}")
    return written


def sentiment_analysis(db_path=None, force_rebuild=False):

    started = _now()
    conn = get_conn(db_path)
    written = 0
    try:
        missing = conn.execute("SELECT COUNT(*) FROM messages WHERE language IS NULL").fetchone()[0]
        if missing:
            print(f"Aborting sentiment analysis: {missing:,} messages have no language yet.")
            print("Please run 'language' first.")
            return 0

        clause = "" if force_rebuild else "AND blob_sentiment IS NULL"
        total = _pending(conn, "blob_sentiment", force_rebuild)
        print(f"Running sentiment analysis on {total:,} messages.")

        cursor = ""
        with tqdm(total=total, desc="Messages", unit="msg") as bar:
            while True:
                rows = conn.execute(
                    f"SELECT message_id, text, language FROM messages "
                    f"WHERE message_id > ? {clause} ORDER BY message_id LIMIT {BATCH}",
                    (cursor,)).fetchall()
                if not rows:
                    break
                cursor = rows[-1][0]
                updates = []
                for message_id, text, language in rows:
                    if text and language == "en":
                        blob, vader = blob_analysis(text), vader_analysis(text)
                    else:
                        blob = vader = "N/A"
                    updates.append((json.dumps(blob), "textblob",
                                    json.dumps(vader), "vader", message_id))
                conn.executemany(
                    "UPDATE messages SET blob_sentiment = ?, blob_sentiment_source = ?, "
                    "vader_sentiment = ?, vader_sentiment_source = ? WHERE message_id = ?", updates)
                conn.commit()
                written += len(updates)
                bar.update(len(updates))

        record_provenance(conn, "sentiment", started, _now(), written, model="textblob+vader")
    finally:
        conn.close()

    print(f"{written:,} records scored.")
    return written


def blob_analysis(text):
    """
    Use the TextBlob library for automatic sentiment analysis.
    Cf. https://textblob.readthedocs.io/en/dev/advanced_usage.html
    """

    analysis = TextBlob(text)
    sentiment_score = analysis.sentiment.polarity

    return round(sentiment_score, 2)


def vader_analysis(text):
    """
    Use the VADER (Valence Aware Dictionary and sEntiment Reasoner) library for automatic sentiment analysis.
    Cf. Hutto, C. J. & Gilbert, E. (2014). "VADER: A Parsimonious Rule-based Model for Sentiment Analysis of Social Media Text", ICWSM: https://github.com/cjhutto/vaderSentiment
    """

    analysis = _VADER
    sentiment_score = analysis.polarity_scores(text)["compound"]

    return round(sentiment_score, 2)


def detect_language(text, min_chars=None, min_confidence=None):
    """
    Cf. https://github.com/Mimino666/langdetect
    """

    min_chars = LANGUAGE_MIN_CHARS if min_chars is None else min_chars
    min_confidence = LANGUAGE_MIN_CONFIDENCE if min_confidence is None else min_confidence

    if not text or len(text.strip()) < min_chars:
        return "unknown"

    try:
        best = detect_langs(text)[0]
    except (LangDetectException, ValueError, TypeError, IndexError):
        return "unknown"

    if best.prob < min_confidence:
        return "unknown"

    return best.lang



def export_anonymization_map(path=ANONYMIZATION_MAP_JSON, db_path=None):

    conn = get_conn(db_path)
    try:
        mapping = {row[0]: row[1] for row in conn.execute(
            "SELECT channel_id, anon_token FROM channels "
            "WHERE anon_token IS NOT NULL AND anon_token != channel_id")}
    finally:
        conn.close()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(mapping, handle, ensure_ascii=False)
    print(f"Wrote {len(mapping):,} mappings to '{path}'.")
    return len(mapping)


def import_anonymization_map(path=ANONYMIZATION_MAP_JSON, db_path=None):

    with open(path, "r", encoding="utf-8") as handle:
        mapping = json.load(handle)
    mapping = {k: v for k, v in mapping.items() if isinstance(v, str)}

    conn = get_conn(db_path)
    try:
        conn.executemany("INSERT INTO channels (channel_id, anon_token) VALUES (?,?) "
                         "ON CONFLICT(channel_id) DO UPDATE SET anon_token = excluded.anon_token",
                         list(mapping.items()))
        conn.commit()
    finally:
        conn.close()
    print(f"Read {len(mapping):,} mappings from '{path}'.")
    return len(mapping)
