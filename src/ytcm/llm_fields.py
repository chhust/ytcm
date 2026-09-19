LANGUAGE_FIELDS = ("llm_language", "language")
SENTIMENT_FIELDS = ("llm_sentiment", "vader_sentiment")
VIDEO_LANGUAGE_FIELDS = ("llm_title_language", "title_language")
VIDEO_DESCRIPTION_LANGUAGE_FIELDS = ("llm_description_language", "description_language")

UNRESOLVED = frozenset({"", "unknown", "N/A", "n/a"})


def is_missing(value):
    if value is None or value != value:
        return True
    return isinstance(value, str) and value.strip() in UNRESOLVED


def read(record, field):
    if record is None:
        return None
    try:
        return record.get(field)
    except AttributeError:
        return None


def first_present(record, fields, default=None):
    for field in fields:
        value = read(record, field)
        if not is_missing(value):
            return value
    return default


def language_of(record, default=None):
    return first_present(record, LANGUAGE_FIELDS, default)


def sentiment_of(record, default="N/A"):
    return first_present(record, SENTIMENT_FIELDS, default)


def sentiment_label_of(record, default=None):
    return first_present(record, ("llm_sentiment_label",), default)


def sentiment_confidence_of(record, default=None):
    return first_present(record, ("llm_sentiment_conf",), default)


def video_info_of(entry):
    if entry is None:
        return None
    info = read(entry, "video_info")
    return info if info is not None else entry


def llm_video_language_of(entry, default=None):
    return first_present(video_info_of(entry), ("llm_title_language",), default)


def video_language_of(entry, default=None):
    return first_present(video_info_of(entry), VIDEO_LANGUAGE_FIELDS, default)


def video_description_language_of(entry, default=None):
    return first_present(video_info_of(entry), VIDEO_DESCRIPTION_LANGUAGE_FIELDS, default)


def uses_llm_data(record):
    return not is_missing(read(record, "llm_language")) or not is_missing(read(record, "llm_sentiment"))
