**This file has been automatically generated using Claude Opus 5 on Sept 19, 2026. Please inform us when you find any factual errors.**


# YTCM102 — Technical Reference

What is in the repository, how the data is laid out, and what every function does. For using the tool, see `YTCM101.md`.


## 1. The repository

Thirty-seven files ship: nine in the root, twenty-eight in `src/ytcm/`.

```
YTCM.py                  the shell: logging, the command class, the entry point
pyproject.toml           package metadata, dependencies, the `ytcm` entry point
requirements.txt         the same dependencies for a plain pip install
uv.lock                  pinned versions
README.md                what the tool is and what it will not do
YTCM101.md               the user manual
YTCM102.md               this file
YOUTUBE.API.example      a placeholder showing the shape of the key file
.gitignore               refuses everything, then names what may pass

src/ytcm/
    __init__.py          empty marker
    config.py            settings, editable at the top, fixed at the bottom
    db.py                schema, connection, indexes, download state, provenance
    repo.py              every read of the corpus that analysis needs
    bridge.py            import and export between the corpus and exchange files
    identity.py          the key, tokens, and the re-identification store
    sanitize.py          finding and hashing identifiers inside text
    api_utils.py         the YouTube search and video endpoints
    channel_utils.py     the YouTube channel endpoint, with a cache
    comments_utils.py    the comment and reply endpoints, with retries
    processing_utils.py  the crawl loop
    data_enrichment_utils.py  language detection, sentiment, manual review
    embed.py             embedding vectors and semantic search
    filter_utils.py      substring and whole-word filtering in memory
    export_utils.py      HTML, CSV and GEXF writers
    io_utils.py          paths inside the working directory, deleting exports
    cache_utils.py       reading and writing the author cache
    helper_utils.py      retries, JSON files, duplicate detection, small tools
    platform_utils.py    file modes, filenames, console encoding, plurals
    logging_config.py    the log filter that scrubs identifiers
    font_utils.py        picking fonts that can draw the corpus
    llm_fields.py        reading annotation fields in a fixed order of preference
    results.py           saving figures and tables from a run
    tubescope.py         counts, distributions, time series
    tubetalk.py          language, word clouds, topic models
    tubegraph.py         channel networks
    tubeconnect.py       overlap between corpora
    shell_commands.py    the command methods, grouped into mix-in classes
```

The import direction is one way. `config.py` imports nothing of ours. `db.py` imports `config`. `repo.py` and `bridge.py` import `db`. The analysis modules import `repo`. `shell_commands.py` imports everything. `YTCM.py` imports `shell_commands.py`. Nothing imports upward, and the only cycles that would exist are broken with imports placed inside functions, marked where they occur.


## 2. Data structures


### 2.1 The corpus, `corpus.db`

SQLite, in WAL mode. The schema is in `db.DDL` and is applied on every open with `CREATE TABLE IF NOT EXISTS`, so opening an old corpus upgrades it.

**`videos`** — one row per video. `video_id` is the primary key, the YouTube ID. Then `title`, `description`, `published_at` (ISO 8601, UTC), `channel_id` and `channel_name` (both tokens after pseudonymization), `duration` (ISO 8601 duration), `likes`, `dislikes`, `views`, `download_time`, `manual_review`, `comments_status`, and `position` for the order the video was crawled in.

**`video_corpora`** — which corpus tags a video belongs to. `(video_id, corpus)` is the key, so one video can sit in several corpora and is stored once. `position` is its order within that corpus.

**`messages`** — comments and replies in one table. `message_id` is the primary key, the YouTube comment or reply ID. `kind` is `comment` or `reply`; a reply carries its comment in `parent_id`. Then `video_id`, `position`, `text`, `date`, `likes`, `author_channel_id`, `author_name`, `author_subscribers`, and the enrichment columns: `language` and `language_source`, `blob_sentiment` and its source, `vader_sentiment` and its source.

**`annotations`** and **`video_annotations`** — anything a source produced that has no column of its own, keyed by `(message_id, dimension, source)` and `(video_id, dimension, source)`. Values are JSON. This is where fields from an imported file end up when the schema does not know them, and where language verdicts from an LLM sit beside the ones from langdetect.

**`channels`** — `channel_id`, `name`, `subscribers`, `anon_token`, and after a `migratehash` also `legacy_token`.

**`download_state`** — one row per video: `status` (`pending`, `done`, `unavailable`), `attempts`, `last_error`, `updated_at`, and the `corpus` it was queued for. This is what makes a crawl resumable.

**`embeddings`** — `(message_id, model)` as the key, with `dimensions`, the `vector` as a BLOB of int8, and `text_hash`. The hash is what makes a vector stale when the text changes.

**`embedding_state`** — one row per model: provider, dimensions, how many records were written, when it started and last moved.

**`mention_map`** — `name_hash` to `person_token`, so an @-mention of a display name can be resolved to the same token as the channel it belongs to.

**`meta`** — key/value. Holds `key_fingerprint`, which is what stops two corpora made with different keys from being mixed.

**`provenance`** — one row per pass that wrote anything: which pass, tool version, provider and model, start and end, how many records, which corpus, and the parameters as JSON.

Twelve indexes are defined in `db.INDEXES`. They are dropped before a bulk import and rebuilt after it, and `status` rebuilds any that went missing.


### 2.2 Fields SQLite has no type for

`author_subscribers`, `blob_sentiment`, `vader_sentiment` and `manual_review` are stored as JSON text, listed in `bridge.JSON_COLUMNS`. They go through `json.dumps` on the way in and `json.loads` on the way out, so `None`, a number and a string stay distinguishable.


### 2.3 The exchange format

An object keyed by video ID:

```json
{
  "VIDEO_ID": {
    "video_info": {"title": "...", "published_at": "...", "channel_id": "U1a2b...",
                   "views": 1234, "...": "..."},
    "comments": [
      {"youtube_comment_id": "...", "text": "...", "author_channel_id": "U...",
       "author_name": "U...", "date": "...", "likes": 3,
       "replies": [{"youtube_reply_id": "...", "youtube_parent_id": "...",
                    "text": "...", "...": "..."}]}
    ]
  }
}
```

JSONL is the same thing one video per line, either as `{"video_id": ..., ...}` or as a single-key object. Both are read by `bridge.read_corpus`, which decides from the first two lines which it is looking at.

Fields the schema does not know are not dropped. They go into `annotations` and come back out in the same place on export, so import and export are a round trip.


### 2.4 Keys, tokens and the store

`ytcm.key` is 32 random bytes, mode 600. A token is `"U" + HMAC-SHA256(key, value)[:20]` in lowercase hex, so the same value always gives the same token and the token cannot be inverted without the key.

The fingerprint is `HMAC-SHA256(key, "fingerprint")[:12]`. It is written into `meta` on first use and checked on every open. A mismatch is refused.

Three prefixes distinguish what a token stands for: a bare token is a channel, `Mentioned…` is a name found in text, `Mail…` an e-mail address.

If `LOOKUP_DIR` is set, `identities.db` in it maps tokens back to values, records every consultation, and carries its own copy of the fingerprint. Without it the pseudonymization is one-way.


### 2.5 The working directory

Everything is relative to the process's current directory. `io_utils.refuse_outside` rejects any path that would leave it, so an export cannot be written to an arbitrary place on the disk.

Written there: `corpus.db` (plus `-wal` and `-shm`), `ytcm.key`, `video_ids.txt`, `channel_data_cache.json`, `logs/`, `figures/<tool>/`, `output/`, the export files, `COMMENTS/`, and `Markers.txt`.


### 2.6 Settings, `config.py`

The file has two halves. The first is meant to be edited: search terms, `DB_PATH`, `DEFAULT_CORPUS`, the two key file names, `LOOKUP_DIR`, the mention and e-mail policies, the language thresholds, `CACHING`, `TUBESCOPE_MODE`, `TUBECONNECTSETTINGS`, `TUBECONNECT_CAP_BASIS`, the embedding model and its URL, and `CONFIRM_ABOVE_SECONDS`. The second half is marked as fixed and holds retry counts, page sizes, batch sizes, the graph band budget, the null model schedule, the file names, and the version fallback.

`VERSION` is read from the installed package metadata, so it cannot drift from `pyproject.toml`.


## 3. Every function

Grouped by module, in the order the modules depend on each other. Private helpers, the ones with a leading underscore, are included: they carry most of the logic.


### 3.1 `config.py`

**`tool_version()`** Reads the installed package version through `importlib.metadata`. Falls back to `FALLBACK_VERSION` when the package is not installed or the metadata is missing. Returns a string. Called once at import to set `VERSION`.


### 3.2 `db.py`

**`class NotADatabase(sqlite3.DatabaseError)`** Raised when the corpus file exists but is not SQLite. Carries a sentence saying what to do about it.

**`class ReadOnlyDatabase(sqlite3.DatabaseError)`** Raised when the corpus or its directory cannot be written. Reading needs to write, because opening applies the schema, so this is fatal rather than a degraded mode.

**`text_hash(text)`** Takes a string, returns a 16-character blake2s digest of its UTF-8 bytes. `None` hashes as the empty string. This is what decides whether an embedding is still current; it is not a security hash and does not use the key.

**`fold(text)`** Case-folds a string, returns anything else unchanged. Registered with SQLite as `PYFOLD` so that a LIKE-free substring search can be case-insensitive beyond ASCII.

**`get_conn(path=None)`** Opens the corpus, applies the pragmas, runs the schema, registers `PYHASH` and `PYFOLD`, and adds the `text_hash` column to `embeddings` if an older corpus has none, backfilling it. Returns the connection, or raises `ReadOnlyDatabase` or `NotADatabase` with a sentence that says what to do.

**`create_indexes(conn)`** Runs the twelve `CREATE INDEX IF NOT EXISTS` statements and commits. Called after a bulk import and after a crawl.

**`missing_indexes(conn)`** Compares the index names in `INDEXES` against the ones SQLite reports. Returns the sorted list of missing names, empty when all are present.

**`drop_indexes(conn)`** Drops every index whose name starts with `idx_`. Used before a bulk import, inside the same transaction as the import so a refused import brings them back.

**`get_meta(conn, key, default=None)`** One lookup in the `meta` table. Returns the stored string or the default.

**`set_meta(conn, key, value)`** Writes a key/value pair into `meta`, replacing what was there, and commits.

**`check_fingerprint(conn, current)`** Compares the corpus's stored key fingerprint against the one in use. Stores it if the corpus has none yet. Raises `ValueError` on a mismatch, which is what stops two corpora made with different keys from being mixed. Returns the fingerprint in force.

**`mark_pending(conn, video_ids, corpus)`** Puts video IDs into `download_state` as pending, or updates the corpus tag of ones already there. Does not reset a status that is already `done`.

**`requeue(conn, video_ids, corpus)`** Like `mark_pending`, but forces the status back to pending even for finished videos. This is what `update` uses. Returns how many IDs were given.

**`mark_status(conn, video_id, status, error=None)`** Sets a video's download status, counts the attempt up, and stores the error message after running it through `sanitize.scrub`, so an identifier in an API error does not end up in the corpus.

**`pending_videos(conn, corpus=None)`** Returns the video IDs still pending, in insertion order, for one corpus tag or for all of them.

**`record_provenance(conn, pass_name, started_at, finished_at, records_written, corpus=None, provider=None, model=None, parameters=None)`** Writes one row into `provenance` with the tool version filled in. Returns the new `run_id`. Every pass that changes the corpus calls this.


### 3.3 `repo.py`

Every read the analysis modules need. All of them take an optional open connection and close their own if they made one.

**`resolve_mode(mode=None)`** Turns `None`, `c`, `r` or `cr` into one of those three, falling back to `TUBESCOPE_MODE`. Raises `ValueError` on anything else, naming the valid values.

**`_kinds(mode)`** Turns a mode into the list of `kind` values it covers: `['comment']`, `['reply']`, or both.

**`filters(mode=None, corpus=None, years=None, language=None, video_id=None)`** Builds the SQL fragments every other query shares: the joins, the WHERE clause, and the parameters. Returns them as a triple. Duplicate joins are collapsed, so asking for a corpus and a year range does not join `videos` twice.

**`_from_json(value)`** Decodes a JSON string, returns `None` for anything that is not a string.

**`_decode_frame(frame)`** Runs `_from_json` over every column of a DataFrame that is listed in `JSON_COLUMNS`. Returns the frame.

**`records_df(mode=None, corpus=None, years=None, language=None, fields=None, limit=None, conn=None)`** The main read: messages as a DataFrame, ordered by video, kind and position. Named annotation dimensions can be pulled in as extra columns through `fields`; where several sources annotated the same message, anything but `import` wins.

**`videos_df(corpus=None, years=None, fields=None, conn=None)`** Videos as a DataFrame, in corpus order when a corpus is named and in crawl order otherwise. Takes annotation dimensions through `fields` the same way.

**`video_stats_df(corpus=None, years=None, conn=None)`** One row per video with its metadata plus counted comments, counted replies, and how many of its comments got a reply. This is the frame most of TubeScope's per-video plots start from.

**`iter_texts(mode=None, corpus=None, years=None, language=None, batch=20000, conn=None)`** Yields `(message_id, text)` pairs in batches, paging by message ID rather than by OFFSET so the cost does not grow with the distance into the corpus.

**`participation_events(corpus=None, years=None, conn=None)`** Returns `(date, channel_id)` for every comment and reply that has both, with the video uploads prepended. Undated and unattributed rows are left out.

**`dates_by_kind(corpus=None, years=None, conn=None)`** Returns three lists of dates: uploads, comments, replies. Used for the weekday breakdown.

**`count_by(column, mode=None, corpus=None, years=None, language=None, conn=None)`** Counts messages grouped by one column of `messages`, most frequent first. Rejects a column that is not groupable, naming the ones that are.

**`count_over_time(unit='month', mode=None, corpus=None, years=None, language=None, conn=None)`** Counts messages per day, month or year by cutting the ISO date string to length. Returns a Series indexed by the bucket string. Messages without a date are left out.

**`effective_language_counts(mode=None, corpus=None, years=None, conn=None)`** Counts messages per language, preferring an LLM annotation over the stored langdetect verdict and ignoring "unknown" on either side. Each message is counted once even when several sources annotated it.

**`replies_per_comment(corpus=None, years=None, conn=None)`** Returns a list with one number per comment: how many replies it got.

**`filter_messages(terms, mode='or', corpus=None, years=None, conn=None, words=False)`** Finds messages containing the terms, in SQL. Substring matching goes through `PYFOLD` so it is case-insensitive beyond ASCII; whole-word matching registers a Python regex as a SQL function. Returns a DataFrame of the matches.

**`word_boundaries_apply(term)`** Says whether whole-word matching means anything for this term. False for terms written only in scripts without word breaks, such as Japanese or Chinese.

**`word_pattern(term)`** Compiles the regex used for whole-word matching, with lookarounds when word boundaries apply and a plain escaped pattern when they do not. Both the SQL path and the in-memory path use this, so they cannot drift apart.

**`terms_without_word_boundaries(terms)`** Returns the terms for which whole-word matching cannot apply, so the shell can say so instead of silently returning nothing.

**`_install_word_match(conn)`** Registers `ytcm_word_match` as a SQL function, caching one compiled pattern per term for the duration of the query.

**`_install_fold(conn)`** Registers `PYFOLD` on a connection that may not have it yet.

**`filter_videos_by_description(terms, mode='or', conn=None, words=False)`** The same matching against video descriptions. Returns the matching video IDs, so a video whose description matches is kept even if none of its comments do.

**`filter_as_nested(terms, mode='or', corpus=None, years=None, db_path=None, words=False)`** Runs both filters and rebuilds the result in the shape of the exchange format: matching videos, with only their matching comments, and under each comment only its matching replies. A comment is kept when it matches or when one of its replies does.

**`participation_by_video(roles=ALL_ROLES, corpus=None, years=None, conn=None)`** Returns the uploader of each video and the set of channels that took part in it, for the roles asked for. The basis of every network in TubeGraph.

**`channel_role_counts(corpus=None, years=None, conn=None)`** Per channel: how often it uploaded, commented and replied, and which videos it appears in. Returns a dict keyed by channel.

**`reply_author_pairs(include_self=False, corpus=None, years=None, conn=None)`** Returns `(parent author, reply author)` for every reply where both are known. Self-replies are dropped unless asked for.

**`corpus_summary(conn=None)`** One row per corpus tag with its video and message counts, biggest first.


### 3.4 `bridge.py`

Import and export. The corpus is the truth; an exchange file is a copy that must be able to come back without losing a field.

**`_now()`** Current UTC time as an ISO 8601 string. Used for the provenance rows.

**`_encode(value)`** / **`_decode(value)`** JSON in and out, passing `None` through untouched.

**`_pack(name, value)`** / **`_unpack(name, value)`** Apply `_encode` / `_decode` only to the columns listed in `JSON_COLUMNS`, leave the rest alone. Every read and write of a message or video goes through these.

**`_leftovers(record, known)`** Returns the fields of an incoming record that the schema has no column for. These become annotations instead of being dropped.

**`class MalformedCorpus(ValueError)`** Raised when an exchange file has the wrong shape. The message names the file, the video, and what was found instead of what was expected.

**`looks_like_jsonl(path)`** Decides whether a file is JSON Lines or one big JSON object, from the first line and the next non-empty one. Returns a bool and reads no more than it needs to.

**`read_corpus(path)`** Opens an exchange file in either format and returns `(pairs, count, streaming)`. JSONL is streamed one video at a time, so a file larger than memory can be imported; plain JSON is loaded whole because it has to be.

**`check_video_shape(video_id, entry, name='the file')`** Checks one video before it is written: that it is an object, that `video_info` and `comments` have the right types, and that every comment and reply is an object. Raises `MalformedCorpus` on the first problem. Returns True when the entry has no comment list at all, which is allowed and counted.

**`unreadable_date(value)`** True when a value is neither empty nor parseable as a date. Empty is not an error; nonsense is.

**`datish_fields(video_id, info, comments)`** Collects every unreadable date in one video, as `(where, value)` pairs, so the import can report them once at the end instead of per record.

**`fingerprint_beside(path)`** Reads the `.fingerprint` sidecar written next to an export, if there is one, and returns the key fingerprint it names. Used to refuse an import from a different key before any work is done.

**`import_json(path, corpus, mode='fill-missing', db_path=None, progress=None, sanitize_text=False, pseudonymize=False)`** The whole import. Checks the sidecar fingerprint, opens the corpus, and runs everything from `BEGIN IMMEDIATE` to one `commit` inside a single transaction, with the indexes dropped for the duration. A failure anywhere rolls the whole thing back, indexes included. Reports duplicate IDs, unreadable dates and missing comment lists at the end. Returns `(rows written, duplicates)`.

**`_pseudonymize_entry(info, comments, pseudonymize, sanitize_text)`** Replaces channel IDs and display names in one video with tokens, remembering the real values in the lookup store when there is one. Values that are already tokens are left alone, so a second pass changes nothing.

**`_write_video(conn, video_id, corpus, info, position, mode)`** Writes the video row and its corpus membership. `merge` overwrites every field, anything else fills only the ones that are still empty. Unknown fields go to `video_annotations`. Returns 1.

**`_write_messages(conn, video_id, comments, mode, seen, duplicates)`** Walks a video's comments and their replies, giving each a stable ID, falling back to a positional one when the file has none. Records IDs seen twice in the same file instead of writing them twice. Returns how many new rows were written.

**`_write_message(conn, message_id, video_id, parent_id, kind, position, record, skip, mode)`** Writes one comment or reply, following the same mode rule as `_write_video`, and records the author in `channels`. Unknown fields go to `annotations`.

**`_write_annotations(conn, table, key_column, key, fields, mode)`** Writes annotation rows under the source `import`. In `merge` the incoming value wins; otherwise an existing value is kept.

**`_video_rows(conn, corpus=None, years=None)`** The SELECT behind every export: video rows in corpus order or crawl order. Returns the cursor and the column names.

**`_annotations_for(conn, table, key_column, keys)`** Fetches annotations for a batch of up to 500 keys at a time and returns them as a dict of dicts. Batched because SQLite has a limit on how many parameters one statement may carry.

**`iter_videos(conn, corpus=None, years=None)`** Yields `(video_id, entry)` in the exchange shape, rebuilding `video_info` and the nested comments. Fields that are `None` are left out rather than written as null.

**`store_video(conn, video_id, corpus, info, comments, position)`** Writes one freshly crawled video: sanitizes it, writes the video and its messages in merge mode, and commits. This is the crawl's write path, one commit per video, so an interruption loses at most the video in flight.

**`renumber_positions(conn, corpus)`** Sorts a corpus by publication date and rewrites the position column, with undated videos last. Run at the end of a crawl so the order does not depend on the order of arrival.

**`export_json(path, corpus=None, years=None, jsonl=False, db_path=None, progress=None)`** Writes the corpus out as one JSON object or as JSON Lines, streaming video by video. Writes a `.fingerprint` sidecar beside it when the corpus was pseudonymized. Returns the number of videos written.

**`export_txt(folder, corpus=None, years=None, db_path=None, progress=None)`** Writes one readable text file per video: metadata, then comments with their replies indented. Control characters are stripped and filenames are made safe for Windows. Returns how many files were written.

**`_comments_for(conn, video_id)`** Rebuilds one video's comment tree from the flat `messages` table. Replies are hung under their comment; a reply whose comment is not in the corpus is counted and left out, with a warning naming how many.

**`migrate_legacy_map(path, db_path=None)`** Reads an old `Anonymization_map.json`, computes the keyed token for each real ID, and stores it beside the old `UserN` token so both still join. Records provenance and remembers the real IDs in the lookup store. Returns how many were migrated.


### 3.5 `identity.py`

The key, the tokens, and the optional way back.

**`class NoKey(Exception)`** Raised when an operation needs the key and there is none, or when the key file cannot be read or is too short to be one.

**`key_path()`** Where the key file is, read from the config at call time so a test or a second project can point it somewhere else.

**`have_key()`** True when the key file exists. Cheap: no read, no parse.

**`generate_key(path=None, replace=False)`** Writes 32 random bytes with mode 600, refusing to overwrite unless asked. Caches the key in the process and returns the new fingerprint.

**`delete_key(path=None)`** Removes the key file and forgets the cached copy. Returns whether a file was there. After this no token can be computed at all.

**`load_key(path=None)`** Returns the key bytes, reading the file once and caching them. Raises `NoKey` with a specific sentence when the file is missing, is not a file, cannot be read, or is shorter than 16 bytes.

**`forget_key()`** Drops the cached key so the next call reads the file again. Used by the tests and after a key is replaced.

**`token(value, path=None)`** The core operation: `"U"` plus the first 20 hex characters of HMAC-SHA256 over the value. Empty values are returned unchanged, so an absent channel does not become a pseudonym.

**`fingerprint(path=None)`** Twelve hex characters derived from the key alone. Identifies a key without revealing it, which is what the corpus and the export sidecar store.

**`is_token(value)`** Checks the shape: the prefix, the length, and hex digits throughout. This is what keeps a second pseudonymization from hashing tokens again.

**`lookup_path()`** The path of the re-identification store, or `None` when `LOOKUP_DIR` is unset.

**`lookup_available()`** True when a store exists on disk. `lookup` uses it to say what is missing.

**`open_lookup(create=False)`** Opens the store, creating the directory with mode 700, a README explaining what the directory is, and the database file with mode 600 when asked to. Checks the stored fingerprint against the key in use. Returns the connection or `None`.

**`_check_store_key(connection, path)`** Writes the fingerprint into a new store, or refuses an existing store that was written with a different key, closing the connection first.

**`remember(values, kind='channel')`** Stores `token -> value` pairs for later re-identification, ignoring duplicates. Silently does nothing when no store is configured. Returns how many rows were offered.

**`remember_as(pairs, kind)`** The same for pairs whose token was computed elsewhere, such as the name hashes the mention map uses.

**`_write(connection, rows)`** The insert behind both `remember` functions. `INSERT OR IGNORE`, then commit.

**`resolve(value, kind=None)`** Looks a token up. Returns the value, or a dict by kind when one token stands for several things, or `None`. Logs the consultation in the store, so the file also records who asked what.

**`delete_lookup()`** Deletes the store and its journal files. Reports which could not be removed because something still holds them open. Returns whether the main file went.


### 3.6 `sanitize.py`

Finding identifiers inside free text and replacing them with tokens. The hard part is @-mentions: a display name has no fixed length, may run into the next word, and may be written in a script with no spaces.

**`strip_invisible(text)`** Removes zero-width and formatting characters used to break up a name so it escapes a pattern. Returns the visible text.

**`scrub(text)`** The cheap pass used on anything about to be logged or stored as an error message: replaces API keys, raw channel IDs and e-mail addresses. Falls back to `[channel]` when there is no key to hash with.

**`_scrub_channel(match)`** Turns one matched channel ID into a token, or into `[channel]` when the key is gone.

**`normalize(name)`** Brings a display name to a comparable form: NFKC, invisible characters stripped, leading sigils and trailing punctuation removed, case-folded, runs of spaces collapsed.

**`key_pair(name)`** Returns the normalized name and, when it differs, the same name without spaces, so `Jane Doe` and `janedoe` both find the same person.

**`name_keys(name)`** Every form of a name worth matching: the pair above, each leading word prefix, and for CJK names the part before an honorific. Returns a list without duplicates.

**`name_hash(key)`** The token for one name key, computed with the same HMAC as a channel token.

**`canonical_key(name)`** The normalized name with spaces removed. The single key a mention token is built from.

**`mention_token(name)`** `Mentioned` plus the name hash, or the empty string when the name normalizes to nothing.

**`is_email_token(text)`** / **`is_mention_token(text)`** Shape checks for the two prefixed token kinds, so an already sanitized text is not sanitized twice.

**`class MentionMap`** The names the corpus knows, as hashes, with the person token behind each.

  **`__init__(self, conn=None)`** — loads the known hashes from `mention_map` into a set. Without a connection it works in memory only.

  **`register(self, name, person_token)`** — stores every key form of a name against a person token and adds them to the in-memory set. Returns how many rows.

  **`holds(self, key)`** — whether a name key is known. One hash and a set lookup.

  **`resolve(self, digest)`** — the person tokens behind a name hash, as a set.

**`_visible(raw)`** Splits a word into its visible characters and the offsets they came from, so a replacement can be measured against the original text including the invisible characters inside it.

**`_candidate(window)`** Takes the text after an `@` and returns the first word, trimmed of trailing punctuation, together with how much of the original it covers. Returns nothing for anything too short or without a letter or digit.

**`_longest_known(window, known)`** Tries the longest run of words first, up to four, and returns the first one the mention map knows. This is what matches `@Jane Doe` rather than only `@Jane`.

**`_run_on_prefix(head, known)`** For scripts without spaces: tries shrinking prefixes of a long word until one is a known name. Returns the key and how many characters it used.

**`class Sanitizer`** Cleans one text, counting what it did.

  **`__init__(self, mentions, policy=None)`** — takes a mention map and the policy for unknown names, defaulting to the configured one.

  **`_replace_ids(self, text)`** — replaces raw `UC…` channel IDs with tokens. Returns early when the text has no `UC` in it at all.

  **`_replace_urls(self, text)`** — replaces the handle in a YouTube, Instagram, X, TikTok, Threads, Facebook or Telegram URL with a mention token, leaving already tokenized ones alone.

  **`_replace_emails(self, text)`** — hashes e-mail addresses to `Mail…` tokens, or redacts them to `[email]`, depending on `EMAIL_POLICY`. Remembers the originals in the lookup store when hashing.

  **`_mention(self, window)`** — decides what one `@` is followed by: an already sanitized token, a known name, a run-on prefix, a name cut at an honorific, or an unknown name handled according to the policy. Returns the replacement and how many characters it consumes.

  **`clean(self, text)`** — the whole pass: IDs, e-mails, URLs, then a walk over the text replacing sigils that start a word. Returns the cleaned text.

**`remember_name(name, person_token)`** Queues a name and the token it belongs to. The crawl calls this while it downloads, before there is a transaction to write into.

**`forget_pending()`** Empties that queue. Used between runs and by the tests.

**`map_for(conn)`** Returns the mention map for a connection, building it once and caching it. A new connection invalidates the cache.

**`release(conn)`** Drops the cached map for a connection that is being closed.

**`flush_names(conn)`** Writes the queued names into `mention_map` and into the lookup store, then clears the queue. Returns how many rows were written.

**`texts_of(comments)`** Yields every comment and every reply in one flat pass, so callers do not each write the same nested loop.

**`_walk(value, clean)`** Applies a cleaning function to every string inside a nested structure of lists and dicts, leaving other types alone.

**`clean_extras(record, known, clean)`** Cleans the fields of a record that the schema does not know, skipping the ones handled elsewhere. This is what keeps an identifier from surviving in an unexpected field.

**`prepare(conn, info, comments, policy=None)`** The entry point: flush the queued names, build a sanitizer, clean the title, the description, every message text, and every unknown field. Returns the counts of what was replaced.


### 3.7 `api_utils.py`

**`load_api_key(file_path)`** Reads the key file, warns and tightens the mode if it is readable by others, and returns the key. Returns `None` and logs when the file is missing or unreadable.

**`init_youtube_service(api_key)`** Builds the Google API client for YouTube Data API v3. One line, kept separate so tests can replace it.

**`get_video_ids(search_terms, youtube, **kwargs)`** Runs one search per term combination, pages through the results, and keeps a video only when its title contains every term of the combination, the year matches, and no excluded term appears. Returns the IDs as a list. Stops and returns what it has when the quota runs out.

**`_video_record(item)`** Turns one API video item into the fields the corpus stores, converting the published date to UTC, the counts to integers, and the channel ID and title to tokens.

**`get_video_information_batch(youtube, video_ids)`** Fetches metadata in batches of 50. Returns `(found, quota_exceeded, unreachable)`. The third value holds the IDs of a batch whose request failed for a transient reason, which is what keeps a 503 from marking a video as gone.

**`get_video_information(youtube, video_id)`** One video, through the batch function. Returns `(record, quota_exceeded)`.


### 3.8 `channel_utils.py`

**`_open_cache()`** Loads the author cache once per process and returns it. An empty dict when caching is off.

**`save_cache()`** Writes the cache back to disk, if one was ever opened.

**`_unknown(channel_id)`** The placeholder record for a channel the API would not talk about: name "Unknown", the token, zero subscribers.

**`get_channel_data(youtube, channel_id)`** One channel, through the batch function. Returns `(record, quota_exceeded)`.

**`get_channel_data_batch(youtube, channel_ids)`** Fetches author names and subscriber counts in batches of 50, skipping anything already cached, and remembers the real IDs in the lookup store. A transient failure leaves those channels uncached rather than marking them unknown, so the next run tries again. Returns `(records by channel, quota_exceeded)`.


### 3.9 `comments_utils.py`

**`get_comments(youtube, total=None, **kwargs)`** Downloads one video's comments and replies, paging through both, tokenizing every author as it goes, and fetching the authors' channel data at the end in one batch. Transient errors are retried with a doubling delay; quota exhaustion, disabled comments and a missing comment section are each told apart and reported differently. Returns `(comments, quota_exceeded, status)` where status is `ok`, `disabled`, `not_found` or `failed`, and `comments` is `None` when the video should be tried again later.

Its three inner helpers: **`note_retry`** logs an attempt before it sleeps, **`fetch`** wraps one endpoint call in the retry loop, and **`classify`** turns an `HttpError` into the right one of those four outcomes.


### 3.10 `processing_utils.py`

**`process_videos(youtube, video_ids, video_ids_file, corpus=DEFAULT_CORPUS, db_path=None)`** The crawl loop. Refuses to start without a key, marks every ID pending, then for each video fetches metadata in batches, fetches the comments, and writes the video. A video whose metadata failed transiently stays pending; one the API did not return at all is marked unavailable. Quota exhaustion stops the loop and leaves the rest pending. Renumbers positions, records provenance and rebuilds the indexes at the end. Returns whether the quota ran out.

Its inner **`publish_todo`** writes the still-pending IDs back to the ID file, every fifty videos and once at the end, so the file on disk and the corpus agree.

**`generate_search_list(primary_lists, secondary_list)`** Combines the primary lists with the secondary terms: every primary list is extended by each secondary term in turn. With no secondary terms the primaries are returned unchanged. Returns a list of term lists.


### 3.11 `data_enrichment_utils.py`

**`_now()`** Current UTC time as an ISO 8601 string, for the provenance rows.

**`_pending(conn, column, force_rebuild, extra='')`** Counts how many messages still need a pass, or all of them when the pass is being rebuilt. Used to size the progress bar before the work starts.

**`review(db_path=None)`** Walks the videos that have not been reviewed, showing title, views and message count, and deletes the ones you reject together with their messages and annotations. Commits after each decision, so quitting halfway keeps what was decided.

**`detect_languages(db_path=None, force_rebuild=False, min_chars=None, min_confidence=None)`** Runs langdetect over every message without a language, in batches of 5,000, paging by message ID. Then does the same for video titles and descriptions, storing those as annotations. Reports how many were left unknown and why: too short, or too uncertain. Returns how many records were written.

**`sentiment_analysis(db_path=None, force_rebuild=False)`** Scores English messages with TextBlob and VADER and writes both, with their source. Refuses to start while any message has no language, because it would have no way to tell which are English. Non-English messages are stored as "N/A" rather than zero. Returns how many were scored.

**`blob_analysis(text)`** TextBlob polarity, rounded to two decimals. Between -1 and 1.

**`vader_analysis(text)`** VADER compound score, rounded to two decimals. Between -1 and 1, computed by a shared analyzer built once at import.

**`detect_language(text, min_chars=None, min_confidence=None)`** Returns a two-letter code or "unknown". Gives up when the text is shorter than the minimum, when langdetect raises, or when the best guess is below the confidence threshold.

**`export_anonymization_map(path=ANONYMIZATION_MAP_JSON, db_path=None)`** Writes the channel-to-token pairs from the `channels` table to a JSON file. For moving a mapping between projects. Returns how many pairs.

**`import_anonymization_map(path=ANONYMIZATION_MAP_JSON, db_path=None)`** Reads such a file back and updates the `anon_token` column. Returns how many pairs.


### 3.12 `embed.py`

**`split_spec(spec=None)`** Splits `provider:model` into its two parts, falling back to the configured spec. Raises `ValueError` naming the expected shape when it does not split.

**`embed_texts(texts, spec=None, timeout=900)`** Posts a batch of texts to the Ollama embedding endpoint and returns the vectors. Raises `RuntimeError` naming the URL and the model when the provider cannot be reached, and `NotImplementedError` for any provider but Ollama.

**`quantise(vector)`** Normalizes a vector to unit length and scales it into int8. This is what makes the corpus's vectors small enough to hold: a dot product of two of them is their cosine similarity times a constant.

**`estimate_local(sample_texts, total, spec=None)`** Embeds a small sample, measures it, and scales the time to the full count. Returns seconds.

**`confirm(spec, total, sample_texts, assume_yes=False)`** Runs the estimate and asks whether to go ahead when it is above the configured threshold. Prints the estimate in minutes or hours. Returns whether to proceed.

**`pending_count(conn, model, corpus=None)`** Counts messages with text that have no current vector for this model. A vector whose `text_hash` no longer matches the text counts as missing.

**`embed_corpus(spec=None, corpus=None, db_path=None, assume_yes=False, limit=None)`** Embeds in batches, committing after each one, so the run can be stopped and picked up later. Paging is by message ID, and the pending test is repeated in the query, so work already done is skipped. Updates `embedding_state`, records provenance, and returns how many were written.

**`search(query, top_k=None, min_score=None, corpus=None, spec=None, db_path=None, chunk=50000)`** Embeds the query, then streams the stored vectors in chunks, scoring each chunk as one matrix product and keeping the best rows in a heap. Returns the ranked rows, the model name, and how many records were searched.

**`write_results(path, rows, query, model, searched, top_k, min_score, jsonl=False)`** Writes the ranking as CSV with a commented manifest, or as JSONL with the manifest as its first line. The manifest records the query, the model, the cutoffs and the size of the search, so a result file can be read months later.

**`preview(rows, limit=None)`** Prints the top rows, with score, language, video and the first 72 characters of the text. Says how many more are in the file, and warns when the result is large enough that it should be read as a ranking rather than as a set of matches.


### 3.13 `filter_utils.py`

**`filter_data(data, query, mode='or', words=False)`** Filters an in-memory corpus in the exchange shape, keeping videos whose description or whose comments match. Uses the same case folding and the same word patterns as the SQL path, so narrowing an existing result gives the same answer as filtering the corpus. Returns the filtered structure. Its inner **`matches_query`** applies one term set to one text.

**`folded_offsets(text)`** Case-folds a text character by character while recording, for each position in the folded text, which position it came from in the original. Case folding can change length, so this is what lets a match found in the folded text be highlighted in the original.

**`match_spans(text, query_list, words=False)`** Returns the `(start, end)` spans of every match in the original text, using the word pattern for whole-word mode and the folded offsets otherwise. Both are the same rules the filter matched with, so what is highlighted is what was found.

**`hits(data, query_list, words=False)`** Prints each matching video, then the passages from its description, comments and replies with the matched term coloured and about fifty characters of context on either side. Its inner **`extract_snippets`** collapses whitespace and builds those passages.


### 3.14 `export_utils.py`

**`read_videos(path)`** Opens an exchange file through the bridge's reader and returns an iterator of `(video_id, entry)`. Logs and returns `None` when the file cannot be read, so the converters can stop quietly.

**`save_comments_to_json(all_comments, filename)`** Writes a corpus structure to JSON through the atomic writer.

**`without_control_characters(value)`** Strips control characters except tab, newline and carriage return. Used before writing anything a text editor or Gephi has to open.

**`esc(value)`** HTML-escapes a value, stripping control characters first, and turns `None` into "N/A".

**`convert_json_to_html(json_file, output_html)`** Writes a single browsable HTML file: one collapsible section per video with its metadata, then its comments and their replies, with language and sentiment beside each.

**`gephi_safe(string)`** Normalizes to NFC and strips control characters, which is what keeps Gephi from refusing a file.

**`convert_json_to_gephi(json_name, gephi_name, include_replies=False)`** Builds a directed graph from uploader to commenter, and optionally from commenter to replier, with the edge weight counting the interactions, and writes it as GEXF.

**`convert_json_to_csv(json_file, csv_file)`** Writes one row per comment and per reply, with the video's fields repeated on each, all values quoted. Thirty-one columns, listed in the function. Its inner **`row_for`** builds one row for a comment or a reply, and **`rows`** yields them in order.


### 3.15 `io_utils.py`

**`inside_working_directory(path)`** Resolves a path and says whether it stays inside the current directory. Symlinks are resolved first, so a link pointing outward does not pass.

**`refuse_outside(path)`** Raises `ValueError` naming the path and the working directory when it would leave it, otherwise returns the path. Every command that writes a file the user named calls this.

**`prepare_output_directory(directory)`** Creates a directory if it is missing, logging and re-raising when that fails.

**`load_existing_comments(filename)`** Reads a corpus file if it is there, returning an empty dict when it is not or cannot be parsed, with the reason logged.

**`delete_all_files()`** Deletes the exported files, the author cache, the marker file and the `COMMENTS/` folder, including the generated patterns such as `*.gexf` and `*.jsonl`. Refuses to delete a `COMMENTS` that resolves outside the working directory.


### 3.16 `cache_utils.py`

**`load_channel_data_cache(filename=CACHE_FILE)`** Reads the author cache, returning an empty dict when caching is off, the file is missing, or the JSON is broken, with the reason logged.

**`save_channel_data_cache(cache, filename=CACHE_FILE)`** Writes the cache atomically, or does nothing when caching is off.


### 3.17 `helper_utils.py`

**`is_quota_exceeded(e)`** Reads the reason codes out of an `HttpError` and says whether the quota is gone. A 403 can mean several things, and this is what tells quota exhaustion apart from comments being switched off.

**`error_reasons(e)`** The reason strings of an `HttpError`, lowercased, as a list. Empty when the error carries no details.

**`is_comments_disabled(e)`** True for a 403 whose reason says comments are disabled. A 403 with no details at all is treated as this rather than as a quota problem, because that is the commoner case.

**`is_transient(e)`** True for 500, 502, 503 and 504, and for backend and service errors by name. False for quota exhaustion, which must not be retried.

**`call_with_retry(operation, attempts, backoff, on_retry=None)`** Calls an operation, retrying transient failures with a doubling delay and reporting each retry through the callback. Re-raises anything else at once, and re-raises the last failure when the attempts run out.

**`safeint(value)`** Integer or 0. The API returns counts as strings and sometimes not at all.

**`safe_write_json(data, target_file)`** Writes JSON to a temporary file and renames it over the target, so a failure halfway leaves the old file intact. Cleans up the temporary file on error.

**`save_video_ids(video_ids, filename)`** Writes the pending ID list one per line, through the same write-and-rename.

**`is_youtube_video_id(id_string)`** Whether a string is eleven characters of the YouTube ID alphabet.

**`check_pending_downloads(filename)`** Counts the non-empty lines of an ID file. Returns 0 when there is none and -1 when it cannot be read.

**`merge_schemas(schema1, schema2)`** Merges two inferred schemas, combining dicts key by key and concatenating lists. Used to describe a file whose records do not all have the same fields.

**`read_any_corpus_file(path)`** Reads a corpus file whether it is JSON or JSON Lines, and raises `json.JSONDecodeError` naming the line when a line is not an object or carries no video ID. Returns the videos as a dict.

**`validate_data_structure(file_path, verbose=True)`** Reads an exchange file, infers the union of all its record shapes, and prints it as an indented tree. Reports the file-level problems rather than raising. Its inner **`extract_schema`** turns a value into its type tree, and

**`print_schema`** prints one.

**`get_df_comments()`** The frame the single-plot TubeScope commands start from: every record for the current mode, with sentiment resolved and the timezone dropped. Returns `(frame, None)`, or `(None, None)` with a message when the corpus holds nothing for this mode.

**`add_occurrence(occurrences, bucket, identifier, path)`** Records that an ID was seen at a path, under a named bucket. The building block of the duplicate finder.

**`find_duplicate_ids(data, ...)`** Walks a corpus structure and records every video, comment and reply ID together with the path it appeared at. Returns the duplicates and the full occurrence map. Its inner **`walk_video`** does the per-video descent.

**`print_dupe_report(dupes)`** Prints each duplicated ID, how often it appears and where, up to ten paths each.

**`duplicate_check(file_path, ...)`** Reads a file and runs the finder and the report over it. Returns both structures.

**`string_len(x)`** Length, or 0 for anything without one.

**`content_score(obj)`** A rough measure of how much is in a structure: keys, items, and string lengths added up. Used to decide which of two records with the same ID to keep.

**`parse_segment(seg)`** Splits a path segment such as `comments[3]` into the key and the index.

**`resolve_parent_and_index(data, path)`** Walks a path such as `/VIDEO/comments[4]/replies[2]` and returns the container and the key or index that addresses the item, or `(None, None)` when the path no longer resolves.

**`gather_items_for_paths(data, paths)`** Resolves several paths and returns the items with their containers, skipping the ones that no longer resolve.

**`delete_many(grouped)`** Deletes items given as container and index, grouping by container and going through each from the highest index down so the earlier indices stay valid. Returns how many were deleted.

**`clean_dupes(data, dupes, ...)`** Keeps the richest occurrence of each duplicated ID, preferring a comment that has replies, and deletes the rest in place. Returns a report of what was kept and removed.

**`load_json_file(path)`** / **`save_json_file(path, data)`** Read and write a corpus file, the first through `read_any_corpus_file`.


### 3.18 `platform_utils.py`

**`posix_permissions_are_meaningful()`** Whether file modes mean anything here. False on Windows, where every ordinary file reports 0o666 and a mode check would warn on every run.

**`private_file_is_readable_by_others(path)`** Whether a file that should be private has group or other bits set. Always False where modes are meaningless.

**`make_file_private(path)`** Tightens a file to 600 and returns a sentence saying so, or a sentence explaining that this platform has no file modes and what to do instead.

**`safe_filename(name, replacement='_')`** Makes a filename legal on Windows as well: forbidden characters and control characters replaced, trailing dots and spaces removed, reserved device names prefixed.

**`console_can_print(text, stream=None)`** Whether the console's encoding can represent a string. Used before printing Japanese or Korean into a terminal that cannot show it.

**`printable(text, stream=None)`** Returns the text, or a version with unrepresentable characters replaced, so a print cannot raise `UnicodeEncodeError` and kill a long run.

**`use_unicode_console()`** Reconfigures stdout and stderr to UTF-8 when they cannot already print CJK. Returns which streams were changed.

**`remove_files(paths)`** Deletes the files that exist and reports which were locked. Returns `(removed, blocked)`.

**`looks_like_a_repository(directory='.')`** Whether a directory has a `.git` in it.

**`live_credential_in_a_repository(key_file, example_file, directory='.')`** True when the working directory is a repository and the key file holds something other than the placeholder. The shell warns about this at startup.

**`plural(count, word, many=None)`** The singular for one, the plural otherwise, with an irregular plural passed in when needed.


### 3.19 `logging_config.py`

**`class Scrubbed(logging.Filter)`**

  **`filter(self, record)`** — runs every log message and every formatted traceback through `sanitize.scrub` before it is written, and replaces the record with a placeholder if that itself fails. Always returns True: it rewrites rather than drops.

**`class WithoutTraceback(logging.Formatter)`** A formatter that leaves the traceback out while it formats, then puts it back. The console handler uses it, so an unexpected failure reaches the screen as one line and the log file still gets the full stack.

**`_scrubbed_add_handler(self, handler)`** Replaces `Logger.addHandler` so every handler gets the scrubbing filter attached, whoever adds it. This is why an identifier cannot reach a log file through a library that configures its own logging.


### 3.20 `font_utils.py`

**`_installed_fonts()`** Builds the name-to-path map of the fonts matplotlib can see, once per process.

**`configure_cjk_fonts()`** Picks the CJK-capable fonts that are installed, appends any installed emoji font after them, and sets that chain as the font family, so text falls through to a font that can draw it. Silences the per-glyph "missing from font" warning, which is unactionable and comes once per glyph. Warns once when no CJK font is present. Returns the path of the font the word cloud should use.

**`get_wordcloud_font_path()`** The font path for the word cloud, configuring the fonts first if that has not happened.


### 3.21 `llm_fields.py`

One place that decides which annotation wins when several sources annotated the same thing. Every module reads through these rather than reaching for a column.

**`is_missing(value)`** Whether a value counts as absent: `None`, NaN, or one of the strings "", "unknown", "N/A", "n/a".

**`read(record, field)`** One field from anything dict-like, returning `None` rather than raising when it is not.

**`first_present(record, fields, default=None)`** The first field in a list that is not missing, or the default. This is the preference order in one function.

**`language_of(record, default=None)`** An LLM language verdict if there is one, else the stored langdetect one.

**`sentiment_of(record, default='N/A')`** An LLM sentiment score if there is one, else VADER.

**`sentiment_label_of(record, default=None)`** / **`sentiment_confidence_of(record, default=None)`** The LLM's label and confidence, when a run produced them.

**`video_info_of(entry)`** The `video_info` of an entry, or the entry itself when it already is one, so callers can pass either.

**`llm_video_language_of(entry, default=None)`** Only the LLM's verdict on a video title, with no fallback. Used where the fallback would be misleading.

**`video_language_of(entry, default=None)`** The video's title language, LLM first, langdetect second.

**`video_description_language_of(entry, default=None)`** The same for the description.

**`uses_llm_data(record)`** Whether a record carries any LLM annotation at all.


### 3.22 `results.py`

Where a run's figures and tables end up.

**`slug(text, limit=60)`** Turns a plot title into a filename part: non-alphanumerics to hyphens, lowercased, cut to length, with a fallback for an untitled figure.

**`class Collector`** A context manager that makes every figure a run draws land on disk while it still appears on screen.

  **`__init__(self, name, adopt=None)`** — sets the target directory under `figures/`, and optionally a directory whose finished PNGs should be taken over at the end. Builds the capture closure, which has to be a function because matplotlib writes a signature onto whatever replaces `plt.show`.

  **`title_of(self, figure)`** — the figure's suptitle, or the first axes title that is set, or the empty string.

  **`save_open_figures(self)`** — saves every open figure that has not been saved yet, numbered in the order they were drawn, and marks it so a later call does not save it again. Figures stay open, so they remain on screen.

  **`show_without_blocking(self)`** — calls the real `plt.show` without blocking, so windows appear and the run carries on.

  **`adopt_written_figures(self)`** — moves PNGs a module wrote under its own names into the figures directory, so everything from one run is in one place.

  **`__enter__(self)`** — creates the directory, silences the "more than 20 figures" warning for the run, and replaces `plt.show` and `Figure.show` with the capture.

  **`__exit__(self, *details)`** — saves what is still open, adopts the named files, and puts `plt.show`, `Figure.show` and the warning limit back, even when the run raised.

**`collect(name, adopt=None)`** Builds a `Collector` for a named tool. What the shell commands wrap themselves in.

**`write_table(name, frame)`** Writes a DataFrame to `output/<name>.csv`, creating the directory. Does nothing for an empty or absent frame. Returns the path or `None`.


### 3.23 `tubescope.py`

Counts, distributions and everything over time. Every plot shows on screen and is saved by the collector the shell wraps the run in.

**`daily_series(series, fill)`** Turns a series indexed by date into one row per calendar day between the first and the last. Dates before 2005 or in the future are named and dropped, since YouTube did not exist and a date in the future is an error. `fill=0` is right for counts, `fill=None` for averages, where a day without a measurement should stay a gap.

**`resolve_mode(mode=None)`** Passes through to the repository's mode resolution, so there is one definition of what `c`, `r` and `cr` mean.

**`mode_label(mode=None, form='plural')`** The human name of what is being counted, for titles and messages: "Comments and Replies", "Comment or Reply", "Comment and Reply".

**`collect_all_records_from_db(mode=None, corpus=None, years=None)`** Reads the messages with the LLM annotation columns attached.

**`collect_records(video_data, mode=None)`** Flattens one video's comments and replies into a list according to the mode, marking each with its kind. For working from an in-memory structure rather than the corpus.

**`collect_all_records(data=None, mode=None)`** Reads from the corpus when given nothing, otherwise flattens the structure it was given.

**`interaction_density_from_counts(total_comments, total_replies, comments_with_replies, views)`** The experimental score: half the share of comments that got a reply, half a depth term from replies per comment. Returns the parts and the score. Views are carried but not used in the score.

**`interaction_density(df_comments, video_info)`** The same for one video, counting from its comment frame.

**`plot_interaction_density_distribution(bin_count=30)`** Histogram of the score over all videos with comments, with mean, median and the top five named. Skips videos that raise rather than failing the run.

**`extract_video_info(video_id, video_data)`** Splits an entry into `(video_info, comments)`, returning `(None, None)` with a message when the shape is wrong.

**`analyze_comments(comments)`** Turns records into a DataFrame with the date parsed as UTC and a `sentiment` column resolved through the annotation preference order.

**`get_most_liked_comments(df_comments, top_n=5)`** The most-liked rows, sorted.

**`calculate_average_sentiment(df_comments)`** Mean sentiment over the rows that have one. Returns `(mean, scored, total)`, with `None` for the mean when nothing is scored.

**`group_comments_by_date(df_comments)`** Counts messages per calendar day. The input to the activity plot.

**`plot_comment_likes_distribution(df_comments, cap_height=100, bin_count=30, x_min=None, x_max=None)`** Histogram of likes in buckets, drawn only over messages with at least one like, with the zero-like share stated in the legend. Bars above the cap are drawn at the cap and labelled with the true count. Bucket edges are widened when the range is too narrow to cut, and the lowest value is included rather than falling out of the first bin.

**`plot_sentiment_distribution(df_comments)`** Histogram of sentiment scores with a density curve, mean and median, and how many messages have no score at all.

**`plot_comments_over_time(comments_per_day)`** Activity over time: the raw daily count on one axis, a rolling average over a window of a fiftieth of the span on the other, and the ten busiest days in the legend.

**`analyze_replies(df_comments)`** The share of comments that got at least one reply, working from the nested replies when they are there and from the parent IDs when they are not. Returns `None` when the frame holds no comments, which is what happens in reply-only mode.

**`analyze_sentiment_over_time(df_comments)`** Average sentiment per calendar day over the rows that have a numeric score.

**`plot_sentiment_over_time(sentiment_per_day)`** That average over time, with a rolling average, mean and median. Days without a measurement stay empty rather than being drawn as zero.

**`plot_participation_timeline()`** Two lines: how many distinct channels were active per day, as a rolling average, and the cumulative count of channels that had appeared by then. Uploads count as participation.

**`plot_interactions_by_weekday(figsize=(12,6), normalize=False)`** Comments and replies as grouped bars per weekday, their total as a line, and uploads on a second axis because there are far fewer of them. Its inner **`to_weekday`** parses dates into weekday numbers, and **`count_per_day`** counts them into a fixed Monday-to-Sunday order.

**`plot_views_vs_comments(include_replies=None, color_by='year', annotate_top=5, ...)`** Views against discussion volume per video on logarithmic axes, with point size by discussion, colour by year or by discussion-per-view, a fitted power law over the videos that have both, and the most talked-about outliers labelled.

**`plot_uploads_over_time(freq='ME')`** Uploads over time at daily, weekly, monthly or yearly resolution, with a rolling average and the busiest periods named.

**`collect_views_dataframe()`** Video ID, title and views for every video that has a view count, most viewed first.

**`shorten(text, n=60)`** Cuts a title and adds an ellipsis. Used for plot labels.

**`plot_top_videos_by_views(df, top_n=15, title_suffix='')`** Horizontal bars of the most viewed videos with the exact count written beside each.

**`plot_views_distribution(df, log_hist=True, bins=50)`** Two figures: a histogram of view counts on a log scale, and a violin of their logarithms with mean, geometric mean, median and quartiles. View counts are distributed such that the arithmetic mean says little on its own.

**`analyze_views_static(top_n=15, log_hist=True, bins=50)`** Runs both view figures and writes the views table to `output/`.

**`tubescope()`** The whole module in order: activity, participation, likes, sentiment over time and by distribution, interaction density, weekdays, uploads, views against discussion, and the view figures. Writes the views and most-liked tables. Says how much it is working on and stops early when the corpus holds nothing for the current mode.


### 3.24 `tubetalk.py`

**`build_comments_df(data=None)`** The frame the language work starts from: video, date, text, language and kind. From the corpus it resolves the language through the annotation order; from a structure it flattens the nesting. The timezone is dropped so the dates group cleanly.

**`language_view(corpus=None, years=None)`** Rebuilds the nested shape the language functions expect, but only with the language fields, so the mismatch counts can work without loading every text.

**`infer_video_language(info, min_support=5, majority_threshold=0.4)`** Guesses a video's language from its comments when the title gives nothing: the commonest language wins if at least `min_support` comments carry it and it holds at least the given share. Returns `None` when neither test passes. Guessed, not measured.

**`resolve_video_language(info, min_support=5, majority_threshold=0.4)`** The video's language in order of trust: the LLM's verdict on the title, then the inference from comments, then langdetect on the title.

**`count_comments()`** Totals and distributions: videos, comments, replies, how many videos have comments, and the mean, median, spread and range of comments per video and replies per comment.

**`count_languages()`** Language counts at three levels: videos by their title language, comments, replies. Messages annotated by several sources are counted once.

**`count_language_mismatches(data, infer_video_lang=True, min_support=5, majority_threshold=0.4)`** Counts the pairs where a comment differs in language from its video and where a reply differs from its comment. Returns both tables and both totals.

**`plot_language_distribution(data, level='comment', top_n=None, normalize=False)`** Bar chart of languages at one level, as counts or shares, with an empty panel and a note when there is nothing to show.

**`plot_language_conflicts(data, top_n=20, normalize=True, ...)`** Two bar charts of the commonest mismatches and a heatmap of reply language against comment language. Its inner **`top_n_ranking`** cuts a table to the top N and adds the percentage.

**`tokenized_stopwords(words)`** Expands a stopword list through the vectorizer's own analyzer, so a multi-word stopword also blocks the tokens it becomes.

**`search_terms_as_stopwords()`** The words from the configured search terms, lowercased and split. Every video was found by them, so they are in every corpus by construction and say nothing.

**`word_frequencies(df, start_date=None, end_date=None, ngram_range=(1,1), ...)`** Counts terms over the texts after filtering by date and language, with the standard stopwords, any extra ones, and the search terms removed. Returns a term-to-count dict, or an empty one when the corpus is too small to build a vocabulary, which is reported quietly rather than raised.

**`plot_wordcloud(freqs)`** Draws the cloud, using a font that can show the corpus. Draws a panel saying there is nothing when the frequency dict is empty.

**`lda_fit(df, n_topics=8, min_frequency=5, max_frequency=0.5, ngram_range=(1,2), stopwords=None, random_state=42)`** Vectorizes the texts and fits the LDA. Returns `(model, vectorizer, matrix, terms)`, with the model `None` when there is no usable vocabulary. The seed is fixed, so two runs over the same corpus give the same topics.

**`lda_topics(df, ..., fitted=None)`** The top terms per topic with their weights, as a long DataFrame. Takes a fitted model to avoid fitting twice.

**`lda_doc_topics(df, ..., fitted=None)`** The dominant topic per document with its weight and date. Takes the same fitted model.

**`plot_topics_bar(topics_dataframe, number_of_columns=2, document_topics_dataframe=None, ...)`** One figure with a panel of top terms per topic, a bar of each topic's share of the corpus, and, when dates are available, the monthly movement of those shares.

**`run_wordcloud(data, ...)`** Builds the frame, counts the terms, draws the cloud. The path the shell command takes.

**`run_topics(data, n_topics=6, n_words=10, min_df=5, max_df=0.6, ngram_range=(1,2))`** Fits once and uses that fit for both the term table and the document assignments, then draws them. Returns the topic table so the caller can write it out.

**`clean_social_media_markers(data, text_col='text')`** Extracts links, e-mail addresses, hashtags, @-mentions and link domains from the texts. Returns them as sets. Its inner helpers: **`to_lower_list`** lowercases, **`extract_links`**,

**`extract_emails`**, **`extract_hashtags`** and **`extract_mentions`** apply one pattern each and keep the order, **`extract_domains`** parses the host out of a URL, and **`flatten`** unrolls a column of lists.

**`smmarkers(markers, path=MARKERS_TXT)`** Prints the readable markers and writes the hashed ones to a file, saying how many. In a corpus that was never sanitized the mentions are real handles and stay on screen; in a sanitized one they are tokens and go to the file, where thousands of them do not bury the rest.

**`tubetalk()`** The whole module: markers, language distribution, language conflicts, word cloud, topic model. Writes the topic table.


### 3.25 `tubegraph.py`

Channel networks. The co-occurrence network is a projection: everyone who appeared under the same video is connected to everyone else who did. That makes a clique of every comment section, which is why the structure measures carry a note saying so and the community detection is reported against a null model.

**`channel_occurrence_stats()`** Per channel: uploads, comments, replies, their sum, and how many distinct videos it appears in. Returns a DataFrame sorted by total activity.

**`plot_channel_role_proportions(df, roles=..., top_n=None, normalize=False, ...)`** Grouped bars of the three role counts per channel, as counts or as shares of that channel's own activity.

**`build_reply_network(include_self=False, min_weight=1)`** The directed graph of who replied to whom, with the edge weight counting the replies. Drops edges below the minimum weight, records the filters on the graph, and writes a GEXF beside it. Returns the graph.

**`plot_reply_network(graph, top_n=30)`** Draws the busiest part of the reply network, sized by degree, with isolated nodes removed.

**`_share_of(part, whole)`** A percentage as a string, "0.0%" when the whole is zero. Used in the filter reports.

**`_select_participation(roles=..., exclude_uploader=True, min_videos_per_channel=2, max_participants_per_video=None, top_channels=None, report=True, label=...)`** Turns the corpus into the channels worth keeping and the videos each appeared in, applying every filter in turn and saying how much each one dropped. Returns `(kept channels, participation)`. The reporting is the point: every later number depends on these cuts.

**`_participation_matrix(keep, participation)`** The sparse channel-by-video incidence matrix, as int32. The width of the integer matters: a narrower one overflows when a pair shares many videos.

**`_band_edges(incidence, block_rows=None, cell_budget=None)`** Cuts the matrix into row bands whose products stay inside a cell budget, so the projection of a large corpus does not try to allocate one enormous matrix.

**`_cooccurrence_bands(incidence, block_rows=None, cell_budget=None)`** Yields each band of the co-occurrence product in turn, as sparse rows.

**`_projected_edges(keep, participation, min_weight=2, top_k_per_node=50, max_edges=None, block_rows=None)`** Walks the bands and collects the edges: pairs above the weight threshold, at most the strongest `top_k_per_node` per channel, optionally capped overall. Returns the edges and the three counts needed to say how many pairs were dropped at each step.

**`build_interaction_graph(roles=..., exclude_uploader=True, ...)`** The undirected co-occurrence graph, with every filter reported as it is applied and recorded on the graph itself. Returns an empty graph rather than failing when nothing survives the filters.

**`channel_video_participation_matrix(roles=..., dtype=bool, top_channels=None, top_videos=None)`** The dense channel-by-video matrix as a DataFrame, optionally cut to the most active channels or the busiest videos. For the heatmap, which needs labels.

**`plot_channel_clustering_heatmap(matrix, max_channels=400, cluster_max=None, fast=True, both=True, ..., auto=True, similarity='dot', transform='none', clip_vmax=None)`** Similarity heatmap of channels, with an optional dendrogram. With `auto` it measures the density and skew of the matrix and picks the transform, the similarity and the clipping to match, printing what it chose. Its inner **`auto_settings`** derives those choices, **`apply_transform`** applies log1p or nothing, **`build_similarity`** computes dot, cosine or correlation and reports how many correlation cells were undefined, and **`clip_upper`** returns an upper percentile so a few extreme cells do not flatten the rest.

**`top_connected_channels(graph, top_n=10)`** The highest-degree channels as a small table.

**`plot_channel_degree_distribution(graph, cap_height=100, bin_count=30, ...)`** Histogram of node degrees, with the zero-degree share in the legend and capped bars labelled with their true count.

**`_detect_communities(graph, method='louvain', seed=42)`** Runs Louvain or greedy modularity, with a fixed seed. Returns each node as its own community when the graph has no edges. Rejects an unknown method by name.

**`community_structure(roles=..., method='louvain', seed=42, draws=20, budget_seconds=600.0, ...)`** Detects communities and reports them against a null model that keeps every channel's number of videos and every video's number of participants. Projects the cost of the null first and reports modularity without one, saying so plainly, when it would blow the budget. Returns the measures table and the channel-to-community table.

**`_modularity_null(keep, participation, min_weight, top_k_per_node, max_edges, block_rows, method, seed, draws, burn_in, thin, report)`** Draws shuffled versions of the channel-video matrix with the curveball algorithm, reprojects and re-detects for each draw, and returns the modularity values. Draws that produce no edges at all are left out. Its inner **`bit_choice`** picks which memberships move in one trade, and **`run`** applies a number of trades.

**`_measure(rows, name, value, note='')`** Appends one measurement with its note, turning NaN into "undefined".

**`_filters_note(graph)`** The filters recorded on a graph, as one readable string, so a number is never shown without what it was computed over.

**`_affordable_path_measures(graph, budget_seconds=30.0, sample=8)`** Times a few single-source searches and projects the cost of doing all of them. Returns the projection and whether it fits the budget.

**`graph_structure(graph, expensive=False, budget_seconds=30.0, report=True)`** The shape of an undirected graph: nodes, edges, density, components, clustering, transitivity, assortativity, k-core, PageRank, and, when asked and affordable, diameter and average path length. Clustering and transitivity carry the note that the projection inflates them.

**`directed_structure(graph, report=True)`** The same for the reply network: weak and strong components, reciprocity, self-replies, mean out-degree.

**`_print_structure(rows, graph)`** Prints a measures table in aligned columns with the notes beside the values.

**`compute_centrality_measures(graph, skip_slow=False, speed_up=True)`** Degree, betweenness and eigenvector centrality. Betweenness is approximated above 500 nodes, with the sample size falling as the graph grows. A failure in one measure is reported and the others are still returned. Its inner **`choose_approx_k`** picks that sample size.

**`plot_centrality_results(centrality_df, top_n=20, both=True, ...)`** One bar panel per measure for the top channels, and a scatter of degree against betweenness with the point size carrying eigenvector.

**`plot_top_channels(df, role='commenter', top_n=10)`** Horizontal bars of the most active channels in one role.

**`plot_network_graph(graph, top_n=None, layout='spring', seed=42, ...)`** Draws a graph or its busiest part, with the layout named. Edge colour carries weight.

**`frequent_channel_pairs(threshold=3, roles=..., top_n=None, ...)`** The channel pairs that appear together at least `threshold` times, computed over the same bands as the graph. Returns a DataFrame sorted by co-occurrence, and reports how many pairs each cut dropped.

**`plot_channel_pair_network(pairs_df, top_n=50)`** Draws the strongest pairs as a network with communities coloured, and writes a GEXF.

**`tubegraph()`** The whole module in one go: channel statistics, the co-occurrence heatmap, frequent pairs, the interaction graph, the network drawing, degree distribution, top degrees, structure, communities with their null, centrality, role proportions, and the reply network. Writes six tables.

**`_shape_of(graph, label)`** One row describing a graph: nodes, edges, density, components, largest component and its share, isolated nodes, mean degree, transitivity. For comparing settings against each other.

**`tubegraphfull(matrix_channels=400, cluster_max=300, pair_threshold=3, ...)`** Everything `tubegraph` does, plus a sweep: the reply network at minimum weights 1, 2, 3 and 5, channel pairs at thresholds 2, 3, 5 and 10, and the interaction graph over the top 8,000 and 30,000 channels, with the shapes collected into one table. Frees each graph before building the next, because they do not all fit at once.


### 3.26 `tubeconnect.py`

Comparing several corpora: who is active in more than one, how that develops, and whether it is more than chance. This is the only module that does inferential work, and most of its length is the null models rather than the counting.

**`class Edge`** A dataclass for one participation event: channel, video, role, weight, which corpus, and the comment or reply it came from with its timestamp.

**`parse_dt(timestamp)`** Parses an ISO 8601 timestamp to an aware UTC datetime, falling back to the part before the fractional seconds, and returning `None` rather than raising.

**`ensure_channel_exists(channel_map, channel_id)`** Returns the role flags for a channel, creating them as all-false if new.

**`provide_roles_set(role_dict)`** / **`provide_roles_string(role_dict)`** The roles a channel actually held, as a set and as a sorted comma string.

**`load_fandom(spec, label, name=None)`** Loads one corpus according to its filter, which may name a language or a corpus tag. Builds its videos, its channels with their roles, and one edge per participation event. Replies whose comment is missing get a shell so their timestamp is not lost.

**`get_overlap(fandoms, basis)`** The time window all corpora share, measured either over comments or over video publication dates. Returns `(start, end)`, or `(None, None)` when they do not overlap or when capping is off.

**`update_channel_roles(fandom)`** Recomputes the role flags from the edges that remain, after a time cap has removed some.

**`cap_fandoms_to_overlap_time(fandoms, start, end, basis)`** Cuts every corpus to the shared window, dropping the edges and videos outside it, so a corpus that ran for ten years is not compared against one that ran for two.

**`compare_sets(set_a, set_b)`** The counts behind a comparison: both, only A, only B, and the Jaccard index and overlap coefficient, with `None` where they are undefined.

**`compute_pairwise_overlaps(fandoms)`** Runs that comparison for every pair, in total and per role. Returns the section of the summary. Its inner **`channels_with_role`** picks the channels that held one role.

**`build_labels_and_index(fandoms)`** The corpus labels and a label-to-position map, so matrices always use the same order.

**`initialize_matrices(labels, role_names)`** Zero matrices for the totals and one per role.

**`map_channel_to_fandom_labels(fandoms)`** Which corpora each channel belongs to, as a dict of sets. The basis of every cross-corpus count.

**`accumulate_interaction_matrices(fandoms, label_to_index, channel_to_fandom_labels, total_matrix, role_matrices)`** Counts each interaction into the cell of the corpus the actor belongs to and the corpus the video belongs to. A channel in two corpora counts in both rows, which is why the plot says not to sum the cells.

**`compute_cross_share(labels, label_to_index, total_matrix)`** Per corpus, the share of its members' interactions that went to another corpus's videos.

**`compute_cross_share_by_role(labels, label_to_index, role_names, role_matrices)`** The same broken down by role.

**`compute_cross_channels(fandoms, labels, role_names, channel_to_fandom_labels)`** How many distinct channels of each corpus interacted across at all, in total and per role.

**`compute_interaction_attribution(fandoms, channel_to_fandom_labels)`** What share of a corpus's interactions came from channels that are also in another one. The difference between many shared people and a few busy shared people.

**`compute_edges_per_video(fandoms)`** Mean and median discussion edges per video, per corpus.

**`matrices_to_lists(role_names, total_matrix, role_matrices)`** Turns the matrices into plain lists so they can go into JSON.

**`compute_cross_fandom_interactions(fandoms)`** Runs all of the above and returns the interaction section of the summary.

**`compute_summary(fandoms, overlap_time)`** The whole summary: per-corpus counts, the pairwise overlaps, the interaction section, and the window that was applied.

**`add_video_nodes(graph, fandoms)`** Adds one node per video, with its corpus and metadata.

**`ensure_channel_node(graph, fandom, channel_id)`** Adds a channel node if it is new, or records that an existing one also belongs to this corpus.

**`add_edge_and_update_degrees(graph, source_channel_id, target_video_id, event_type, weight, fandom_origin, comment_id, reply_id)`** Adds or strengthens one channel-to-video edge and keeps the per-role degree counters on both ends current.

**`build_graph(fandoms)`** The bipartite channel-video graph over all corpora, ready for Gephi.

**`export_gexf(G, out_path, graph_label=None)`** Writes a graph as GEXF, creating the directory. Returns the path.

**`save_summary_json(summary, out_path)`** Writes the summary as indented JSON.

**`plot_interaction_matrix(fandom_labels, matrix, title)`** Draws a matrix as a heatmap with the value written in each cell, in black or white depending on the cell's brightness.

**`plot_bar(data_dict, title, ylabel)`** A labelled bar chart from a dict.

**`generate_plots(summary)`** Draws everything the summary supports: the interaction matrices in total and per role, cross-shares, cross-corpus channel counts, edges per video, and the attribution share.

**`interaction_identity(edge)`** A stable identity for one interaction, so the same event is not counted twice when it appears in two corpora.

**`event_year(edge, video_year)`** The year an interaction belongs to: the comment's or reply's own year, falling back to the video's.

**`yearly_membership(fandoms)`** Who was active in which corpus in which year, on both membership bases, plus how active each channel was. The input to everything that follows.

**`bootstrap_jaccard_interval(both, a_only, b_only, population_size, rng, draws=...)`** A percentile confidence interval for the Jaccard index, by resampling channels from a multinomial over the four cells. Returns `(low, high)`, or `(None, None)` when the union is empty.

**`membership_patterns(members_by_label, labels, year)`** One integer per channel with a bit set per corpus it belongs to. Channels in no corpus are left out; they carry no information and cannot be traded.

**`curveball_trade(patterns, first, second, bit_choice)`** One curveball step: two channels keep the corpora they share and redraw the rest between them, so both keep their number of memberships and every corpus keeps its size. Works on integers or on sets.

**`schedule_for(size, burn_in=None, thin=None)`** How many trades to burn in and how many between samples, scaled to the number of channels.

**`_trade_stream(rng, size, steps, block=1 << 20)`** Yields random channel pairs in blocks, because drawing them one at a time dominates the runtime.

**`fixed_margins_null(patterns, index_a, index_b, rng, permutations=..., burn_in=None, thin=None, report=True)`** The null overlap distribution for one pair, through the all-pairs function.

**`fixed_margins_null_all_pairs(patterns, pair_positions, rng, permutations=..., ...)`** Runs one Markov chain and reads every pair's overlap off the same states, which is what makes several pairs affordable. Returns the draws per pair. Its inner **`bit_choice`** picks which memberships move.

**`permutation_overlap_null(set_a, set_b, activity_by_channel, rng, permutations=...)`** The alternative null, drawing channels with probability proportional to their activity, for comparison with the fixed-margins one.

**`benjamini_hochberg(p_values)`** False discovery rate correction. Returns the adjusted values in the original order. Necessary because every year of every pair is a separate test.

**`population_for(activity_by_year, year, frame=POPULATION_FRAME)`** The population a year's overlap is judged against: everyone active in that year, or everyone in the corpus.

**`analyse_overlap_over_time(fandoms, permutations=..., seed=..., frame=..., bootstraps=..., activity_null=False)`** The main analysis: per pair and per year, the observed overlap, the Jaccard index with its interval, the null mean, the ratio to chance, the p-value with the flooring that keeps a permutation p from being zero, and the corrected significance. Returns the rows.

**`save_overlap_table(rows, out_dir)`** Writes those rows as JSON and as CSV. Returns both paths.

**`plot_overlap_over_time(rows, out_dir='output', basis='audience')`** Per pair, a figure with the observed and expected overlap over the years above and the ratio to chance below.

**`report_overlap_over_time(fandoms, out_dir='output', ...)`** Runs the analysis, writes the table, prints how many years differ from chance, and draws the figures. Says plainly where no baseline exists because the corpora together account for everyone active that year.

**`plot_jaccard_over_time(fandoms, include_by_role=True, out_dir='output')`** Jaccard over time per pair, with the shared count beside it, and a details file per pair. Its inner **`ensure_year`** fills a year with an empty membership so a gap is a gap and not a missing point.

**`load_and_cap(cap_basis=None)`** Loads every corpus named in the settings and caps them to their shared window. Returns the corpora and the window that was applied.

**`read_summary(path=SUMMARY_PATH)`** Reads a written summary, returning an empty dict when it is missing or broken.

**`merge_summary(section, path=SUMMARY_PATH)`** Merges a new section into the summary on disk and writes it back, so the modes can be run one after another.

**`window_section(overlap_time)`** The window as a summary section, with `None` values when no capping applied.

**`tubeconnect(mode='all', cap_basis=None, permutations=..., bootstraps=..., seed=..., frame=..., out_dir='output', confirm=True, activity_null=False)`** The entry point. `overlaps`, `interaction`, `graph`, `plots`, `null` or `all`. Each mode writes its section into the same summary file, so `interaction` leaves behind what `plots` needs. Rejects an unknown mode by name.


### 3.27 `shell_commands.py`

Ten mix-in classes, one per command group, combined into the shell class in `YTCM.py`. Every method is a `cmd.Cmd` command: its name after `do_` is what you type, its docstring is what `help` prints. What each command does for the user is in `YTCM101.md`; here it is what the code does.

**`reject_unknown_flags(parts, known, syntax)`** Checks a command line for flags that are not in the known set, prints them with the syntax line, and returns whether to stop. Every command that takes flags starts with this, so a typo is refused rather than ignored.

**`ask_graph_settings()`** Asks the four questions the graph commands share — roles, whether to include the uploader, the minimum edge weight, the minimum videos per channel — and returns them as keyword arguments. Falls back to the defaults on an unreadable answer.


#### `class YTCMCoreCommands`

**`do_info`** — prints the version, the authors, the licence and what the four analysis modules are for.

**`do_howto`** — prints the recommended order of commands, and how to resume an interrupted crawl.

**`do_status`** — prints the working directory, the API state, the search terms, the key and lookup state, then opens the corpus, rebuilds any missing index, and prints the table counts, the corpora, the download states and the last provenance row.

**`do_validate`** — reads an exchange file and prints its inferred structure, or just checks it with `silent`.

**`do_duplicates`** — reports repeated IDs in an exchange file, and says that the database cannot hold one.

**`do_purgedupes`** — loads a file, finds its duplicates, keeps the richest of each, and writes a `.cleaned.json` beside it. Refuses a path outside the working directory.

**`do_reset`** — deletes the exported files and, with `--all`, the corpus. Prints what will go, requires "yes", and reports which files were locked.

**`do_exit`** — returns True, which ends the command loop.


#### `class YTCMDownloadCommands`

**`do_connect`** — reads the key file, asks for the key if there is none, and builds the API service.

**`do_download`** — parses the corpus flag, picks up pending IDs from the corpus when the session has none, then checks the three preconditions: a list, a connection, a key. Runs the crawl, catching a missing or mismatched key and an unreadable corpus as messages rather than tracebacks. Afterwards reports what is still pending and what was unavailable.


#### `class YTCMEnrichmentCommands`

**`do_generatehash`** — makes the key, requiring the word "replace" to overwrite an existing one, and says what a replacement would cost.

**`do_deletehash`** — destroys the key after the fingerprint is typed back.

**`do_lookup`** — resolves a token to a value, or with `--reverse` a value to its token. Strips the mention and mail prefixes, and says which of the two requirements is missing when it cannot answer.

**`do_deletelookup`** — deletes the re-identification store after a confirmation.

**`do_migratehash`** — runs the legacy map migration and reports how many channels moved.

**`do_review`** — runs the manual review.

**`do_language`** — parses `rebuild` and the two thresholds and runs language detection.

**`do_sentiment`** — parses `rebuild` and runs the sentiment pass.


#### `class YTCMExportCommands`

**`do_import`** — parses the file, corpus, mode and the two cleaning flags, refuses `--sanitize` or `--pseudonymize` without a key, confirms a `replace`, and reports the rows written and the duplicates collapsed. Turns every failure into a message.

**`do_export`** — writes the derived formats. Exports from the corpus through a temporary JSONL file, and says so when a stale `Comments.json` is lying about; falls back to that file only when there is no corpus. Removes the temporary file afterwards.


#### `class YTCMFilterCommands`

**`do_exportjson`** — parses the filename, corpus, year range and `--jsonl`, switching the default name to `.jsonl` when needed, refuses stray arguments, and writes the export.

**`do_embed`** — parses the corpus, limit and `--yes` and runs the embedding pass.

**`do_semsearch`** — parses a quoted concept and its flags, catches the common mistake of an unquoted multi-word concept, runs the search, prints the preview and writes the result file.

**`do_filter`** — parses the mode and `--words`, warns about terms for which whole-word matching cannot apply, then filters the corpus or narrows an existing result, and remembers the terms and the mode for `hits`.

**`do_unfilter`** — drops the filter result and the remembered terms.

**`do_hits`** — prints the matching passages, using the same mode the filter used.

**`do_filtersave`** — writes the filter result to a file inside the working directory.


#### `class YTCMSearchCommands`

**`do_primary`** — parses search terms, with semicolons separating sublists; shows or clears them when asked.

**`do_secondary`** / **`do_exclude`** — the same for the flat lists.

**`do_year`** — sets the search year, refusing anything outside 2005 to the current year.

**`do_search`** — checks the terms, the year and the connection, runs the search, and either appends to or replaces the ID file after asking.

**`do_update`** — parses `--stale-days`, selects the videos to refresh, reports how many and how many have no recorded download time, requeues them and writes the ID file.

**`do_load`** — reads an ID file into the session, saying how many lines were not video IDs.

**`do_addid`** — validates one ID, appends it to the file, marks it pending, and says if the video is already in the corpus with another status.


#### `class YTCMTubeConnectCommands`

**`do_tubeconnect`** — parses the mode and `--activity-null`, rejects an unknown mode, and runs TubeConnect without its confirmation prompt, inside a collector that also takes over the PNGs TubeConnect writes under its own names.


#### `class YTCMTubeGraphCommands`

**`do_tubegraph`** / **`do_tubegraphfull`** — run the module, short or with the sweep, inside a collector.

**`do_interactiongraph`** — asks the graph settings, the subgraph size and the layout, builds the graph and draws it.

**`do_degreedist`** — asks the graph settings and the histogram parameters and draws the degree distribution.

**`do_centralities`** — asks the graph settings and whether to compute the slow measures, then computes and plots them.

**`do_structure`** — asks the graph settings, whether to afford the path measures and what budget to allow, describes the interaction graph, then offers the reply network.

**`do_communities`** — asks the graph settings, the method, the number of draws and the budget, runs the detection with its null, and offers to write the membership table.

**`do_replygraph`** — asks about self-replies and the minimum weight and draws the reply network.

**`do_cooccurrence`** — asks for the roles and the two size caps, builds the participation matrix, reports its shape, and draws the heatmap.

**`do_channelstats`** — computes the per-channel statistics and plots one role.

**`do_pairgraph`** — asks for the threshold, roles and caps, computes the pairs and draws them.

**`do_roleproportions`** — asks how many channels and whether to normalize, then plots the role split.

**`do_topdegree`** — asks the graph settings and prints the highest-degree channels as a table.

Each of these wraps its work in a `try` that logs the failure and prints one line, so a graph that cannot be built does not end the session.


#### `class YTCMTubeScopeCommands`

**`do_tubescope`** — runs the whole module inside a collector.

**`do_likesgraph`**, **`do_moodline`**, **`do_activityline`**,

**`do_sentimentdist`** — each loads the message frame, stops with a message when it is empty, asks for the parameters it needs, and draws one figure.

**`do_topcomments`** — asks how many and prints the most-liked comments, truncated.

**`do_sentimentmean`** — prints the mean sentiment and on how many records it was computed.

**`do_replyquote`** — prints the share of comments with replies, or explains which half of the data the current mode is missing.

**`do_interactiondensity`** — asks for the bin count and draws the score distribution.

**`do_channelline`**, **`do_weekdays`**, **`do_viewscorrelation`** — draw the participation timeline, the weekday breakdown, and views against discussion, the last two after asking about shares and log axes.


#### `class YTCMTubeTalkCommands`

**`do_tubetalk`** — runs the whole module inside a collector.

**`do_langdist`** — asks for the level, the top-N and normalization, and draws the language distribution.

**`do_langconf`** — asks for the top-N, normalization and the inference settings, and draws the language conflicts.

**`do_wordcloud`** — asks for the date range, n-gram size, extra stopwords, language filter, frequency cutoffs and feature cap, and draws the cloud.

**`do_topics`** — asks for the topic count, words per topic, frequency cutoffs and n-gram size, and runs the topic model.


### 3.28 `YTCM.py`

**`configure_logging()`** Creates `logs/`, adds a file handler at INFO with a timestamped filename and a console handler at ERROR. The console handler formats through `WithoutTraceback`, so the log keeps the stack and the screen keeps one line. Exits the process if logging cannot be set up at all.

**`pending_downloads(ids_file="video_ids.txt")`** How many videos are waiting and where that was read from: the corpus first, the ID file second. Returns `(count, source)`. Any failure to open the corpus is treated as zero rather than raised, because this runs before the prompt.

**`class YTCMShell`** The shell itself, built from the ten mix-in classes and `cmd.Cmd`.

  **`__init__(self)`** — sets the session state: no API, the search terms from the config, an empty ID list, no filter.

  **`cmdloop(self, intro=None)`** — prints the banner, says how many downloads are pending, then hands over to the standard loop with the banner suppressed so it is not printed twice.

  **`emptyline(self)`** — does nothing, instead of repeating the last command, which is what `cmd` does by default and is never what anyone wants here.

  **`do_EOF(self, _)`** — ends the session on Ctrl-D.

  **`onecmd(self, line)`** — runs a command and turns the four interesting failures into something survivable: Ctrl-C says so and returns to the prompt, EOF ends the session when the input is a pipe, a broken pipe ends quietly, and anything else is logged with its traceback and reported as one line.

  **`precmd(self, line)`** — maps `quit` and `q` onto `exit`.

  **`default(self, line)`** — names the unknown command and points at `help`.

**`parse_arguments()`** Parses the command line. There is one flag and it is the default, so this exists to give `--help` something to print.

**`main()`** Sets the console encoding, warns when a live API key sits in a git repository, and starts the shell. Catches Ctrl-C at the top level and logs anything else.
