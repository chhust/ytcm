import logging
logger = logging.getLogger(__name__)

import json
import os

from ytcm.config import CACHE_FILE, CACHING
from ytcm.helper_utils import safe_write_json

def load_channel_data_cache(filename=CACHE_FILE):

    if not CACHING:
        return {}

    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError as e:
            logger.error(f"Error: JSON data in cache file '{filename}' is corrupt: {e}.")
        except (OSError, UnicodeDecodeError) as e:
            logger.error(f"Could not read the cache file '{filename}': {e}.")
    else:
        logger.info(f"Cache file '{filename}' does not exist. Building a new cache.")
    return {}


def save_channel_data_cache(cache, filename=CACHE_FILE):

    if not CACHING:
        return

    safe_write_json(cache, filename)
