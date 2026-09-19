import logging
logger = logging.getLogger(__name__)

from datetime import datetime, timezone

from ytcm.api_utils import get_video_information_batch
from ytcm.bridge import renumber_positions, store_video
from ytcm.channel_utils import save_cache
from ytcm.comments_utils import get_comments
from ytcm.config import (CHECKPOINT_EVERY, COMMENT_PAGE_SIZE, DEFAULT_CORPUS,
                                    ID_BATCH_SIZE)
from ytcm import identity as ident
from ytcm.db import (check_fingerprint, create_indexes, get_conn, mark_pending,
                                    mark_status, pending_videos, record_provenance)
from ytcm.helper_utils import save_video_ids


def process_videos(youtube, video_ids, video_ids_file, corpus=DEFAULT_CORPUS, db_path=None):

    if not ident.have_key():
        raise ident.NoKey(
            "No pseudonymization key. Run 'generatehash' before downloading.")

    started = datetime.now(timezone.utc).isoformat()
    total_videos = len(video_ids)
    processed_videos = 0
    failed_videos = 0
    quota_exhausted = False
    metadata = {}
    unreachable = set()

    conn = get_conn(db_path)
    check_fingerprint(conn, ident.fingerprint())
    mark_pending(conn, video_ids, corpus)

    def publish_todo():
        try:
            save_video_ids(pending_videos(conn, corpus), video_ids_file)
        except Exception as e:
            logger.error(f"Error saving remaining video IDs: {e}.")

    try:
        for index, video_id in enumerate(video_ids, start=1):
            print(f"Processing video {index} of {total_videos} (ID: {video_id}).")

            try:
                if video_id not in metadata:
                    chunk = [v for v in video_ids[index - 1:index - 1 + ID_BATCH_SIZE]
                             if v not in metadata]
                    fetched, quota_exceeded, missed = get_video_information_batch(youtube, chunk)
                    if quota_exceeded:
                        logger.error("Quota exceeded while retrieving video information. Stopping download.")
                        quota_exhausted = True
                        break
                    unreachable |= missed
                    metadata.update(fetched)
                    for absent in chunk:
                        metadata.setdefault(absent, None)

                video_info = metadata[video_id]

                if not video_info:
                    logger.error(f"Failed to retrieve information for video ID {video_id}. Skipping this video ID.")
                    if video_id in unreachable:
                        mark_status(conn, video_id, "pending", "metadata not yet retrieved; stays pending")
                    else:
                        mark_status(conn, video_id, "unavailable", "metadata not available; this is permanent")
                    failed_videos += 1
                    continue

                expected = video_info.pop("comment_count", None)
                comments, quota_exceeded, comments_status = get_comments(
                    youtube, total=expected, part="snippet", videoId=video_id,
                    maxResults=COMMENT_PAGE_SIZE, textFormat="plainText")

                if quota_exceeded:
                    logger.error(f"Quota exceeded while retrieving comments for video ID {video_id}. Stopping download.")
                    quota_exhausted = True
                    break

                if comments is None:
                    logger.error(f"Could not retrieve comments for video ID {video_id}. "
                                 f"Leaving it pending so the next run retries it.")
                    mark_status(conn, video_id, "pending", "comment fetch failed")
                    failed_videos += 1
                    continue

                video_info["comments_status"] = comments_status
                store_video(conn, video_id, corpus, video_info, comments, index - 1)
                mark_status(conn, video_id, "done")

                processed_videos += 1
                print(f"Video {index} of {total_videos} done ({video_id}).")

                if processed_videos % CHECKPOINT_EVERY == 0:
                    publish_todo()

            except Exception as e:
                logger.error(f"Unexpected error processing video ID {video_id}: {e}.")
                mark_status(conn, video_id, "pending", str(e))
                failed_videos += 1
                continue

        renumber_positions(conn, corpus)
        publish_todo()
        record_provenance(conn, "download", started,
                          datetime.now(timezone.utc).isoformat(), processed_videos,
                          corpus=corpus)

        create_indexes(conn)
    finally:
        conn.close()

    save_cache()

    print(f"\nProcessing complete. Processed {processed_videos} of {total_videos} videos.")
    if failed_videos > 0:
        logger.error(f"Failed to process {failed_videos} videos, see above for specific errors.")

    return quota_exhausted


def generate_search_list(primary_lists, secondary_list):
    """
    Combine primary and secondary lists to a list of search terms.
    """

    combined = []

    if not primary_lists:
        return []

    if not secondary_list:
        return primary_lists

    for primary in primary_lists:
        for secondary in secondary_list:
            combined.append(primary + [secondary])
    return combined
