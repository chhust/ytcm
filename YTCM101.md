**This file has been automatically generated using Claude Opus 5 on Sept 19, 2026. Please inform us when you find any factual errors.**


# YTCM101 — Using YTCM

This is the user manual. It covers installation, setup, a full run from the first search to the last plot, and every command the shell has. For the code itself, see `YTCM102.md`.


## 1. Before you start


### 1.1 Installing

YTCM needs Python 3.12 or newer. Get the files, then install the dependencies:

```
pip install -r requirements.txt
```

If you use `uv`, `uv sync` reads `uv.lock` and installs the pinned set of versions this project resolves to.

Start the shell from the project directory:

```
python YTCM.py
```

You can also install the package itself with `pip install -e .`, which gives you an `ytcm` command you can call from anywhere. Either way the shell behaves the same.

Install YTCM in a local folder. Not Dropbox, not iCloud, not OneDrive. SQLite and file-syncing services do not get along, and a half-synced database is a lost database.


### 1.2 The API key, and what a crawl costs

Searching and downloading need your own YouTube Data API key. Analysis of a corpus you already have does not.

To get one, go to the Google Cloud Console at https://console.cloud.google.com/, make a project, enable "YouTube Data API v3" under APIs & Services, and create an API key under Credentials. Google's own instructions are at https://developers.google.com/youtube/v3/getting-started.

Put the key in a file called `YOUTUBE.API` in your working directory, on one line and nothing else. `YOUTUBE.API.example` shows the shape. YTCM reads the file at `connect`. If the file is missing, `connect` asks you to paste the key instead.

**The quota is what limits you, not the speed of your computer.** A key is normally good for 10,000 units a day, and the clock resets at midnight Pacific time. The prices are not all the same:

| call | units | when it happens |
|---|---|---|
| a search page | 100 | every `search`, once per page of results |
| video metadata | 1 | per batch of up to 50 videos in `download` |
| a page of comments | 1 | up to 100 comments |
| a page of replies | 1 | up to 100 replies |
| channel data | 1 | per batch of up to 50 authors |

A dozen searches cost more than a thousand comments. Plan for that. A large crawl is measured in days, not hours, and you spend most of them waiting for the quota to reset. When it runs out, `download` stops and says so, and the videos it has not reached stay on the list. Start it again the next day and it carries on where it stopped.


### 1.3 Ollama and Qwen, if you want semantic search

`embed` and `semsearch` rank a corpus by meaning rather than by exact words. They need a local embedding model. YTCM expects Ollama, running on `http://localhost:11434`, with `qwen3-embedding:0.6b` pulled:

```
ollama pull qwen3-embedding:0.6b
```

Nothing else in YTCM needs it. If you never run `embed`, you never need Ollama.


### 1.4 The working directory

YTCM writes everything relative to the directory you start it in. Use one directory per project. `status` prints which one you are in.

What appears there as you work:

| | |
|---|---|
| `corpus.db` | the corpus. An SQLite database, written on the first download or import. |
| `ytcm.key` | the pseudonymization key, mode 600. Made by `generatehash`. |
| `private_lookup/` | the re-identification store, only if you set `LOOKUP_DIR`. |
| `video_ids.txt` | the video IDs waiting to be downloaded. |
| `channel_data_cache.json` | author names and subscriber counts, so a second run does not pay for them again. |
| `logs/` | one log file per shell session. |
| `figures/<tool>/` | the plots each analysis command draws, as PNG. |
| `output/` | the tables and network files the analyses write. |
| `Comments.json`, `.csv`, `.html`, `.gexf` | whatever you export. |
| `COMMENTS/` | one text file per video, from `export txt`. |
| `Markers.txt` | the hashed handles TubeTalk found. |

Everything in that list holds data about real people, except the log files, which usually do not but sometimes carry a snippet of comment text. Keep the directory off shared drives and out of backups that travel.


### 1.5 Settings: `src/ytcm/config.py`

The top of `config.py` is meant to be edited. The bottom half is not; it is marked and you can leave it alone.

**Search terms.** `PRIMARY_SEARCH_TERMS` is a list of lists. Each inner list is one search, and its words are combined. `SECONDARY_SEARCH_TERMS` is a flat list, and every secondary term is added to every primary list in turn, so two primaries and three secondaries make six searches. `EXCLUDED_TERMS` drops results whose title contains any of them. `SEARCH_YEAR` limits a search to one year. You can change all four from the shell as well, with `primary`, `secondary`, `exclude` and `year`, and the shell versions only last for the session.

**Where things live.** `DB_PATH` is the corpus file, `DEFAULT_CORPUS` the tag new downloads are filed under, `API_KEY_FILE` and `HASH_KEY_FILE` the two key files.

**`LOOKUP_DIR`** decides whether re-identification is possible at all. It is unset by default, and then no store is kept: pseudonyms are one-way and `lookup` cannot answer. Set it to a directory name and YTCM records which token belongs to which channel. See section 2.11 before you do.

**Text handling.** `MENTION_UNKNOWN_POLICY` says what happens to an @-mention of a name the corpus does not know: `label` hashes it, `redact` replaces it with `[user]`, `keep` leaves it alone. `EMAIL_POLICY` is `hash` or `redact`.

**Language.** `LANGUAGE_MIN_CHARS` and `LANGUAGE_MIN_CONFIDENCE` decide when `language` gives up and writes "unknown" instead of guessing. Short comments and emoji are not worth a guess.

**Analysis.** `TUBESCOPE_MODE` is `c`, `r` or `cr` and decides whether counts and plots cover comments, replies, or both. `TUBECONNECTSETTINGS` defines the corpora TubeConnect compares, as a list of `(filter, short label, long name)` triples; the filter is a dict with `language` or `corpus` in it. **TubeConnect does nothing useful until you have edited this for your own data.** `CACHING` turns the author cache on and off. `EMBEDDINGS` and `OLLAMA_URL` point at the embedding model. `CONFIRM_ABOVE_SECONDS` sets when `embed` stops to ask whether you really want to wait that long.


## 2. A run, from start to finish


### 2.1 Look first

```
YTCM> status
```

`status` prints the working directory, whether the API is connected, the current search terms, whether a key exists, and what is in the corpus. Run it when you arrive and when something surprises you. It is the one command that never changes anything.

`howto` prints the short version of this section.


### 2.2 Make the key

```
YTCM> generatehash
```

This writes `ytcm.key` and must happen **before** the first download. Without it, `download` refuses to start.

The key turns a channel ID into a token, always the same token for the same ID. It never leaves your machine. Two corpora made with the same key can be joined; corpora made with different keys cannot, and YTCM refuses to mix them rather than producing nonsense.

Keep the key. If you lose it you can still read and analyze the corpus, but you can never extend it or match it against another one.


### 2.3 Say what to look for

```
YTCM> primary ethel smyth
YTCM> secondary opera; interview
YTCM> exclude reaction, shorts
YTCM> year 2024
```

`primary` takes words separated by spaces or commas; a semicolon starts a second search. `secondary` and `exclude` take a flat list. `year` takes one year, from 2005 to the current one.

Called with no arguments, each of them prints what is set now. `--clear` empties it. `status` shows the combinations that will actually be searched.

Search is title-based and narrow on purpose: a result is kept only if every term of its combination appears in the title. Expect to run several searches with different years and different secondary terms, and expect most of them to return very little.


### 2.4 Connect and search

```
YTCM> connect
YTCM> search
```

`connect` reads the key file and opens the API. `search` runs every combination for the set year and writes the IDs it found to `video_ids.txt`.

If IDs are already waiting there, `search` asks whether to add the new ones or replace the file. Adding is usually what you want while you are still collecting.

Remember: every search page costs 100 units. Ten searches are a tenth of your day.


### 2.5 Download

```
YTCM> download
```

This is the long part. YTCM fetches metadata in batches of 50, then the comments and replies of each video one at a time, and writes each video into the corpus as it arrives. A progress bar shows the comments coming in.

It can be interrupted safely. Close the window, lose the connection, run out of quota — what has arrived is in the corpus, and the rest stays marked as pending. Start the shell again and type `download`, and it picks the pending list out of the corpus by itself.

Videos that the API refuses temporarily stay pending and are tried again. Videos that are gone, private, or have comments switched off are marked once and not retried.

`download --corpus TAG` files the videos under a tag other than the default. A video can belong to several corpora at once; it is stored once and pointed at twice.

`load` reads an ID list from a file, `addid` adds a single video by ID, and `update` puts videos you already have back on the list to fetch them again — useful when a discussion has continued since you crawled it, expensive when the corpus is large.


### 2.6 Language and sentiment

```
YTCM> language
YTCM> sentiment
```

`language` runs langdetect over every message that has no language yet and writes a two-letter code, or "unknown" when the text is too short or the guess too weak. It also labels video titles and descriptions. `language rebuild` does the whole corpus again; `--min-chars` and `--min-confidence` change the thresholds for one run.

`sentiment` scores every message with TextBlob and VADER. **Both are English-only**, so a message in another language gets no score, and the tool says how many did. Run `language` first — `sentiment` refuses to start otherwise, because it needs to know which messages are English.

`review` walks you through the videos one by one so you can delete what does not belong in the corpus. It is a one-way door: a deleted video and its comments are gone.


### 2.7 Filtering

```
YTCM> filter --and piano sonata
YTCM> hits
YTCM> filtersave
YTCM> unfilter
```

`filter` narrows the corpus to the videos whose description, comments or replies contain your terms. `--and` needs all of them, `--or` any of them; `--and` is the default. Matching ignores case beyond ASCII, so `äpfel` finds `ÄPFEL`.

By default a term matches anywhere, so `art` also matches `party`. `--words` matches whole words only. For scripts without word breaks, Japanese or Chinese for example, whole-word matching cannot apply and the tool says so and searches for the substring instead.

Run `filter` again and it narrows what you already have rather than starting over. `hits` prints the matching passages with the term highlighted. `filtersave` writes the result to `Filtered.json`. `unfilter` drops it.


### 2.8 Semantic search

```
YTCM> embed
YTCM> semsearch "nostalgia for a lost home"
```

`embed` gives every message a vector from the local model. It tells you how long that will take before it starts, and it can be stopped and resumed: messages that already have a current vector are skipped. Change a text and its vector becomes stale and is recomputed on the next run.

`semsearch` ranks the whole corpus against a concept and writes the ranking to `semsearch.csv`, with a manifest of what was searched and how. It shows the top rows on screen.

Read the ranking for what it is. Every message gets a score, so there is always a "best" one, even in a corpus that contains nothing of the sort. For a claim you can defend in print, use `filter`.


### 2.9 Analysis

Five commands run a whole module and need no input at all. They print their numbers, show each plot on screen, save every plot into `figures/<tool>/`, and write their tables into `output/`. Start one and go to lunch.

```
YTCM> tubescope        counts, distributions, everything over time
YTCM> tubetalk         languages, word cloud, topic model, social media markers
YTCM> tubegraph        the channel network: pairs, communities, centrality, replies
YTCM> tubegraphfull    the same plus a parameter sweep. Hours, not minutes.
YTCM> tubeconnect      overlap between the corpora named in TUBECONNECTSETTINGS
```

Every part is also available on its own, and those versions ask you for the parameters: `likesgraph`, `moodline`, `activityline`, `sentimentdist`, `interactiondensity`, `channelline`, `weekdays`, `viewscorrelation`, `topcomments`, `sentimentmean`, `replyquote` for TubeScope; `langdist`, `langconf`, `wordcloud`, `topics` for TubeTalk; `interactiongraph`, `degreedist`, `centralities`, `structure`, `communities`, `replygraph`, `cooccurrence`, `channelstats`, `pairgraph`, `roleproportions`, `topdegree` for TubeGraph.

Two warnings about the network numbers. First, the co-occurrence network is built by connecting everyone who appeared under the same video, which makes a clique out of every comment section. Clustering and modularity are therefore high whatever the data does, which is why `communities` and `structure` say so beside the number and compare it against a null model that keeps every channel's and every video's size fixed. Read the pair, not the value. Second, `interactiondensity` is an experiment, not a measure; it is labelled as one in the code and should not carry an argument.


### 2.10 Getting data in and out

```
YTCM> exportjson
YTCM> export all
YTCM> import Comments.json --corpus B
```

`exportjson` writes the corpus back out as `Comments.json`, or as JSONL with `--jsonl`, which is the better choice for anything large. `--corpus` and `--years` narrow what goes out. This is the round trip: what comes out can go back in without losing a field.

`export` writes the derived formats: `html` to read, `csv` for a spreadsheet or pandas, `gephi` for Gephi, `txt` for one file per video, `all` for everything.

`import` reads a `Comments.json` back in under a corpus tag. `--mode fill-missing` only fills gaps, `merge` lets the incoming file win field by field, `replace` throws that corpus away and rebuilds it. A refused import changes nothing: if the file turns out to be malformed halfway through, the corpus is as it was.

`validate` prints the structure of an exchange file, `duplicates` looks for repeated IDs in one, and `purgedupes` writes a cleaned copy next to it, keeping the fuller of each pair.


### 2.11 Pseudonyms, and how to finish

YTCM replaces channel IDs and display names with keyed tokens while it downloads, so the real ones never reach the corpus in the first place. Comment texts are a different matter: they are stored as they were written, and people write names, handles and e-mail addresses into them.

To clean the texts, take the corpus out and put it back in:

```
YTCM> exportjson
YTCM> import Comments.json --sanitize --mode merge
```

`--sanitize` hashes e-mail addresses, channel IDs, and @-mentions it can match against names it knows. Then delete the JSON file you just wrote, because it still contains the unsanitized text.

`lookup TOKEN` answers who is behind a pseudonym, and needs `LOOKUP_DIR` to be set. `lookup --reverse VALUE` works the other way and needs only the key.

When the analysis is done, decide how far to close the door:

- `deletelookup` removes the re-identification store. Every analysis keeps working; only `lookup` stops.
- `deletehash` destroys the key. After that no token can be computed at all, the corpus can never be extended or joined with another, and `lookup --reverse` stops too. It asks you to type the fingerprint to confirm.
- `reset` deletes the exported files. `reset --all` deletes the corpus with them.

Also delete `logs/`, the exported files, and anything under `figures/` and `output/` that you are not keeping. An error message can carry a line of comment text.

None of this makes a corpus publishable. It makes a corpus you can work with locally. What you may keep, and for how long, is a question for your jurisdiction and your ethics committee, not for this tool.


## 3. All 66 commands

Alphabetical. `help` lists them in the shell, `help NAME` prints the syntax.


### `activityline`

`activityline` — Comment activity over time: the raw daily count, a rolling average over a window scaled to the corpus, and the ten busiest days.


### `addid`

`addid [VIDEO_ID]` — Put one video on the download list by hand. Asks for the ID if you do not give one, checks that it looks like a YouTube ID, and says if the video is already in the corpus.


### `centralities`

`centralities` — Degree, betweenness and eigenvector centrality for the interaction graph. Asks which roles to include and whether to compute the slow measures; betweenness is approximated on graphs above 500 nodes.


### `channelline`

`channelline` — Two lines over time: how many channels were active per day, as a rolling average, and how many distinct channels had appeared by then.


### `channelstats`

`channelstats` — How often each channel uploaded, commented and replied, and on how many videos it appears. Asks for a role and a top-N and plots that.


### `communities`

`communities` — Community detection on the interaction graph, with a null model that keeps every channel's and every video's size fixed. Reports modularity beside what chance gives, and offers to write the channel-to-community table to a file.


### `connect`

`connect` — Read `YOUTUBE.API` and open the YouTube API. Asks you to paste the key if the file is missing. Needed for `search` and `download`, for nothing else.


### `cooccurrence`

`cooccurrence` — Heatmap of how similar channels are by the videos they appear under, with an optional dendrogram. Asks for the roles and how many channels to build the matrix over.


### `degreedist`

`degreedist` — Histogram of node degrees in the interaction graph. Asks where to cap the bars and how many bins, and prints the exact count above any capped bar.


### `deletehash`

`deletehash` — Destroy the pseudonymization key. Afterwards no token can be computed, the corpus can never be extended or joined, and `lookup --reverse` stops. Asks you to type the fingerprint. Cannot be undone.


### `deletelookup`

`deletelookup` — Delete the re-identification store. Every analysis keeps working; only `lookup` stops answering. Cannot be undone.


### `download`

`download [--corpus TAG]` — Fetch metadata, comments and replies for the video IDs on the list and write them into the corpus. Needs the key and a connection. Can be interrupted and resumed; what arrived stays, the rest stays pending.


### `duplicates`

`duplicates [FILE]` — Report IDs that appear more than once in an exchange file. The database cannot hold a duplicate, so this is about files, not about the corpus.


### `embed`

`embed [--corpus TAG] [--limit N] [--yes]` — Give every message a vector from the local model so `semsearch` can rank it. Estimates the time first and asks; `--yes` skips the question, `--limit` stops after N messages. Resumable.


### `exclude`

`exclude TERM ...` — Words that must not appear in a video title. Separated by spaces or commas. Without arguments it shows the current list, `--clear` empties it.


### `exit`

`exit`, `quit`, `q` — Leave the shell.


### `export`

`export [html|gephi|csv|txt|all]` — Write the corpus out in a derived format: readable HTML, a CSV row per message, GEXF for Gephi, or one text file per video under `COMMENTS/`. Default is `all`.


### `exportjson`

`exportjson [FILE] [--corpus TAG] [--years FROM-TO] [--jsonl]` — Write the corpus back out as an exchange file, losing nothing. `--jsonl` writes one video per line, which is what you want for anything large.


### `filter`

`filter [--and|--or] [--words] TERM ...` — Narrow the corpus to videos whose description, comments or replies contain the terms. `--and` is the default. `--words` matches whole words. Run again to narrow further.


### `filtersave`

`filtersave [FILE]` — Write the current filter result to `Filtered.json`, or to the file you name.


### `generatehash`

`generatehash` — Create the pseudonymization key in `ytcm.key`. Do this before the first download. Replacing an existing key gives every channel a different pseudonym and is confirmed separately.


### `hits`

`hits` — Print the passages that matched the last `filter`, with the term highlighted and a little context on each side.


### `howto`

`howto` — The recommended order of commands, from `status` to `download`, and how to resume an interrupted crawl.


### `import`

`import FILE [--corpus TAG] [--mode fill-missing|merge|replace] [--sanitize] [--pseudonymize]` — Read an exchange file into the corpus. `fill-missing` only fills gaps, `merge` lets the file win, `replace` rebuilds that corpus. `--sanitize` cleans the texts, `--pseudonymize` hashes identifiers a foreign corpus still holds.


### `info`

`info` — Who wrote YTCM, which version this is, and what the four analysis modules are for.


### `interactiondensity`

`interactiondensity` — Distribution of an experimental per-video score built from replies per comment and the share of comments that got any. Experimental means experimental; do not quote it.


### `interactiongraph`

`interactiongraph` — Draw the undirected network of channels that appeared under the same videos. Asks for roles, edge weight, and how many nodes to draw.


### `langconf`

`langconf` — Where a comment is in a different language than its video, and a reply in a different language than its comment. Bar charts and a heatmap.


### `langdist`

`langdist` — Language distribution across videos, comments or replies, as counts or shares.


### `language`

`language [rebuild] [--min-chars N] [--min-confidence F]` — Detect the language of every message that has none yet, and of titles and descriptions. `rebuild` does the whole corpus again. Short or ambiguous texts are labelled "unknown", and the command says how many.


### `likesgraph`

`likesgraph` — Histogram of likes per message, in buckets. Asks where to cap the bars, how many bins, and an optional range.


### `load`

`load [FILE]` — Read video IDs from a file into the session, skipping lines that are not YouTube IDs.


### `lookup`

`lookup TOKEN` | `lookup --reverse CHANNEL_ID|NAME|EMAIL` — Ask who is behind a pseudonym, or which pseudonym a value has. The forward direction needs `LOOKUP_DIR` to be set; the reverse direction needs only the key.


### `migratehash`

`migratehash MAPFILE` — Move a corpus that used the old `UserN` scheme onto keyed tokens, keeping every join that already worked. For old projects only.


### `moodline`

`moodline` — Average sentiment per day over time, with a rolling average, mean and median. Days without a scored message leave a gap rather than a zero.


### `pairgraph`

`pairgraph` — The channel pairs that appear under the same videos most often, drawn as a network with clusters coloured. Asks for the minimum co-occurrence.


### `primary`

`primary TERM ...` — The main search terms. Words are combined; a semicolon starts a second, separate search. Without arguments it shows what is set, `--clear` empties it.


### `purgedupes`

`purgedupes [FILE]` — Write a cleaned copy of an exchange file next to it, keeping the fuller of each duplicated pair. The original is not touched.


### `replygraph`

`replygraph` — The directed network of who replied to whom. Asks whether to include self-replies and how light an edge may be.


### `replyquote`

`replyquote` — The share of comments that got at least one reply. Needs both comments and replies in the frame, so it says nothing under `TUBESCOPE_MODE = 'c'`.


### `reset`

`reset [--all]` — Delete the exported files and the author cache. `--all` also deletes the corpus. Asks you to type "yes". Cannot be undone.


### `review`

`review` — Walk through the videos one at a time and delete the ones that do not belong in the corpus. Deletion takes the video's comments with it.


### `roleproportions`

`roleproportions` — How each channel's activity divides between uploading, commenting and replying, as counts or as shares.


### `search`

`search [FILE]` — Run every combination of primary and secondary terms for the set year and write the video IDs found to `video_ids.txt`. Asks whether to add to an existing list or replace it. Costs 100 quota units per page of results.


### `secondary`

`secondary TERM ...` — Terms added to each primary search in turn. Without arguments it shows what is set, `--clear` empties it.


### `semsearch`

`semsearch "CONCEPT" [--top-k N] [--min-score F] [--corpus TAG] [--out FILE] [--jsonl]` — Rank the corpus by closeness to a concept and write the ranking to a file. It is a ranking, not a filter: everything gets a score. Needs `embed` to have run.


### `sentiment`

`sentiment [rebuild]` — Score every message with TextBlob and VADER. English only; everything else is left unscored. Refuses to start until `language` has run.


### `sentimentdist`

`sentimentdist` — Distribution of sentiment scores, with mean, median, and how many messages have no score at all.


### `sentimentmean`

`sentimentmean` — The average sentiment over the corpus, and on how many of the messages it was computed.


### `status`

`status` — Working directory, API connection, search terms, key and lookup store, and what the corpus holds. Rebuilds missing indexes if it finds any.


### `structure`

`structure` — The shape of the interaction graph: components, density, clustering, assortativity, k-core, PageRank, and optionally diameter and average path length. Then the same for the reply network.


### `topcomments`

`topcomments` — The most-liked comments, with their like count and the first hundred characters of the text.


### `topdegree`

`topdegree` — The channels with the most connections in the interaction graph, as a table.


### `topics`

`topics` — LDA topic model over the corpus: top words per topic, each topic's share, and how the shares move month by month. Asks for the number of topics and the frequency cutoffs.


### `tubeconnect`

`tubeconnect [overlaps|interaction|graph|plots|null|all] [--activity-null]` — Compare the corpora named in `TUBECONNECTSETTINGS`: who is active in more than one, how the overlap develops, and whether it is more than chance. Writes its figures and tables without asking.


### `tubegraph`

`tubegraph` — The whole network analysis in one go: channel statistics, co-occurrence heatmap, frequent pairs, the interaction graph, degree distribution, structure, communities, centrality, and the reply network. Runs without input.


### `tubegraphfull`

`tubegraphfull` — `tubegraph` plus a parameter sweep: the reply network at four minimum weights, channel pairs at four thresholds, the interaction graph over 8,000 and 30,000 channels. Hours, not minutes.


### `tubescope`

`tubescope` — The whole statistical analysis in one go: activity over time, participation, likes, sentiment over time and by distribution, interaction density, weekdays, uploads, views against discussion, and view counts. Runs without input.


### `tubetalk`

`tubetalk` — The whole language analysis in one go: social media markers, language distribution, language conflicts, word cloud, topic model. Runs without input.


### `unfilter`

`unfilter` — Drop the current filter and go back to the whole corpus.


### `update`

`update [FILE] [--stale-days N]` — Put videos already in the corpus back on the download list to fetch them again. `--stale-days` limits it to the ones not refreshed for that long. Costs quota like a first crawl.


### `validate`

`validate [FILE] [silent]` — Read an exchange file and print the structure it found, field by field. `silent` checks without printing the tree.


### `viewscorrelation`

`viewscorrelation` — Views against discussion volume per video, on logarithmic axes, with a fitted trend and the most talked-about outliers labelled.


### `weekdays`

`weekdays` — Uploads, comments and replies by weekday, as counts or as shares.


### `wordcloud`

`wordcloud` — A word cloud over the comment texts. Asks for a date range, n-gram size, extra stopwords, a language filter and the frequency cutoffs. The terms you searched for are dropped, since every video has them by definition.


### `year`

`year YYYY` — The year `search` looks in, from 2005 to the current one. Without an argument it shows what is set.
