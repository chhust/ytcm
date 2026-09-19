import json
import logging
import os
import re
import time

from googleapiclient.errors import HttpError

from collections import defaultdict

logger = logging.getLogger(__name__)


def is_quota_exceeded(e):
    """
    Inspect an HttpError's parsed error details to tell YouTube API quota exhaustion
    apart from other errors that share the same HTTP status code (e.g. a 403 for
    "commentsDisabled" vs. a 403 for "quotaExceeded").
    """

    details = getattr(e, "error_details", None)
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and detail.get("reason") in (
                "quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"
            ):
                return True
    return False


def error_reasons(e):

    reasons = []
    details = getattr(e, "error_details", None)
    if isinstance(details, list):
        for detail in details:
            if isinstance(detail, dict) and detail.get("reason"):
                reasons.append(str(detail["reason"]).lower())
    return reasons


def is_comments_disabled(e):

    if getattr(getattr(e, "resp", None), "status", None) != 403:
        return False
    reasons = error_reasons(e)
    if reasons:
        return any("commentsdisabled" in r for r in reasons)
    return True


def is_transient(e):

    if is_quota_exceeded(e):
        return False
    status = getattr(getattr(e, "resp", None), "status", None)
    if status in (500, 502, 503, 504):
        return True
    return any(r in ("backenderror", "internalerror", "servicunavailable",
                     "serviceunavailable") for r in error_reasons(e))


def call_with_retry(operation, attempts, backoff, on_retry=None):

    delay = backoff
    for attempt in range(1, max(1, int(attempts or 0)) + 1):
        try:
            return operation()
        except HttpError as e:
            if attempt >= attempts or not is_transient(e):
                raise
            if on_retry:
                on_retry(attempt, e, delay)
            if delay:
                time.sleep(delay)
            delay *= 2


def safeint(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def safe_write_json(data, target_file):
    temp_file = target_file + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temp_file, target_file)
    except (OSError, TypeError, ValueError) as e:
        logger.error(f"Error: could not write to '{target_file}': {e}.")
        if os.path.exists(temp_file):
            os.remove(temp_file)


def save_video_ids(video_ids, filename):
    """
    Write the given list of video IDs to disk, one per line.
    """
    temp_file = filename + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as handle:
            for video_id in video_ids:
                handle.write(f"{video_id}\n")
        os.replace(temp_file, filename)
    except OSError as e:
        logger.error(f"Error: could not write to '{filename}': {e}.")
        if os.path.exists(temp_file):
            os.remove(temp_file)


def is_youtube_video_id(id_string):
    return bool(re.compile(r"^[A-Za-z0-9_-]{11}$").match(id_string))


def check_pending_downloads(filename):
    if not os.path.exists(filename):
        return 0
    try:
        with open(filename, "r", encoding="utf-8") as id_file:
            temp_ids = [line.strip() for line in id_file if line.strip()]
        if temp_ids:
            return len(temp_ids)
        else:
            return 0
    except (OSError, UnicodeDecodeError) as e:
        logger.error(f"Could not check for pending downloads in '{filename}': {e}.")
        return -1


def merge_schemas(schema1, schema2):
    if isinstance(schema1, dict) and isinstance(schema2, dict):
        merged = dict(schema1)
        for key, value in schema2.items():
            if key in merged:
                merged[key] = merge_schemas(merged[key], value)
            else:
                merged[key] = value
        return merged
    elif isinstance(schema1, list) and isinstance(schema2, list):
        return schema1 + schema2
    elif isinstance(schema1, list):
        return schema1 + [schema2]
    elif isinstance(schema2, list):
        return [schema1] + schema2
    else:
        return schema1


def read_any_corpus_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    collected = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise json.JSONDecodeError(f"line {number} is not an object", line, 0)
        if "video_id" in record:
            collected[record["video_id"]] = record
        elif len(record) == 1:
            collected.update(record)
        else:
            raise json.JSONDecodeError(f"line {number} carries no video id", line, 0)
    if not collected:
        raise json.JSONDecodeError("nothing to read", text[:80], 0)
    return collected


def validate_data_structure(file_path, verbose=True):
    try:
        json_data = read_any_corpus_file(file_path)
    except json.JSONDecodeError as e:
        logger.error(f"JSON error in '{file_path}': {e}.")
        print(f"JSON error in '{file_path}'.")
        return
    except FileNotFoundError:
        logger.error(f"File not found error: '{file_path}'.")
        print(f"File not found error: '{file_path}'.")
        return

    if not isinstance(json_data, dict):
        logger.error("Error: no (dict) object found on top level.")
        print(f"JSON structure in '{file_path}' is invalid.")
        return

    def extract_schema(node):
        if isinstance(node, dict):
            return {
                key: extract_schema(value) for key, value in node.items()
            }
        elif isinstance(node, list):
            combined_schema = {}
            for item in node:
                item_schema = extract_schema(item)
                combined_schema = merge_schemas(combined_schema, item_schema)
            return [combined_schema]
        else:
            return type(node).__name__

    combined_schema = {}
    for video_id, video_entry in json_data.items():
        if not isinstance(video_entry, dict):
            logger.warning(f"Warning: ID '{video_id}' has no object.")
            print(f"Problem encountered in VideoID {video_id}. Continuing")
            continue

        entry_schema = extract_schema(video_entry)
        combined_schema = merge_schemas(combined_schema, entry_schema)

    def print_schema(schema, indent=0, root="video_ID"):
        indent_str = "  " * indent
        if isinstance(schema, dict):
            print(f"{indent_str}\"{root}\" (dict):")
            for key, value in schema.items():
                print_schema(value, indent + 1, key)
        elif isinstance(schema, list):
            print(f"{indent_str}\"{root}\" (list):")
            if schema:
                print_schema(schema[0], indent + 1, "<item>")
            else:
                print(f"{indent_str}  <empty>")
        else:
            print(f"{indent_str}\"{root}\" ({schema})")

    if verbose:
        print_schema(combined_schema)


def get_df_comments():

    from ytcm.tubescope import analyze_comments, collect_all_records, mode_label, resolve_mode

    all_records = collect_all_records()               # comments and/or replies, per TUBESCOPE_MODE
    if all_records is None or (hasattr(all_records, "empty") and all_records.empty):
        print(f"Error: no {mode_label().lower()} found (TUBESCOPE_MODE = {resolve_mode()!r}).")
        return None, None

    df = analyze_comments(all_records)
    df["date"] = df["date"].dt.tz_localize(None)        # delete timezone info

    return df, None



def add_occurrence(occurrences, bucket, identifier, path):
    if identifier is None:
        return

    identifier = str(identifier)
    if bucket not in occurrences:
        occurrences[bucket] = defaultdict(list)

    occurrences[bucket][identifier].append(path)


def find_duplicate_ids(data, video_bucket_name="video_id", comment_bucket_name="comment_id",
                       reply_bucket_name="reply_id", comments_key="comments", replies_key="replies",
                       comment_id_key="youtube_comment_id", reply_id_key="youtube_reply_id",
                       video_id_field_fallback=None):
    """
    Scan the dataset for duplicate IDs
    """

    occurrences = {}

    if isinstance(data, dict):
        for vid in data.keys():
            add_occurrence(occurrences, video_bucket_name, vid, f"/{vid}")
    elif isinstance(data, list) and video_id_field_fallback:
        for i, item in enumerate(data):
            if isinstance(item, dict):
                add_occurrence(occurrences, video_bucket_name, item.get(video_id_field_fallback), f"/videos[{i}]")

    def walk_video(node, base_path):
        if not isinstance(node, dict):
            return
        comments = node.get(comments_key, [])
        if isinstance(comments, list):
            for comment_index, comment in enumerate(comments):
                comment_path = f"{base_path}/{comments_key}[{comment_index}]"
                if isinstance(comment, dict):
                    add_occurrence(occurrences, comment_bucket_name, comment.get(comment_id_key), comment_path)
                    replies = comment.get(replies_key, [])
                    if isinstance(replies, list):
                        for reply_index, reply in enumerate(replies):
                            reply_path = f"{comment_path}/{replies_key}[{reply_index}]"
                            if isinstance(reply, dict):
                                add_occurrence(occurrences, reply_bucket_name, reply.get(reply_id_key), reply_path)

    if isinstance(data, dict):
        for vid, payload in data.items():
            walk_video(payload, f"/{vid}")
    elif isinstance(data, list):
        for i, payload in enumerate(data):
            walk_video(payload, f"/videos[{i}]")

    dupes = {}
    for bucket, id_map in occurrences.items():
        for _id, paths in id_map.items():
            if len(paths) > 1:
                if bucket not in dupes:
                    dupes[bucket] = {}
                dupes[bucket][_id] = paths

    return dupes, occurrences

def print_dupe_report(dupes):
    if not dupes:
        print("No duplicate IDs in any bucket.")
        return
    for bucket, id_map in dupes.items():
        print(f"\nDuplicates in bucket '{bucket}':")
        for _id, paths in id_map.items():
            print(f"  - ID '{_id}' appears {len(paths)}x")
            for p in paths[:10]:
                print(f"      • {p}")
            if len(paths) > 10:
                print(f"      • … (+{len(paths)-10} more)")


def duplicate_check(file_path, comments_key="comments", replies_key="replies", comment_id_key="youtube_comment_id",
                    reply_id_key="youtube_reply_id"):
    """
    Load JSON and run the duplicate finder with dataset defaults.
    """

    data = read_any_corpus_file(file_path)
    dupes, occurrences = find_duplicate_ids(
        data,
        comments_key=comments_key,
        replies_key=replies_key,
        comment_id_key=comment_id_key,
        reply_id_key=reply_id_key
    )
    print_dupe_report(dupes)
    return dupes, occurrences


def string_len(x):
    try:
        return len(x)
    except TypeError:
        return 0


def content_score(obj):
    """
    Higher score means 'richer' item (which will be kept).
    """

    if obj is None:
        return 0

    if isinstance(obj, dict):
        score = len(obj)  # number of keys
        for v in obj.values():
            score += content_score(v)
        return score

    if isinstance(obj, list):
        score = len(obj)  # number of items
        for it in obj:
            score += content_score(it)
        return score

    if isinstance(obj, (str,)):
        return 1 + string_len(obj)
    if isinstance(obj, (int, float, bool)):
        return 1

    return 0


def parse_segment(seg):
    """
    Parse a path segment like comments[3] and returns (key, index).
    """

    if "[" in seg and seg.endswith("]"):
        key = seg[:seg.index("[")]
        idx_str = seg[seg.index("[")+1:-1]

        try:
            idx = int(idx_str)
        except ValueError:
            idx = None

        return key, idx

    return seg, None


def resolve_parent_and_index(data, path):
    """
    Given a path like '/VIDEO123/comments[4]' or '/VIDEO123/comments[4]/replies[2]',
    return (parent_list, index) so that parent_list[index] is the target item.
    If the last query is into a dict (no index), returns (parent_container, key).
    """

    if not path or path[0] != "/":
        raise ValueError(f"Unsupported path format: {path}")

    node = data
    parent = None
    key_or_index = None

    parts = [p for p in path.split("/") if p]

    for seg in parts:
        key, idx = parse_segment(seg)

        if isinstance(node, dict):
            if key not in node:
                # Path became invalid after previous edits
                return None, None
            parent = node
            key_or_index = key
            node = node[key]
            if idx is not None:
                if not isinstance(node, list):
                    return None, None
                if idx < 0 or idx >= len(node):
                    return None, None
                parent = node
                key_or_index = idx
                node = node[idx]
        elif isinstance(node, list):
            if idx is None:
                return None, None
            if idx < 0 or idx >= len(node):
                return None, None
            parent = node
            key_or_index = idx
            node = node[idx]
        else:
            return None, None

    return parent, key_or_index


def gather_items_for_paths(data, paths):
    items = []
    for path in paths:
        parent, key_or_index = resolve_parent_and_index(data, path)
        if parent is None:
            continue
        item = parent[key_or_index] if isinstance(parent, list) else parent.get(key_or_index)
        items.append((path, parent, key_or_index, item))

    return items

def delete_many(grouped):
    """
    grouped: list of tuples (parent_list, index). This function deletes by descending index per parent.
    """

    deletions = 0
    buckets = {}
    for parent, idx in grouped:
        if isinstance(parent, list):
            buckets.setdefault(id(parent), (parent, []))[1].append(idx)
        elif isinstance(parent, dict):
            buckets.setdefault(id(parent), (parent, []))[1].append(idx)  # idx is a key
    for _, (parent, indices) in buckets.items():
        if isinstance(parent, list):
            for index in sorted(set(indices), reverse=True):
                if 0 <= index < len(parent):
                    del parent[index]
                    deletions += 1
        else:
            for key in set(indices):
                if key in parent:
                    del parent[key]
                    deletions += 1
    return deletions


def clean_dupes(data, dupes, replies_key="replies", comment_bucket_name="comment_id",
                reply_bucket_name="reply_id"):
    """
    Purge duplicates in-place based on the 'dupes' structure from find_duplicate_ids.
    Keeps the occurrence with the highest content score; removes the others.
    """

    report = {'kept': {}, 'removed': {}, 'stats': {'removed_count': 0, 'buckets': {}}}

    pending = []
    for bucket in [comment_bucket_name, reply_bucket_name]:
        if bucket not in dupes:
            continue
        id_map = dupes[bucket]
        for record_id, paths in id_map.items():
            packed = gather_items_for_paths(data, paths)
            packed = [t for t in packed if t[3] is not None]
            if len(packed) <= 1:
                continue

            scored = []
            for path, parent, key_or_index, item in packed:
                score = content_score(item)
                # small bonus if comment has replies (for comment bucket), to prefer richer threads
                if bucket == comment_bucket_name and isinstance(item, dict):
                    reps = item.get(replies_key, [])
                    if isinstance(reps, list):
                        score += max(0, len(reps)) * 2
                scored.append((score, path, parent, key_or_index))

            scored.sort(key=lambda x: (-x[0], paths.index(x[1])))
            keep_path = scored[0][1]

            to_delete = []
            removed_paths = []
            for _, path, parent, key_or_index in scored[1:]:
                to_delete.append((parent, key_or_index))
                removed_paths.append(path)

            pending.extend(to_delete)

            report['kept'].setdefault(bucket, {})[record_id] = keep_path
            if removed_paths:
                report['removed'].setdefault(bucket, {})[record_id] = removed_paths
            report['stats']['buckets'][bucket] = \
                report['stats']['buckets'].get(bucket, 0) + len(to_delete)

    report['stats']['removed_count'] = delete_many(pending)

    return report


def load_json_file(path):
    return read_any_corpus_file(path)


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
