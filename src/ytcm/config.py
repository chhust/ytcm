"""
Part I: Settings
"""


PRIMARY_SEARCH_TERMS = [["primary"]]

SECONDARY_SEARCH_TERMS = ["secondary"]

EXCLUDED_TERMS = []

SEARCH_YEAR = 2024

DB_PATH        = "corpus.db"
DEFAULT_CORPUS = "A"

API_KEY_FILE  = "YOUTUBE.API"
HASH_KEY_FILE = "ytcm.key"

LOOKUP_DIR = None

MENTION_UNKNOWN_POLICY = "label"

EMAIL_POLICY = "hash"

LANGUAGE_MIN_CHARS      = 10
LANGUAGE_MIN_CONFIDENCE = 0.90

CACHING = True

TUBESCOPE_MODE = "cr"

TUBECONNECTSETTINGS = [
    ({"language": "en"}, "EN", "English"),
    ({"language": "ja"}, "JP", "Japanese"),
]

TUBECONNECT_CAP_BASIS = "comment"

EMBEDDINGS = "ollama:qwen3-embedding:0.6b"
OLLAMA_URL = "http://localhost:11434"

CONFIRM_ABOVE_SECONDS = 60


"""
Part II

Don't touch, don't change. Hic sunt dracones :)
"""

RETRY_ATTEMPTS        = 3
RETRY_BACKOFF_SECONDS = 2.0

CHECKPOINT_EVERY = 50

SEARCH_PAGE_SIZE  = 50
COMMENT_PAGE_SIZE = 100
REPLY_PAGE_SIZE   = 100
ID_BATCH_SIZE     = 50

EMBEDDING_BATCH                 = 128
SEMSEARCH_TOP_K                 = 100
SEMSEARCH_PREVIEW               = 20
SEMSEARCH_RANKING_WARNING_ABOVE = 1000

GRAPH_BAND_CELLS = 50_000_000

TUBECONNECT_BURN_IN_MULTIPLE = 20
TUBECONNECT_THIN_MULTIPLE    = 1

CACHE_FILE      = "channel_data_cache.json"
COMMENTS_FOLDER = "COMMENTS"

COMMENTS_JSON          = "Comments.json"
ANONYMIZATION_MAP_JSON = "Anonymization_map.json"
COMMENTS_HTML          = "Comments.html"
COMMENTS_GEXF_ON       = "Comments+replies.gexf"
COMMENTS_GEXF_OFF      = "Comments.gexf"
COMMENTS_CSV           = "Comments.csv"
FILTER_JSON            = "Filtered.json"
MARKERS_TXT            = "Markers.txt"

FALLBACK_VERSION = "0.1.0"


def tool_version():
    try:
        from importlib.metadata import version, PackageNotFoundError
    except ImportError:
        return FALLBACK_VERSION
    try:
        return version("ytcm")
    except PackageNotFoundError:
        return FALLBACK_VERSION


VERSION = tool_version()
