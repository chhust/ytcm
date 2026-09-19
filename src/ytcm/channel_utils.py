import logging
logger = logging.getLogger(__name__)

from googleapiclient.errors import HttpError

from ytcm.cache_utils import load_channel_data_cache, save_channel_data_cache
from ytcm.config import ID_BATCH_SIZE
from ytcm.helper_utils import is_quota_exceeded
from ytcm import identity as ident
from ytcm import sanitize as sanitize

_cache = None


def _open_cache():
    global _cache
    if _cache is None:
        _cache = load_channel_data_cache()
    return _cache


def save_cache():

    if _cache is not None:
        save_channel_data_cache(_cache)


def _unknown(channel_id):
    return {"author_name": "Unknown", "author_channel_id": ident.token(channel_id),
            "author_subscribers": 0}


def get_channel_data(youtube, channel_id):

    data, quota_exceeded = get_channel_data_batch(youtube, [channel_id])
    if quota_exceeded:
        return None, True
    return data.get(channel_id, _unknown(channel_id)), False


def get_channel_data_batch(youtube, channel_ids):

    cache = _open_cache()
    wanted = [channel_id for channel_id in dict.fromkeys(channel_ids) if channel_id]
    ident.remember(wanted)
    missing = [channel_id for channel_id in wanted if ident.token(channel_id) not in cache]

    for start in range(0, len(missing), ID_BATCH_SIZE):
        chunk = missing[start:start + ID_BATCH_SIZE]
        request_failed = False
        try:
            response = youtube.channels().list(id=",".join(chunk), part="snippet,statistics").execute()
        except HttpError as e:
            if is_quota_exceeded(e):
                logger.error(f"API quota exceeded while retrieving data for {len(chunk)} channels.")
                return {}, True
            logger.error(f"HTTP error while retrieving channel data: {e}.")
            response = {"items": []}
            request_failed = True
        except Exception as e:
            logger.error(f"Unexpected error while retrieving channel data: {e}.")
            response = {"items": []}
            request_failed = True

        returned = set()
        for item in response.get("items", []):
            channel_id = item.get("id")
            if not channel_id:
                continue
            returned.add(channel_id)
            snippet = item.get("snippet", {})
            statistics = item.get("statistics", {})
            sanitize.remember_name(snippet.get("title"), ident.token(channel_id))
            cache[ident.token(channel_id)] = {
                "author_name"       : ident.token(snippet.get("title", "N/A")),
                "author_channel_id" : ident.token(channel_id),
                "author_subscribers": statistics.get("subscriberCount", 0)
            }

        if not request_failed:
            for channel_id in chunk:
                if channel_id not in returned:
                    logger.warning(f"No data found for channel {ident.token(channel_id)}.")
                    cache[ident.token(channel_id)] = _unknown(channel_id)

    return {channel_id: cache[ident.token(channel_id)] for channel_id in wanted if ident.token(channel_id) in cache}, False
