import logging
logger = logging.getLogger(__name__)

from googleapiclient.errors        import HttpError
from tqdm                          import tqdm

from ytcm import identity as ident
from ytcm.channel_utils import get_channel_data_batch
from ytcm import sanitize as sanitize
from ytcm.config import (REPLY_PAGE_SIZE, RETRY_ATTEMPTS,
                                           RETRY_BACKOFF_SECONDS)
from ytcm.helper_utils import (call_with_retry, is_comments_disabled,
                                           is_quota_exceeded)

OK = "ok"
DISABLED = "disabled"
NOT_FOUND = "not_found"
FAILED = "failed"


def get_comments(youtube, total=None, **kwargs):

    video_id = kwargs.get("videoId", "unknown")
    attempts = kwargs.pop("retry_attempts", RETRY_ATTEMPTS)
    backoff = kwargs.pop("retry_backoff", RETRY_BACKOFF_SECONDS)

    def note_retry(attempt, error, delay):
        logger.warning(f"Transient error for video ID {video_id} on attempt {attempt}, "
                       f"retrying in {delay:.0f}s: {error}.")

    def fetch(endpoint, request_kwargs):
        return call_with_retry(lambda: endpoint(**request_kwargs).execute(),
                               attempts, backoff, note_retry)

    def classify(e, what):
        if is_quota_exceeded(e):
            logger.error(f"API quota exceeded while retrieving {what} for video ID {video_id}.")
            return None, True, FAILED
        if is_comments_disabled(e):
            logger.info(f"Comments are disabled for video ID {video_id}.")
            return [], False, DISABLED
        if getattr(getattr(e, "resp", None), "status", None) == 404:
            logger.warning(f"No comment section found for video ID {video_id}.")
            return [], False, NOT_FOUND
        logger.error(f"Giving up on {what} for video ID {video_id}, will retry next run: {e}.")
        return None, False, FAILED

    try:
        results = fetch(youtube.commentThreads().list, kwargs)
    except HttpError as e:
        return classify(e, "comments")
    except Exception as e:
        logger.error(f"Unexpected error retrieving comments for video ID {video_id}, "
                     f"will retry next run: {e}.")
        return None, False, FAILED

    comments = []
    pending_authors = []
    ccounter = 0
    bar = tqdm(total=total or None, unit="comment", leave=False,
                  desc=f"{video_id[:11]}", dynamic_ncols=True)

    while results:
        for item in results.get("items", []):
            top_level_comment = item.get("snippet", {}).get("topLevelComment", {})
            comment_snippet = top_level_comment.get("snippet")
            if comment_snippet is None:
                continue

            comment_data = {
                "youtube_comment_id": item.get("id", ""),
                "text"              : comment_snippet.get("textDisplay", ""),
                "author_name"       : ident.token(comment_snippet.get("authorDisplayName", "")),
                "date"              : comment_snippet.get("publishedAt", ""),
                "likes"             : comment_snippet.get("likeCount", 0),
                "replies"           : []
            }

            if item["snippet"].get("totalReplyCount", 0) > 0:
                reply_kwargs = {
                    "parentId"  : item.get("id", ""),
                    "part"      : "snippet",
                    "maxResults": REPLY_PAGE_SIZE,
                    "textFormat": "plainText"
                }

                while True:
                    try:
                        reply_results = fetch(youtube.comments().list, reply_kwargs)
                    except HttpError as e:
                        return classify(e, "replies")
                    except Exception as e:
                        logger.error(f"Unexpected error retrieving replies for video ID "
                                     f"{video_id}, will retry next run: {e}.")
                        return None, False, FAILED

                    for reply in reply_results.get("items", []):
                        reply_snippet = reply.get("snippet", {})
                        author_channel_id = ""
                        if isinstance(reply_snippet.get("authorChannelId"), dict):
                            author_channel_id = reply_snippet["authorChannelId"].get("value", "")
                        ident.remember([author_channel_id])
                        sanitize.remember_name(reply_snippet.get("authorDisplayName"),
                                               ident.token(author_channel_id))

                        comment_data["replies"].append({
                            "youtube_reply_id" : reply.get("id", ""),
                            "youtube_parent_id": reply_snippet.get("parentId", ""),
                            "text"             : reply_snippet.get("textDisplay", ""),
                            "date"             : reply_snippet.get("publishedAt", ""),
                            "likes"            : reply_snippet.get("likeCount", 0),
                            "author_name"      : ident.token(reply_snippet.get("authorDisplayName", "")),
                            "author_channel_id": ident.token(author_channel_id)
                        })
                        bar.update(1)

                    if "nextPageToken" not in reply_results:
                        break
                    reply_kwargs["pageToken"] = reply_results["nextPageToken"]

            author_channel_id = ""
            if isinstance(comment_snippet.get("authorChannelId"), dict):
                author_channel_id = comment_snippet["authorChannelId"].get("value", "")
            if author_channel_id:
                comment_data["author_channel_id"] = ident.token(author_channel_id)
                pending_authors.append((comment_data, author_channel_id))
                sanitize.remember_name(comment_snippet.get("authorDisplayName"),
                                       ident.token(author_channel_id))

            comments.append(comment_data)
            ccounter += 1
            bar.update(1)

        if "nextPageToken" not in results:
            break
        kwargs["pageToken"] = results["nextPageToken"]
        try:
            results = fetch(youtube.commentThreads().list, kwargs)
        except HttpError as e:
            return classify(e, "further comment pages")
        except Exception as e:
            logger.error(f"Unexpected error paging comments for video ID {video_id}, "
                         f"will retry next run: {e}.")
            return None, False, FAILED

    if pending_authors:
        authors, quota_exceeded = get_channel_data_batch(
            youtube, [channel_id for _, channel_id in pending_authors])
        if quota_exceeded:
            logger.error(f"API quota exceeded while retrieving channel data "
                         f"for video ID {video_id}.")
            return None, True, FAILED
        for comment_data, channel_id in pending_authors:
            author_data = authors.get(channel_id)
            if author_data:
                comment_data.update({k: v for k, v in author_data.items()
                                     if k != "author_name" or not comment_data.get("author_name")})

    replies = sum(len(c.get("replies") or ()) for c in comments)
    bar.close()
    print(f"{ccounter:,} comments and {replies:,} replies downloaded for this video.")
    logger.info(f"{ccounter} comments downloaded for video ID {video_id}.")

    return comments, False, OK
