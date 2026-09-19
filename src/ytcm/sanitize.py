import logging
logger = logging.getLogger(__name__)

import re
import unicodedata
from collections import Counter

from ytcm import config
from ytcm import identity as ident

SIGILS = "@+"
FULLWIDTH_AT = "＠"
MENTION_PREFIX = "Mentioned"
REDACTED_USER = "[user]"
REDACTED_EMAIL = "[email]"
EMAIL_PREFIX = "Mail"
REDACTED_CHANNEL = "[channel]"

MAX_MENTION_WORDS = 4
MENTION_WINDOW = 80
MINIMUM_CANDIDATE_LENGTH = 2
MINIMUM_PREFIX_LENGTH = 4
MAXIMUM_NAME_LENGTH = 40
RUN_ON_LENGTH = 20
HONORIFIC_WINDOW = 20
TRAILING_PUNCTUATION = ".,!?:;)]}>\"'…、。！？，"

EMAIL_PATTERN = re.compile(r"[\w.+%-]+@[\w-]+\.[\w.-]*[A-Za-z]")
CHANNEL_ID_PATTERN = re.compile(r"\bUC[A-Za-z0-9_-]{20,}")
CJK_PATTERN = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
HONORIFIC_PATTERN = re.compile(r"(さん|ちゃん|くん|君|様|씨|님)")
WORD_PATTERN = re.compile(r"\S+")
LEGACY_TOKEN_PATTERN = re.compile(r"(?:User|Channel|Mentioned)\d+(?!\d)")

GLUED_HANDLE = re.compile(r"([A-Za-z][A-Za-z0-9._-]*[_\-0-9][A-Za-z0-9._-]*)")

URL_HANDLE = re.compile(
    r"((?:youtube\.com|youtu\.be)/(?:c|user)/"
    r"|(?:instagram|twitter|facebook|tiktok|threads)\.(?:com|net)/@?"
    r"|x\.com/@?"
    r"|t\.me/)"
    r"([A-Za-z0-9._%\-\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]{2,})", re.I)

INVISIBLE = "".join((
    "­", "͏", "؜", "ᅟ", "ᅠ", "឴", "឵",
    "᠎", "​", "‌", "‍", "‎", "‏", " ",
    " ", "‪", "‫", "‬", "‭", "‮", "⁠",
    "⁡", "⁢", "⁣", "⁤", "⁦", "⁧", "⁨",
    "⁩", "⁪", "⁫", "⁬", "⁭", "⁮", "⁯",
    "ㅤ", "︀", "﻿", "ﾠ",
))
INVISIBLE_TABLE = {ord(c): None for c in INVISIBLE}


def strip_invisible(text):
    return text.translate(INVISIBLE_TABLE)


API_KEY_PATTERN = re.compile(r"(?:AIza[0-9A-Za-z_\-]{35})|(?<=[?&]key=)[0-9A-Za-z_\-]{20,}")


def scrub(text):

    if not text:
        return text
    text = str(text)
    if "AIza" in text or "key=" in text:
        text = API_KEY_PATTERN.sub("[api-key]", text)
    if "UC" in text:
        text = CHANNEL_ID_PATTERN.sub(_scrub_channel, text)
    if "@" in text:
        text = EMAIL_PATTERN.sub(REDACTED_EMAIL, text)
    return text


def _scrub_channel(match):
    try:
        return ident.token(match.group(0))
    except ident.NoKey:
        return REDACTED_CHANNEL


def normalize(name):

    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name))
    text = strip_invisible(text)
    text = text.lstrip(SIGILS + FULLWIDTH_AT)
    text = text.strip().strip(TRAILING_PUNCTUATION).strip()
    return " ".join(text.casefold().split())


def key_pair(name):

    canonical = normalize(name)
    if not canonical:
        return ()
    joined = canonical.replace(" ", "")
    return (canonical,) if joined == canonical else (canonical, joined)


def name_keys(name):

    keys = list(key_pair(name))
    words = normalize(name).split()
    for count in range(1, len(words)):
        for prefix in key_pair(" ".join(words[:count])):
            if prefix not in keys:
                keys.append(prefix)
    for key in list(keys):
        if CJK_PATTERN.search(key):
            honorific = HONORIFIC_PATTERN.search(key)
            if honorific and honorific.start() >= MINIMUM_CANDIDATE_LENGTH:
                for trimmed in key_pair(key[:honorific.start()]):
                    if trimmed not in keys:
                        keys.append(trimmed)
    return keys


def name_hash(key):
    return ident.token(key)


def canonical_key(name):

    return normalize(name).replace(" ", "")


def mention_token(name):

    key = canonical_key(name)
    return MENTION_PREFIX + name_hash(key) if key else ""


def is_email_token(text):
    return text.startswith(EMAIL_PREFIX) and ident.is_token(text[len(EMAIL_PREFIX):])


def is_mention_token(text):
    return (text.startswith(MENTION_PREFIX)
            and ident.is_token(text[len(MENTION_PREFIX):]))


class MentionMap:

    def __init__(self, conn=None):
        self.conn = conn
        self.known = set()
        self.minted = Counter()
        if conn is not None:
            self.known = {row[0] for row in conn.execute("SELECT name_hash FROM mention_map")}

    def register(self, name, person_token):
        if not name or not person_token:
            return 0
        rows = []
        for key in name_keys(name):
            digest = name_hash(key)
            rows.append((digest, person_token))
            self.known.add(digest)
        if self.conn is not None and rows:
            self.conn.executemany(
                "INSERT OR IGNORE INTO mention_map (name_hash, person_token) VALUES (?,?)",
                rows)
        return len(rows)

    def holds(self, key):
        return bool(key) and name_hash(key) in self.known

    def resolve(self, digest):
        if self.conn is None:
            return set()
        return {row[0] for row in self.conn.execute(
            "SELECT person_token FROM mention_map WHERE name_hash = ?", (digest,))}


def _visible(raw):

    kept = []
    offsets = []
    for index, character in enumerate(raw):
        if character not in INVISIBLE:
            kept.append(character)
            offsets.append(index)
    offsets.append(len(raw))
    return "".join(kept), offsets


def _candidate(window):
    match = WORD_PATTERN.match(window)
    if not match:
        return "", 0, []
    raw = match.group(0)
    visible, offsets = _visible(raw)
    trimmed = visible.rstrip(TRAILING_PUNCTUATION)
    if len(trimmed.strip()) < MINIMUM_CANDIDATE_LENGTH:
        return "", 0, []
    if not any(c.isalnum() for c in trimmed):
        return "", 0, []
    return trimmed, offsets[len(trimmed)], offsets


def _longest_known(window, known):

    ends = [m.end() for m in WORD_PATTERN.finditer(window)][:MAX_MENTION_WORDS]
    for end in reversed(ends):
        span = window[:end]
        key = normalize(span)
        if key and known.holds(key):
            return key, end
        joined = key.replace(" ", "")
        if joined and joined != key and known.holds(joined):
            return joined, end
    return "", 0


def _run_on_prefix(head, known):
    for size in range(min(len(head), MAXIMUM_NAME_LENGTH), MINIMUM_PREFIX_LENGTH - 1, -1):
        key = normalize(head[:size])
        if key and known.holds(key):
            return key, size
    return "", 0


class Sanitizer:

    def __init__(self, mentions, policy=None):
        self.mentions = mentions
        self.policy = policy or getattr(config, "MENTION_UNKNOWN_POLICY", "label")
        self.counts = Counter()

    def _replace_ids(self, text):
        if "UC" not in text:
            return text

        def substitute(match):
            self.counts["channel ids hashed"] += 1
            return ident.token(match.group(0))

        return CHANNEL_ID_PATTERN.sub(substitute, text)

    def _replace_urls(self, text):
        if "/" not in text:
            return text

        def substitute(match):
            prefix, segment = match.group(1), match.group(2)
            if is_mention_token(segment) or ident.is_token(segment) \
                    or LEGACY_TOKEN_PATTERN.fullmatch(segment):
                return match.group(0)
            token = mention_token(segment)
            if not token:
                return match.group(0)
            self.counts["identifiers in a URL path hashed"] += 1
            return prefix + token

        return URL_HANDLE.sub(substitute, text)

    def _replace_emails(self, text):

        if "@" not in text:
            return text
        if getattr(config, "EMAIL_POLICY", "hash") == "redact":
            cleaned, replaced = EMAIL_PATTERN.subn(REDACTED_EMAIL, text)
            if replaced:
                self.counts["e-mail addresses redacted"] += replaced
            return cleaned

        found = []

        def substitute(match):
            found.append(match.group(0))
            return EMAIL_PREFIX + ident.token(match.group(0).casefold())

        cleaned = EMAIL_PATTERN.sub(substitute, text)
        if found:
            self.counts["e-mail addresses hashed"] += len(found)
            ident.remember([e.casefold() for e in found], kind="email")
        return cleaned

    def _mention(self, window):
        head, consumed, offsets = _candidate(window)
        if not head:
            return None, 0
        legacy = LEGACY_TOKEN_PATTERN.match(head)
        if legacy or is_mention_token(head):
            rest = head[legacy.end():] if legacy else ""
            tail = GLUED_HANDLE.match(rest) if len(rest) >= 3 else None
            if tail and len(tail.group(0)) >= 3:
                end = legacy.end() + tail.end()
                token = mention_token(tail.group(0))
                if token:
                    self.counts["handle glued to an older token, hashed"] += 1
                    return head[:legacy.end()] + " @" + token, offsets[end]
            self.counts["already sanitized, left alone"] += 1
            return None, 0

        key, covered = _longest_known(window, self.mentions)
        if key:
            self.counts["resolved against a known name"] += 1
            return mention_token(key), covered

        run_on = CJK_PATTERN.search(head) and len(head) > RUN_ON_LENGTH
        if run_on:
            key, size = _run_on_prefix(head, self.mentions)
            if key:
                self.counts["resolved as a run-on prefix"] += 1
                return mention_token(key) or None, offsets[size]
            honorific = HONORIFIC_PATTERN.search(head[:HONORIFIC_WINDOW])
            if honorific and honorific.start() >= MINIMUM_CANDIDATE_LENGTH:
                end = honorific.end() if CJK_PATTERN.search(head) else honorific.start()
                token = mention_token(head[:end])
                if token:
                    self.counts["cut at an honorific, unresolved"] += 1
                    return token, offsets[end]

        if self.policy == "keep":
            self.counts["unknown name, left alone"] += 1
            return None, 0
        if self.policy == "redact":
            self.counts["unknown name, redacted"] += 1
            return REDACTED_USER, consumed

        token = mention_token(head)
        if not token:
            self.counts["not a name, left alone"] += 1
            return None, 0
        self.counts["unknown name, hashed unresolved"] += 1
        return token, consumed

    def clean(self, text):
        if not text:
            return text
        cleaned = self._replace_urls(self._replace_emails(self._replace_ids(str(text))))
        if not any(sigil in cleaned for sigil in SIGILS + FULLWIDTH_AT):
            return cleaned

        pieces = []
        position = 0
        length = len(cleaned)
        while position < length:
            character = cleaned[position]
            following = position + 1
            usable = following < length and not cleaned[following].isspace()
            word_start = position == 0 or cleaned[position - 1].isspace()
            sigil = character in SIGILS or character == FULLWIDTH_AT
            if sigil and usable and (word_start or character != "+"):
                window = cleaned[following:following + MENTION_WINDOW]
                replacement, consumed = self._mention(window)
                if replacement is not None:
                    pieces.append("@" + replacement)
                    position += 1 + consumed
                    continue
            pieces.append(character)
            position += 1

        return "".join(pieces)


_pending = []
_maps = {}


def remember_name(name, person_token):

    if name and person_token:
        _pending.append((str(name), person_token))


def forget_pending():
    _pending.clear()


def map_for(conn):
    mentions = _maps.get(id(conn))
    if mentions is None or mentions.conn is not conn:
        mentions = MentionMap(conn)
        _maps.clear()
        _maps[id(conn)] = mentions
    return mentions


def release(conn):
    _maps.pop(id(conn), None)


def flush_names(conn):
    if not _pending:
        return 0
    mentions = map_for(conn)
    written = 0
    for name, person_token in _pending:
        written += mentions.register(name, person_token)
    ident.remember_as(
        [(name_hash(key), name) for name, _ in _pending for key in name_keys(name)],
        kind="name")
    _pending.clear()
    return written


def texts_of(comments):
    for comment in comments:
        yield comment
        for reply in comment.get("replies") or []:
            yield reply


IDENTITY_FIELDS = frozenset({
    "channel_title", "channel_name", "channel_id", "uploader", "uploader_id",
    "author_name", "author_channel_id", "author_channel_url", "channel_url",
})

SKIP_FIELDS = frozenset({"replies", "text", "title", "description"})


def _walk(value, clean):
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, list):
        return [_walk(v, clean) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v, clean) for k, v in value.items()}
    return value


def clean_extras(record, known, clean):

    for field, value in list(record.items()):
        if field in known or field in SKIP_FIELDS or not isinstance(value, (str, list, dict)):
            continue
        record[field] = _walk(value, clean)


def prepare(conn, info, comments, policy=None):

    flush_names(conn)
    sanitizer = Sanitizer(map_for(conn), policy)

    from ytcm.bridge import MESSAGE_COLUMNS, VIDEO_COLUMNS

    if info:
        for field in ("title", "description"):
            if info.get(field):
                info[field] = sanitizer.clean(info[field])
        clean_extras(info, set(VIDEO_COLUMNS), sanitizer.clean)

    for record in texts_of(comments or []):
        if record.get("text"):
            record["text"] = sanitizer.clean(record["text"])
        clean_extras(record, set(MESSAGE_COLUMNS), sanitizer.clean)

    return sanitizer.counts
