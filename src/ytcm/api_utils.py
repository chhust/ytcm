import logging
logger = logging.getLogger(__name__)


from ytcm import platform_utils as platform

from datetime                  import datetime, timezone
from dateutil.parser           import isoparse                  # python-dateutil
from googleapiclient.discovery import build                     # google-api-python-client
from googleapiclient.errors    import HttpError                 # google-api-python-client

from ytcm import identity as ident
from ytcm.config import ID_BATCH_SIZE
from ytcm.helper_utils import safeint, is_quota_exceeded

def load_api_key(file_path):

    try:
        if platform.private_file_is_readable_by_others(file_path):
            logger.warning(f"'{file_path}' is readable by other users. "
                           f"{platform.make_file_private(file_path)}")

        with open(file_path, "r", encoding="utf-8") as handle:
            return handle.read().strip()

    except FileNotFoundError:
        logger.error(f"Error: Could not find '{file_path}'.")
        return None

    except (OSError, UnicodeDecodeError) as e:
        logger.error(f"Could not read the API key from '{file_path}': {e}.")
        return None


def init_youtube_service(api_key):
    """
    Create and return a service object for interaction with the YouTube Data API.
    "build" is a function provided by "googleapiclient.discovery".
    """
    
    return build("youtube", "v3", developerKey=api_key)


def get_video_ids(search_terms, youtube, **kwargs):
    """
    Download video IDs.
    """

    video_ids = set()
    excluded_terms = [term.lower() for term in kwargs.get("excluded_terms", [])]
    search_year = kwargs.get("year")

    if search_year is None:
        print("Search year is not specified.")
        return []

    for terms_list in search_terms:
        page_token = None

        while True:
            try:
                search_response = youtube.search().list(
                    q=" ".join(terms_list),
                    type="video",
                    pageToken=page_token,
                    part="snippet",
                    maxResults=kwargs.get("maxResults", 50),
                    publishedAfter=f"{search_year}-01-01T00:00:00Z",
                    publishedBefore=f"{search_year + 1}-01-01T00:00:00Z",
                ).execute()

                for search_result in search_response.get("items", []):
                    year = int(search_result["snippet"]["publishedAt"].split("-")[0])
                    title = search_result["snippet"]["title"].lower()
                    if (
                        year == search_year
                        and all(term.lower() in title for term in terms_list)
                        and not any(exclusion in title for exclusion in excluded_terms)
                    ):
                        video_ids.add(search_result["id"]["videoId"])

                page_token = search_response.get("nextPageToken")
                if not page_token:
                    break

            except HttpError as e:
                if is_quota_exceeded(e):
                    logger.error(f"API quota exceeded during the search for \'{' '.join(terms_list)}\'.")
                    return list(video_ids)
                logger.error(f"An error occurred with search request \'{' '.join(terms_list)}\': {e}. No results retrieved.")
                break
            except Exception as e:
                logger.error(f"An error occurred with search request \'{' '.join(terms_list)}\': {e}. No results retrieved.")
                break

    return list(video_ids)


def _video_record(item):
    snippet = item.get("snippet", {})
    statistics = item.get("statistics", {})
    content_details = item.get("contentDetails", {})

    published_at_raw = snippet.get("publishedAt", "")
    try:
        published_at = isoparse(published_at_raw).astimezone(timezone.utc).isoformat()
    except Exception:
        published_at = published_at_raw

    channel_id = snippet.get("channelId", "")
    ident.remember([channel_id])

    return {
        "title": snippet.get("title", ""),
        "description"  : snippet.get("description", ""),
        "published_at" : published_at,
        "channel_name" : ident.token(snippet.get("channelTitle", "")),
        "channel_id"   : ident.token(channel_id),
        "duration"     : content_details.get("duration", ""),
        "likes"        : safeint(statistics.get("likeCount", 0)),
        "dislikes"     : safeint(statistics.get("dislikeCount", 0)),
        "views"        : safeint(statistics.get("viewCount", 0)),
        "download_time": datetime.now(timezone.utc).isoformat(),
        "manual_review": False,
        "comment_count": safeint(statistics.get("commentCount", 0)),
    }


def get_video_information_batch(youtube, video_ids):

    wanted = [v for v in dict.fromkeys(video_ids) if v]
    found = {}
    unreachable = set()

    for start in range(0, len(wanted), ID_BATCH_SIZE):
        chunk = wanted[start:start + ID_BATCH_SIZE]
        try:
            response = youtube.videos().list(part="snippet,statistics,contentDetails",
                                             id=",".join(chunk)).execute()
        except HttpError as e:
            if is_quota_exceeded(e):
                logger.error(f"API quota exceeded while retrieving information for {len(chunk)} videos.")
                return found, True, unreachable
            logger.error(f"HTTP error retrieving video data: {e}")
            unreachable.update(chunk)
            continue
        except Exception as e:
            logger.error(f"Unexpected error retrieving video data: {e}")
            unreachable.update(chunk)
            continue

        for item in response.get("items", []):
            video_id = item.get("id")
            if video_id:
                found[video_id] = _video_record(item)

        for video_id in chunk:
            if video_id not in found:
                logger.warning(f"Video ID {video_id} not found or no longer available.")

    return found, False, unreachable


def get_video_information(youtube, video_id):

    data, quota_exceeded, _ = get_video_information_batch(youtube, [video_id])
    if quota_exceeded:
        return None, True
    return data.get(video_id), False
