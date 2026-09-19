import logging
logger = logging.getLogger(__name__)

import csv
import heapq
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import numpy as np
from tqdm import tqdm

from ytcm.config import (CONFIRM_ABOVE_SECONDS, EMBEDDINGS,
                         EMBEDDING_BATCH, OLLAMA_URL, SEMSEARCH_PREVIEW,
                         SEMSEARCH_RANKING_WARNING_ABOVE, SEMSEARCH_TOP_K)
from ytcm.db import get_conn, record_provenance, text_hash
from ytcm.platform_utils import printable

SCALE = 127.0


def split_spec(spec=None):

    spec = spec or EMBEDDINGS
    provider, _, model = (spec or "").partition(":")
    if not provider or not model:
        raise ValueError(f"Not a provider spec: {spec!r}. Expected 'provider:model'.")
    return provider, model


def embed_texts(texts, spec=None, timeout=900):

    provider, model = split_spec(spec)
    if provider != "ollama":
        raise NotImplementedError(
            f"No embedding provider '{provider}'. Only ollama is wired up.")

    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=json.dumps({"model": model, "input": list(texts)}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)["embeddings"]
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Could not reach the embedding provider at {OLLAMA_URL}: {e}. "
            f"Is ollama running, and is '{model}' pulled?") from e


def quantise(vector):

    array = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(array)
    if norm:
        array = array / norm
    return np.round(array * SCALE).astype(np.int8)


def estimate_local(sample_texts, total, spec=None):

    started = time.time()
    embed_texts(sample_texts, spec)
    seconds = (time.time() - started) / max(len(sample_texts), 1) * total
    return seconds


def confirm(spec, total, sample_texts, assume_yes=False):

    provider, model = split_spec(spec)

    if provider != "ollama":
        raise NotImplementedError(
            f"No embedding provider '{provider}'. Only ollama is wired up.")

    seconds = estimate_local(sample_texts, total, spec)
    if seconds < CONFIRM_ABOVE_SECONDS or assume_yes:
        if seconds >= CONFIRM_ABOVE_SECONDS:
            print(f"About {seconds / 60:,.0f} minutes for {total:,} records.")
        return True
    shown = f"{seconds / 60:,.0f} minutes" if seconds < 5400 else f"{seconds / 3600:,.1f} hours"
    print(f"\nThis will take about {shown} for {total:,} records on this computer, "
          f"measured on a batch of {len(sample_texts)}.")
    return input("Continue (y/n)? ").strip().lower().startswith("y")


STILL_CURRENT = "AND e.model = ? AND (e.text_hash IS NULL OR e.text_hash = PYHASH(m.text))"


def pending_count(conn, model, corpus=None):
    join = "JOIN video_corpora vc ON vc.video_id = m.video_id" if corpus else ""
    where = "AND vc.corpus = ?" if corpus else ""
    params = [model] + ([corpus] if corpus else [])
    return conn.execute(
        f"SELECT COUNT(*) FROM messages m {join} WHERE m.text IS NOT NULL AND m.text != '' "
        f"AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.message_id = m.message_id "
        f"{STILL_CURRENT}) {where}", params).fetchone()[0]


def embed_corpus(spec=None, corpus=None, db_path=None, assume_yes=False, limit=None):

    if limit is not None and limit < 0:
        raise ValueError("--limit can't be negative.")

    provider, model = split_spec(spec)
    started = datetime.now(timezone.utc).isoformat()
    conn = get_conn(db_path)
    written = 0

    try:
        pending = pending_count(conn, model, corpus)
        total = pending if limit is None else min(pending, limit)
        if not pending:
            print("Every message already has an embedding for this model.")
            return 0
        if not total:
            print(f"--limit {limit} asks for nothing; {pending:,} messages are waiting.")
            return 0

        sample = [row[0] for row in conn.execute(
            "SELECT text FROM messages WHERE text IS NOT NULL AND text != '' "
            "LIMIT 32")]
        if not confirm(spec, total, sample, assume_yes):
            print("Nothing was submitted for the main embedding run.")
            return 0

        conn.execute("INSERT INTO embedding_state (model, provider, started_at, updated_at) "
                     "VALUES (?,?,?,?) ON CONFLICT(model) DO UPDATE SET updated_at = excluded.updated_at",
                     (model, provider, started, started))
        conn.commit()

        join = "JOIN video_corpora vc ON vc.video_id = m.video_id" if corpus else ""
        where = "AND vc.corpus = ?" if corpus else ""
        params = [model] + ([corpus] if corpus else [])

        cursor = ""
        with tqdm(total=total, desc="Messages", unit="msg", leave=False) as bar:
            while written < total:
                rows = conn.execute(
                    f"SELECT m.message_id, m.text FROM messages m {join} "
                    f"WHERE m.message_id > ? AND m.text IS NOT NULL AND m.text != '' "
                    f"AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.message_id = "
                    f"m.message_id {STILL_CURRENT}) {where} ORDER BY m.message_id "
                    f"LIMIT {int(min(EMBEDDING_BATCH, total - written))}",
                    [cursor] + params).fetchall()
                if not rows:
                    break
                cursor = rows[-1][0]
                vectors = embed_texts([text for _, text in rows], spec)
                payload = []
                for (message_id, text), vector in zip(rows, vectors):
                    quantised = quantise(vector)
                    payload.append((message_id, model, int(quantised.size), quantised.tobytes(),
                                    text_hash(text)))
                conn.executemany("INSERT OR REPLACE INTO embeddings "
                                 "(message_id, model, dimensions, vector, text_hash) "
                                 "VALUES (?,?,?,?,?)", payload)
                conn.execute("UPDATE embedding_state SET records = records + ?, dimensions = ?, "
                             "updated_at = ? WHERE model = ?",
                             (len(payload), payload[0][2],
                              datetime.now(timezone.utc).isoformat(), model))
                conn.commit()
                written += len(payload)
                bar.update(len(payload))

        record_provenance(conn, "embed", started, datetime.now(timezone.utc).isoformat(),
                          written, corpus=corpus, provider=provider, model=model)
    finally:
        conn.close()

    return written


def search(query, top_k=None, min_score=None, corpus=None, spec=None, db_path=None,
           chunk=50000):

    provider, model = split_spec(spec)
    top_k = SEMSEARCH_TOP_K if top_k is None else top_k
    needle = quantise(embed_texts([query], spec)[0]).astype(np.float32)

    conn = get_conn(db_path)
    try:
        join = "JOIN video_corpora vc ON vc.video_id = m.video_id" if corpus else ""
        where = "AND vc.corpus = ?" if corpus else ""
        params = [model] + ([corpus] if corpus else [])
        cursor = conn.execute(
            f"SELECT e.message_id, e.vector, m.video_id, m.language, m.text "
            f"FROM embeddings e JOIN messages m ON m.message_id = e.message_id {join} "
            f"WHERE e.model = ? {where}", params)

        shortlist, searched = [], 0
        while True:
            rows = cursor.fetchmany(chunk)
            if not rows:
                break
            searched += len(rows)
            matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.int8)
            matrix = matrix.reshape(len(rows), -1).astype(np.float32)
            scores = matrix @ needle / (SCALE * SCALE)
            for score, row in zip(scores, rows):
                score = float(score)
                if min_score is not None and score < min_score:
                    continue
                item = (score, row[0], row[2], row[3], row[4])
                if len(shortlist) < top_k:
                    heapq.heappush(shortlist, item)
                elif score > shortlist[0][0]:
                    heapq.heapreplace(shortlist, item)
        return sorted(shortlist, reverse=True), model, searched
    finally:
        conn.close()


def write_results(path, rows, query, model, searched, top_k, min_score, jsonl=False):

    manifest = {
        "query": query, "model": model, "written_at": datetime.now(timezone.utc).isoformat(),
        "records_searched": searched, "rows_returned": len(rows),
        "top_k": top_k, "min_score": min_score,
        "note": "a ranking of the whole corpus; the cutoff above was chosen",
    }
    with open(path, "w", encoding="utf-8", newline="") as handle:
        if jsonl:
            handle.write(json.dumps({"manifest": manifest}, ensure_ascii=False) + "\n")
            for score, message_id, video_id, language, text in rows:
                handle.write(json.dumps(
                    {"score": round(score, 6), "message_id": message_id,
                     "video_id": video_id, "language": language, "text": text},
                    ensure_ascii=False) + "\n")
        else:
            for key, value in manifest.items():
                handle.write(f"# {key}: {value}\n")
            writer = csv.writer(handle)
            writer.writerow(["score", "message_id", "video_id", "language", "text"])
            for score, message_id, video_id, language, text in rows:
                writer.writerow([round(score, 6), message_id, video_id, language, text])
    return path


def preview(rows, limit=None):
    limit = limit or SEMSEARCH_PREVIEW
    for score, message_id, video_id, language, text in rows[:limit]:
        snippet = (text or "").replace("\n", " ")[:72]
        print(printable(f"  {score:5.3f}  {str(language or '--'):<4} "
                        f"{video_id:<12} {snippet}"))
    if len(rows) > limit:
        print(f"  ... {len(rows) - limit:,} more rows, in the file")
    if len(rows) > SEMSEARCH_RANKING_WARNING_ABOVE:
        print(f"\n  {len(rows):,} rows is a ranking. Every record has a score.\n"
              f"For an exact match use 'filter'.")
