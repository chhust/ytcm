# This is a collection of mix-in classes for the shell plus their helper functions


import itertools
import json
import logging
import os
import re
logger = logging.getLogger(__name__)

from datetime                   import datetime

from ytcm.api_utils import get_video_ids, init_youtube_service, load_api_key

from ytcm.config import (API_KEY_FILE, CACHING, COMMENTS_CSV, COMMENTS_FOLDER, COMMENTS_GEXF_ON,
                                        COMMENTS_GEXF_OFF, COMMENTS_HTML, COMMENTS_JSON, DB_PATH, DEFAULT_CORPUS,
                                        HASH_KEY_FILE, LOOKUP_DIR,
                                        SEMSEARCH_TOP_K,
                                        FILTER_JSON,
                                        SEARCH_PAGE_SIZE,
                                        VERSION)
from ytcm.data_enrichment_utils import detect_languages, sentiment_analysis, review
from ytcm.export_utils import convert_json_to_csv, convert_json_to_gephi, convert_json_to_html
from ytcm.filter_utils import filter_data, hits
from ytcm.helper_utils import (check_pending_downloads, clean_dupes, find_duplicate_ids, get_df_comments,
                                        is_youtube_video_id, load_json_file, duplicate_check, validate_data_structure,
                                        save_json_file)
from ytcm.io_utils import delete_all_files, refuse_outside
from ytcm.platform_utils import plural

from ytcm import identity as ident
from ytcm import sanitize as sanitize
from ytcm.bridge import export_json, export_txt, import_json, migrate_legacy_map
from ytcm.embed import (embed_corpus, preview, write_results,
                                        search as semantic_search)
from ytcm.db import (get_conn, mark_pending, missing_indexes, create_indexes,
                     pending_videos, requeue, NotADatabase, ReadOnlyDatabase)
from ytcm.processing_utils import generate_search_list, process_videos
from ytcm import platform_utils as platform
from ytcm import repo as repo
from ytcm import results as results

from ytcm.tubeconnect import tubeconnect, TUBECONNECT_MODES

from ytcm.tubegraph import (tubegraph, tubegraphfull, build_interaction_graph, plot_network_graph,
                                        compute_centrality_measures, plot_centrality_results,
                                        graph_structure, directed_structure,
                                        community_structure,
                                        COMMUNITY_METHODS,
                                        channel_video_participation_matrix, plot_channel_clustering_heatmap,
                                        channel_occurrence_stats, plot_top_channels, top_connected_channels,
                                        plot_channel_role_proportions,
                                        plot_channel_degree_distribution, build_reply_network, plot_reply_network,
                                        frequent_channel_pairs, plot_channel_pair_network)
from ytcm.tubescope import (tubescope, plot_comment_likes_distribution, analyze_sentiment_over_time,
                                        plot_sentiment_over_time, group_comments_by_date, plot_comments_over_time,
                                        get_most_liked_comments, calculate_average_sentiment, analyze_replies,
                                        plot_interaction_density_distribution, plot_sentiment_distribution,
                                        plot_participation_timeline, plot_interactions_by_weekday,
                                        plot_views_vs_comments, mode_label, resolve_mode)
from ytcm.tubetalk import (tubetalk, plot_language_distribution, plot_language_conflicts,
                                        run_wordcloud, run_topics)


def reject_unknown_flags(parts, known, syntax):
    unknown = [part for part in parts if part.startswith("--") and part not in known]
    if unknown:
        print(f"Unknown: {' '.join(unknown)}.")
        print(syntax)
        return True
    return False


def ask_graph_settings():
    known_roles = ("uploader", "commenter", "replier")
    role_text = input("Include which roles (comma-separated: uploader,commenter,replier)? [all] ").strip()
    roles = tuple(reply.strip().lower() for reply in role_text.split(",") if reply.strip()) \
        if role_text else known_roles
    unknown_roles = [role for role in roles if role not in known_roles]
    if unknown_roles:
        print(f"Not a role: {', '.join(unknown_roles)}. Using all three.")
        roles = known_roles

    exclude = not (input("Include the uploader's own channel (y/n)? [n]: ")
                   or "n").strip().lower().startswith("y")

    def whole_number(question, default):
        try:
            return int(input(question) or default)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            return default

    weight = whole_number("Drop edges shared by fewer than how many videos? [2]: ", 2)
    minimum = whole_number("Ignore channels seen on fewer than how many videos? [2]: ", 2)

    return {"roles": roles, "exclude_uploader": exclude, "min_weight": weight,
            "min_videos_per_channel": minimum}


class YTCMCoreCommands:
    def do_info(self, _):
        """
        Show info about the program.
        """

        print("\nYTCM - YouTube Comment Miner")
        print(f"Written by Christoph Hust and Natasha Niaz. Version {VERSION}, MIT licensed.")
        print("This is a modular YouTube comments downloader and analyzer.")
        print("It is designed to be used interactively or in combination with other tools like Gephi or MAXQDA,")
        print("producing fast results with various export formats.\n")
        print("Also incorporated are TubeConnect, a series of tools for network intersection analysis,")
        print("                      TubeGraph, a series of tools for network analysis,")
        print("                      TubeScope, another set of tools for statistical data analysis,")
        print("                  and TubeTalk, a set of tools for linguistic analysis.\n")


    def do_howto(self, _):
        """
        Show information how to use the program.
        """

        print("\nRecommended order:")
        print("1. Check the current status of the program by calling 'status'.")
        print("2. If there is no pseudonymization key yet, make one by calling 'generatehash'.")
        print("3. If necessary, change primary search terms by calling 'primary' plus parameters.")
        print("4. If necessary, change secondary search terms by calling 'secondary' plus parameters.")
        print("5. If necessary, change excluded terms by calling 'exclude' plus parameters.")
        print("6. If necessary, change search year by calling 'year' plus parameter.")
        print("7. Do the API setup by calling 'connect'.")
        print("8. Check everything is set up correctly by calling 'status' again.")
        print("9. Search for video IDs by calling 'search'.")
        print("10. Download the comments for the video IDs by calling 'download'.")
        print("\n   If the download process is interrupted, resume it by")
        print("   a. re-starting after the API quota has been reset,")
        print("   b. loading the list of un-processed video IDs by calling 'load', and")
        print("   c. continuing from step 10.\n")

        print("Recommended workflow before analysis:")
        print("1. Review the downloaded comments by calling 'review'.\n")
        print("2. Detect the language of the comments by calling 'language'.\n")
        print("3. Perform sentiment analysis on the comments by calling 'sentiment'.\n")


    def do_status(self, _):
        """
        Show the current status.
        Syntax: status
        """

        c_status = ["off", "on"]

        print(f"Working directory     : {os.getcwd()}")
        print(f"API status            : {'ready' if self.youtube else 'not initialized'}")
        print(f"Primary search terms  : {self.primary_terms}")
        print(f"Secondary search terms: {self.secondary_terms}")
        combined = generate_search_list(self.primary_terms, self.secondary_terms)
        print(f"Combined search terms : {combined}")
        print(f"Excluded terms        : {self.excluded_terms}")
        print(f"Search year           : {self.search_year}")
        print(f"Caching               : {c_status[CACHING]}")
        if ident.have_key():
            try:
                print(f"Pseudonymization key  : {ident.key_path()}, "
                      f"fingerprint {ident.fingerprint()}")
            except (ident.NoKey, OSError) as e:
                logger.error(f"status: the pseudonymization key is unusable: {e}.")
                print(f"Pseudonymization key  : UNUSABLE - {e}")
        else:
            print("Pseudonymization key  : MISSING - 'generatehash' before downloading")
        if not LOOKUP_DIR:
            print("Re-identification     : no store - LOOKUP_DIR unset in config file")
        elif ident.lookup_available():
            print(f"Re-identification     : available, {LOOKUP_DIR}")
        else:
            print(f"Re-identification     : no store yet at {LOOKUP_DIR}")

        print(f"\nDatabase              : {DB_PATH}", end="")
        if not os.path.exists(DB_PATH):
            print("  (not created yet - it appears on the first download or import)")
            return
        print(f"  ({os.path.getsize(DB_PATH) / 1e6:,.1f} MB)")

        connection = get_conn()
        try:
            absent = missing_indexes(connection)
            if absent:
                print(f"  {len(absent)} {plural(len(absent), 'index', 'indices')} missing. "
                      f"Rebuilding ... ", end="", flush=True)
                create_indexes(connection)
                print("done.")

            for table in ("videos", "messages", "channels", "annotations",
                          "video_annotations", "mention_map", "embeddings",
                          "provenance"):
                count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"  {table:<20}{count:>12,}")
            corpora = connection.execute(
                "SELECT corpus, COUNT(*) FROM video_corpora GROUP BY corpus "
                "ORDER BY 2 DESC").fetchall()
            if corpora:
                print("  corpora             " +
                      ", ".join(f"{name} ({count:,})" for name, count in corpora))
            waiting = connection.execute(
                "SELECT status, COUNT(*) FROM download_state GROUP BY status").fetchall()
            if waiting:
                print("  downloads           " +
                      ", ".join(f"{state} ({count:,})" for state, count in waiting))
            last = connection.execute(
                "SELECT pass, finished_at, records_written FROM provenance "
                "ORDER BY run_id DESC LIMIT 1").fetchone()
            if last:
                print(f"  last pass           {last[0]} at {(last[1] or '')[:19]}, "
                      f"{last[2]:,} records")
        finally:
            connection.close()


    def do_validate(self, arg):
        """
        Validate a Comments.json.
        Syntax: validate [filename] [silent]
        """

        tokens = arg.split()
        silent = any(t.lower() == "silent" for t in tokens)
        rest = [t for t in tokens if t.lower() != "silent"]
        filename = rest[0] if rest else COMMENTS_JSON

        if not os.path.exists(filename):
            print(f"'{filename}' doesn't exist. Type 'status' to show the corpus.")
            return

        validate_data_structure(filename, verbose=not silent)


    def do_duplicates(self, arg):
        """
        Check a Comments.json for duplicate IDs.
        Syntax: duplicates [filename]
        """

        filename = arg.strip() if arg else COMMENTS_JSON
        if not os.path.exists(filename):
            print(f"'{filename}' doesn't exist. 'status' shows the corpus.")
            print("The database itself can never hold duplicate IDs.")
            return
        try:
            duplicate_check(filename)
        except json.JSONDecodeError as e:
            logger.error(f"duplicates: '{filename}' is not valid JSON: {e}.")
            print(f"'{filename}' is not valid JSON: {e}.")


    def do_purgedupes(self, arg):
        """
        Purge duplicate comments and replies from a Comments.json.
        Syntax: purgedupes [filename]
        """

        filename = arg.strip() if arg else COMMENTS_JSON
        if not os.path.exists(filename):
            print(f"'{filename}' doesn't exist.")
            return

        corpus = load_json_file(filename)     # dict
        dupes, _occ = find_duplicate_ids(corpus)
        report = clean_dupes(corpus, dupes)   # in-place cleanup

        # Named after the file it came from, so purging a second file doesnt
        # overwrite the result of the first.
        stem, extension = os.path.splitext(os.path.basename(filename))
        cleaned = os.path.join(os.path.dirname(filename) or ".",
                               platform.safe_filename(f"{stem}.cleaned.json"))
        try:
            refuse_outside(cleaned)
        except ValueError as e:
            logger.error(f"purgedupes: {e}.")
            print(str(e))
            return
        save_json_file(cleaned, corpus)

        removed = report["stats"]["removed_count"]
        print(f"{removed:,} duplicate {plural(removed, 'record')} removed, keeping the "
              f"fuller of each pair.")
        print(f"Written to '{cleaned}'. '{filename}' is untouched.")


    def do_reset(self, arg):
        """
        Delete the exported files, and with '--all', the corpus itself. USE WITH CAUTION!
        Syntax: reset [--all]
        """

        if reject_unknown_flags(arg.split(), ("--all",), "Syntax: reset [--all]"):
            return
        everything = arg.strip() == "--all"
        print("This deletes the exported files (JSON, CSV, HTML, GEXF, cache).")
        print("It can't be undone.")
        if everything:
            print(f"--all also deletes the corpus '{DB_PATH}' and everything in it.")
        else:
            print(f"The corpus '{DB_PATH}' is NOT deleted.")
            print("Use 'reset --all' for the corpus.")
        choice = input("Are you sure? Type 'yes' to reset, everything else to cancel: ").strip().lower()

        if choice != "yes":
            print("Reset canceled.")
            return

        delete_all_files()
        if everything:
            gone, blocked = platform.remove_files(
                [DB_PATH + suffix for suffix in ("", "-wal", "-shm", "-journal")])
            for path in gone:
                print(f"{path} deleted.")
            for path in blocked:
                print(f"Couldn't delete '{path}': file is still open. ")


    def do_exit(self, _):
        """
        Quit the program.
        Syntax: exit, quit, q
        """

        print("Goodbye!")
        return True


class YTCMDownloadCommands:
    def do_connect(self, _):
        """
        Get the API key and connect to the YouTube API.
        Syntax: connect
        """

        print("Setting up API key and YouTube access ... ", end="")
        self.api_key = load_api_key(API_KEY_FILE)
        if not self.api_key:
            self.api_key = input("No API key file found on disk. Enter API key manually: ").strip()
            if not self.api_key:
                logger.error("Error: no API key.")
                return

        self.youtube = init_youtube_service(self.api_key)
        if not self.youtube:
            logger.error("Couldn't connect to the YouTube API.")
            return

        print("done.")
        print("Connected to the YouTube API.")


    def do_download(self, arg):
        """
        Load data for current list of video IDs.
        Syntax: download [--corpus TAG]
        """

        parts = arg.split()
        if reject_unknown_flags(parts, ("--corpus",), "Syntax: download [--corpus TAG]"):
            return
        corpus = DEFAULT_CORPUS
        for flag, value in zip(parts, parts[1:]):
            if flag == "--corpus":
                corpus = value

        if not self.video_ids and os.path.exists(DB_PATH):
            try:
                connection = get_conn()
            except (NotADatabase, ReadOnlyDatabase) as e:
                logger.error(f"download: {e}.")
                print(str(e))
                return
            try:
                self.video_ids = pending_videos(connection)
            finally:
                connection.close()
            if self.video_ids:
                print(f"{len(self.video_ids):,} video IDs still pending in '{DB_PATH}'.")

        if not self.video_ids:
            print("No video ID list. Either do 'search' or 'load' first.")
            return

        if not self.youtube:
            print("No API connection. Use 'connect' first.")
            return

        if not ident.have_key():
            print("No pseudonymization key. Run 'generatehash' before downloading.")
            return

        print(f"Start downloading for {len(self.video_ids)} video IDs.")

        print(f"Writing into corpus '{corpus}'.")
        try:
            quota_exceeded = process_videos(self.youtube, self.video_ids, self.video_ids_file,
                                            corpus=corpus)
        except (ident.NoKey, ValueError, NotADatabase, ReadOnlyDatabase) as e:
            logger.error(f"download: {e}.")
            print(str(e))
            return

        try:
            connection = get_conn()
        except (NotADatabase, ReadOnlyDatabase) as e:
            logger.error(f"download: {e}.")
            print(str(e))
            return
        try:
            pending = set(pending_videos(connection, corpus))
            remaining_ids = [v for v in self.video_ids if v in pending]
            platzhalter = ",".join("?" * len(self.video_ids))
            unusable = [r[0] for r in connection.execute(
                f"SELECT video_id FROM download_state WHERE corpus = ? AND status = 'unavailable' "
                f"AND video_id IN ({platzhalter})", [corpus] + list(self.video_ids))] if self.video_ids else []
        finally:
            connection.close()

        if quota_exceeded:
            print("\nAPI quota exceeded. Download stopped early.")

        if remaining_ids:
            print(f"{len(remaining_ids)} video IDs could not be downloaded.")
            self.video_ids = remaining_ids
            print(f"List of remaining video IDs in '{self.video_ids_file}' has been updated accordingly.")
            if quota_exceeded:
                print("Resume with 'download' once the quota resets.")
        elif unusable:
            print(f"{len(unusable)} of {len(self.video_ids)} video IDs unavailable. "
                  f"Not retried.")
            self.video_ids = []
        else:
            print("All video IDs could be downloaded.")
            self.video_ids = []


class YTCMEnrichmentCommands:
    def do_generatehash(self, _):
        """
        Create the pseudonymization key.
        Syntax: generatehash
        """

        if ident.have_key():
            print(f"A key already exists, fingerprint {ident.fingerprint()}.")
            print("Replacing it gives every channel a different pseudonym. Existing data")
            print("is not rewritten and would no longer join.")
            if input("Replace it anyway? Type 'replace' to continue: ").strip().lower() != "replace":
                print("Kept the existing key.")
                return

        fingerprint = ident.generate_key(replace=True)
        print(f"Key written to '{HASH_KEY_FILE}', mode 600, fingerprint {fingerprint}.")
        print("Keep it secret, keep it safe!")
        if LOOKUP_DIR:
            print(f"Re-identification will be recorded in '{LOOKUP_DIR}'.")
        else:
            print("LOOKUP_DIR is unset.")


    def do_deletehash(self, _):
        """
        Destroy the pseudonymization key; the corpus keeps working but can't be extended later.
        Syntax: deletehash
        """

        if not ident.have_key():
            print("There is no key.")
            return

        print(f"This destroys key {ident.fingerprint()}.")
        print("This can't be undone. To proceed, use 'deletelookup'.")
        if input("Type the fingerprint to confirm: ").strip() != ident.fingerprint():
            print("Aborted.")
            return
        ident.delete_key()
        print("Key destroyed.")


    def do_lookup(self, arg):
        """
        Ask who is behind a pseudonym, or which pseudonym something has.
        Syntax: lookup TOKEN | lookup --reverse CHANNEL_ID|NAME|EMAIL
        """

        parts = arg.split()
        if not parts:
            print("Syntax: lookup TOKEN | lookup --reverse CHANNEL_ID|NAME|EMAIL")
            print("  A token may stand for a channel id, a display name or an e-mail")
            print("  address. --reverse needs only the key; the forward direction needs")
            print(f"  the store in LOOKUP_DIR (currently {LOOKUP_DIR or 'unset'}).")
            return

        try:
            if parts[0].startswith("--") and parts[0] != "--reverse":
                print(f"Unknown: {parts[0]}.")
                print("Syntax: lookup TOKEN | lookup --reverse CHANNEL_ID|NAME|EMAIL")
                return

            if parts[0] == "--reverse":
                if len(parts) < 2:
                    print("Syntax: lookup --reverse CHANNEL_ID|NAME|EMAIL")
                    return
                value = " ".join(parts[1:])
                if ident.is_token(value):
                    print(f"'{value}' is already a token. Drop --reverse to look it up.")
                    return
                print(f"{value} -> {ident.token(value)}")
                print("(computed from the key; no re-identification store was needed)")
                return

            token = parts[0]
            for prefix in (sanitize.MENTION_PREFIX, sanitize.EMAIL_PREFIX):
                if token.startswith(prefix) and ident.is_token(token[len(prefix):]):
                    token = token[len(prefix):]
                    break

            if not ident.lookup_available():
                print("No re-identification store. Either LOOKUP_DIR is unset, or the store")
                print("was deleted, or this corpus was never crawled with one.")
                return
            found = ident.resolve(token)
            if isinstance(found, dict):
                for kind, value in sorted(found.items()):
                    print(f"{token} -> {value}   ({kind})")
            elif found:
                print(f"{token} -> {found}")
            else:
                print(f"{token} is not in the store.")
        except ident.NoKey as e:
            logger.error(f"lookup: {e}.")
            print(e)


    def do_deletelookup(self, _):
        """
        End re-identification. The corpus and every analysis keep working.
        Syntax: deletelookup
        """

        if not ident.lookup_available():
            print("There is no re-identification store.")
            return
        print("This removes the way from a pseudonym back to a channel ID.")
        print("Every analysis keeps working, but 'lookup' stops. This can't be undone.")
        if input("Are you sure? Type 'yes' to continue: ").strip().lower() != "yes":
            print("Aborted.")
            return
        ident.delete_lookup()
        print("Deleted.")


    def do_migratehash(self, arg):
        """
        Move an old UserN corpus onto keyed pseudonyms, keeping every existing join.
        This is for legacy and backwards compatability only.
        Syntax: migratehash MAPFILE
        """

        if not arg:
            print("Syntax: migratehash MAPFILE")
            print("  MAPFILE is the old Anonymization_map.json, which maps a real")
            print("  channel id to its UserN token.")
            return
        try:
            migrated = migrate_legacy_map(arg.strip())
        except (FileNotFoundError, ident.NoKey, ValueError) as e:
            logger.error(f"migratehash: {e}.")
            print(e)
            return
        print(f"Migrated {migrated:,} channels.")
        print("UserN survives as channels.legacy_token; anything joining on it keeps")
        print("working. The map file is now the only route from UserN to a real id.")


    def do_review(self, _):
        """
        Do a manual review of the downloaded videos.
        Syntax: review
        """

        review()


    def do_language(self, arg):
        """
        Language detection.
        Syntax: language [rebuild] [--min-chars N] [--min-confidence F]

        Both thresholds decide what is labeled "unknown". They apply to this run only.
        """

        parts = arg.split()
        rebuild = any(part.lower() == "rebuild" for part in parts)
        settings = {}
        for flag, converter, name in (("--min-chars", int, "min_chars"),
                                      ("--min-confidence", float, "min_confidence")):
            if flag in parts:
                position = parts.index(flag)
                try:
                    settings[name] = converter(parts[position + 1])
                except (IndexError, ValueError):
                    print(f"{flag} needs a number, for example: language {flag} "
                          f"{'10' if converter is int else '0.90'}")
                    return
        unknown = [part for part in parts if part.startswith("--")
                 and part not in ("--min-chars", "--min-confidence")]
        if unknown:
            print(f"Unknown: {' '.join(unknown)}.")
            print("Syntax: language [rebuild] [--min-chars N] [--min-confidence F]")
            return

        detect_languages(force_rebuild=rebuild, **settings)


    def do_sentiment(self, arg):
        """
        Sentiment analysis.
        Syntax: sentiment [rebuild]
        """

        keyword = arg.strip().lower()
        if keyword == "rebuild":
            sentiment_analysis(force_rebuild=True)
        elif keyword:
            print(f"Unknown: {arg.strip()}.")
            print("Syntax: sentiment [rebuild]")
        else:
            sentiment_analysis()


class YTCMExportCommands:
    def do_import(self, arg):
        """
        Read a Comments.json into the database under a corpus tag.
        Syntax: import FILE [--corpus TAG] [--mode fill-missing|merge|replace] [--sanitize] [--pseudonymize]
        """

        parts = arg.split()
        if not parts:
            print("Syntax: import FILE [--corpus TAG] [--mode fill-missing|merge|replace]")
            print("               [--sanitize] [--pseudonymize]")
            print("  fill-missing (default) writes only where the database holds nothing,")
            print("  merge lets incoming values win per field,")
            print("  replace drops that corpus and rebuilds it from the file.")
            print("  --sanitize cleans e-mail addresses, channel IDs and mentions out of")
            print("  the text on the way in. Off by default.")
            print("  --pseudonymize replaces the real channel IDs of a corpus that")
            print("  still holds them.")
            return

        path = parts[0]
        corpus = DEFAULT_CORPUS
        mode = "fill-missing"
        clean = "--sanitize" in parts[1:]
        hashed = "--pseudonymize" in parts[1:]
        for flag, value in zip(parts[1:], parts[2:]):
            if flag == "--corpus":
                corpus = value
            elif flag == "--mode":
                mode = value

        unknown = [part for part in parts[1:] if part.startswith("--")
                   and part not in ("--corpus", "--mode", "--sanitize", "--pseudonymize")]
        if unknown:
            print(f"Unknown: {' '.join(unknown)}.")
            print("Syntax: import FILE [--corpus TAG] [--mode fill-missing|merge|replace]")
            print("               [--sanitize] [--pseudonymize]")
            return

        if (clean or hashed) and not ident.have_key():
            print("That needs the pseudonymization key. Run 'generatehash' first.")
            return

        if mode not in ("fill-missing", "merge", "replace"):
            print(f"Unknown mode '{mode}'.")
            return

        if mode == "replace":
            print(f"This deletes corpus '{corpus}' and rebuilds it from '{path}'.")
            if input("Are you sure? Type 'yes' to continue: ").strip().lower() != "yes":
                print("Aborted.")
                return

        try:
            written, duplicates = import_json(path, corpus=corpus, mode=mode,
                                              sanitize_text=clean, pseudonymize=hashed)
        except FileNotFoundError:
            logger.error(f"import: couldn't find '{path}'.")
            print(f"Couldn't find '{path}'.")
            return
        except OSError as e:
            logger.error(f"import: couldn't read '{path}': {e}.")
            print(f"Couldn't read '{path}': {e}.")
            return
        except ident.NoKey as e:
            logger.error(f"import: {e}.")
            print(str(e))
            return
        except ValueError as e:
            logger.error(f"import: {e}.")
            print(e)
            return

        print(f"Imported {written:,} {plural(written, 'row')} into corpus '{corpus}' "
              f"in mode '{mode}'"
              f"{', identifiers hashed' if hashed else ''}"
              f"{', text sanitized' if clean else ''}.")
        if duplicates:
            print(f"{duplicates:,} records repeated an id already seen in the file "
                  f"and were stored once.")


    def do_export(self, arg):
        """
        Export the retrieved comments from the file on disk to other formats.
        Syntax: export [format]
        Format: html, gephi, csv, txt, all (default is "all")
        """

        format_choice = arg.strip().lower() if arg else "all"

        if format_choice not in ["html", "gephi", "csv", "txt", "all"]:
            print("Invalid export format. Use 'html', 'gephi', 'csv', 'txt', or 'all'.")
            return

        if format_choice == "txt":
            if not os.path.exists(DB_PATH):
                print(f"No '{DB_PATH}'. The txt export reads the database.")
                return
            try:
                print("Exporting as text files ... ", end="", flush=True)
                written = export_txt(COMMENTS_FOLDER)
                print(f"done ({written:,} files in '{COMMENTS_FOLDER}').")
            except Exception as e:
                logger.error(f"Error exporting text files: {e}.")
                print(f"failed: {e}")
            return

        temporary = None
        if os.path.exists(DB_PATH):
            temporary = COMMENTS_JSON + ".export.tmp"
            print("Writing the corpus out for the converters ... ", end="", flush=True)
            try:
                export_json(temporary, jsonl=True)
            except Exception as e:
                logger.error(f"Couldn't write the corpus out: {e}.")
                print(f"failed: {e}")
                return
            print("done.")
            if os.path.exists(COMMENTS_JSON):
                print(f"(Exporting from '{DB_PATH}'. '{COMMENTS_JSON}' is an exchange file, "
                      f"not the corpus, and was not read.)")
        elif os.path.exists(COMMENTS_JSON):
            print(f"No '{DB_PATH}'. Exporting from '{COMMENTS_JSON}' instead.")
            try:
                with open(COMMENTS_JSON, "r", encoding="utf-8") as handle:
                    content = handle.read().strip()
                    if not content:
                        logger.error(f"Error: There are no comments stored in '{COMMENTS_JSON}'.")
                        return
                    json.loads(content)   # test JSON integrity to be on the safe side :)
            except json.JSONDecodeError as e:
                logger.error(f"Error: JSON structure in '{COMMENTS_JSON}' is corrupted: {e}.")
                return
        else:
            print(f"Neither '{DB_PATH}' nor '{COMMENTS_JSON}' exists. Download first.")
            return

        source = temporary or COMMENTS_JSON

        if format_choice in ["html", "all"]:
            try:
                print("Exporting as HTML file ... ", end="")
                convert_json_to_html(source, COMMENTS_HTML)
                print(f"done ('{COMMENTS_HTML}').")
            except Exception as e:
                logger.error(f"Error exporting HTML: {e}.")

        if format_choice in ["csv", "all"]:
            try:
                print("Exporting as CSV file ... ", end="")
                convert_json_to_csv(source, COMMENTS_CSV)
                print(f"done ('{COMMENTS_CSV}').")
            except Exception as e:
                logger.error(f"Error exporting CSV: {e}.")

        if format_choice == "all" and os.path.exists(DB_PATH):
            try:
                print("Exporting as text files ... ", end="", flush=True)
                written = export_txt(COMMENTS_FOLDER)
                print(f"done ({written:,} files in '{COMMENTS_FOLDER}').")
            except Exception as e:
                logger.error(f"Error exporting text files: {e}.")

        if format_choice in ["gephi", "all"]:
            try:
                print("Exporting as Gephi network graph ... ", end="")
                convert_json_to_gephi(source, COMMENTS_GEXF_ON, include_replies=True)
                convert_json_to_gephi(source, COMMENTS_GEXF_OFF, include_replies=False)
                print(f"done ('{COMMENTS_GEXF_ON}' and '{COMMENTS_GEXF_OFF}').")
            except Exception as e:
                logger.error(f"Error exporting Gephi network graph: {e}.")

        platform.remove_files([leftover for leftover
                               in (temporary, (temporary or "") + ".fingerprint")
                               if leftover])


class YTCMFilterCommands:
    def do_exportjson(self, arg):
        """
        Write the database back out as a Comments.json.
        Syntax: exportjson [FILE] [--corpus TAG] [--years FROM-TO] [--jsonl]
        """

        parts = arg.split()
        path = parts[0] if parts and not parts[0].startswith("--") else COMMENTS_JSON
        corpus = None
        years = None
        jsonl = "--jsonl" in parts
        used = {0} if parts and not parts[0].startswith("--") else set()
        for index, (flag, value) in enumerate(zip(parts, parts[1:]), start=0):
            if flag == "--corpus":
                corpus = value
                used |= {index, index + 1}
            elif flag == "--years":
                used |= {index, index + 1}
                try:
                    first, last = value.split("-")
                    years = (int(first), int(last))
                except ValueError:
                    print("Syntax for years: --years 2019-2021")
                    return
        used |= {i for i, word in enumerate(parts) if word == "--jsonl"}
        if reject_unknown_flags(parts, ("--corpus", "--years", "--jsonl"),
                                "Syntax: exportjson [FILE] [--corpus TAG] "
                                "[--years FROM-TO] [--jsonl]"):
            return
        if jsonl and path == COMMENTS_JSON:
            path = os.path.splitext(COMMENTS_JSON)[0] + ".jsonl"
        stray = [word for i, word in enumerate(parts) if i not in used]
        if stray:
            print(f"Don't know what to do with: {' '.join(stray)}.")
            print("A filename with spaces in it will not work here.")
            return

        try:
            refuse_outside(path)
            written = export_json(path, corpus=corpus, years=years, jsonl=jsonl)
        except ident.NoKey as e:
            logger.error(f"exportjson: {e}.")
            print(str(e))
            return
        except OSError as e:
            logger.error(f"Couldn't write '{path}': {e}.")
            print(f"Couldn't write '{path}': {e.strerror or e}.")
            return
        except ValueError as e:
            logger.error(f"exportjson: {e}.")
            print(str(e))
            return
        print(f"Wrote {written:,} videos to '{path}'"
              f"{' as JSONL' if jsonl else ''}.")


    def do_embed(self, arg):
        """
        Give every message an embedding, so 'semsearch' can rank the corpus.
        Syntax: embed [--corpus TAG] [--limit N] [--yes]
        """

        parts = arg.split()
        if reject_unknown_flags(parts, ("--corpus", "--limit", "--yes"),
                                "Syntax: embed [--corpus TAG] [--limit N] [--yes]"):
            return
        corpus = None
        limit = None
        for flag, value in zip(parts, parts[1:]):
            if flag == "--corpus":
                corpus = value
            elif flag == "--limit":
                try:
                    limit = int(value)
                except ValueError:
                    print("--limit needs a number as argument.")
                    return
                if limit < 0:
                    print("--limit can't be negative.")
                    return
        try:
            written = embed_corpus(corpus=corpus, assume_yes="--yes" in parts, limit=limit)
        except (RuntimeError, NotImplementedError, ValueError) as e:
            logger.error(f"embed: {e}.")
            print(e)
            return
        if written:
            print(f"Embedded {written:,} messages.")


    def do_semsearch(self, arg):
        """
        Rank the corpus by how close each message is to a concept.
        Syntax: semsearch "concept" [--top-k N] [--min-score F] [--corpus TAG] [--out FILE] [--jsonl]
        """

        if not arg:
            print('Syntax: semsearch "concept" [--top-k N] [--min-score F] [--corpus TAG] '
                  '[--out FILE] [--jsonl]')
            print("  Ranks every message. Use 'filter' for an exact, reportable match.")
            return

        syntax = ('Syntax: semsearch "concept" [--top-k N] [--min-score F] [--corpus TAG] '
                  '[--out FILE] [--jsonl]')
        parts = arg.split()
        if arg.lstrip().startswith(('"', "'")):
            quote = arg.lstrip()[0]
            rest = arg.lstrip()[1:]
            query, _, remainder = rest.partition(quote)
            parts = remainder.split()
        else:
            query = parts[0]
            parts = parts[1:]
            dropped = list(itertools.takewhile(lambda part: not part.startswith("--"), parts))
            if dropped:
                print(f'Did you mean: semsearch "{query} {" ".join(dropped)}"')
                return

        if query.startswith("--"):
            print("The concept comes first, then the flags. Put a concept of several "
                  "words in quotes.")
            return

        top_k, min_score, corpus, out = None, None, None, None
        jsonl = "--jsonl" in parts
        for flag, value in zip(parts, parts[1:]):
            if flag == "--top-k":
                try:
                    top_k = int(value)
                except ValueError:
                    print(f"--top-k needs a whole number, not '{value}'.")
                    return
                if top_k < 1:
                    print("--top-k needs at least 1.")
                    return
            elif flag == "--min-score":
                try:
                    min_score = float(value)
                except ValueError:
                    print("--min-score needs a number.")
                    return
            elif flag == "--corpus":
                corpus = value
            elif flag == "--out":
                out = value
        unknown = [part for part in parts if part.startswith("--")
                   and part not in ("--top-k", "--min-score", "--corpus", "--out", "--jsonl")]
        if unknown:
            print(f"Unknown: {' '.join(unknown)}.")
            print(syntax)
            return

        path = out or ("semsearch.jsonl" if jsonl else "semsearch.csv")
        try:
            refuse_outside(path)
        except ValueError as e:
            logger.error(f"semsearch: {e}.")
            print(str(e))
            return

        try:
            rows, model, searched = semantic_search(query, top_k=top_k, min_score=min_score,
                                                    corpus=corpus)
        except (RuntimeError, NotImplementedError, ValueError) as e:
            logger.error(f"semsearch: {e}.")
            print(e)
            return

        if not rows:
            if not searched:
                print("No embeddings yet. Run 'embed' first.")
            elif min_score is not None:
                print(f"None of the {searched:,} embedded messages scored above "
                      f"{min_score}.")
            else:
                print(f"Nothing came back from {searched:,} embedded messages.")
            return

        print(f"\n{len(rows):,} of {searched:,} messages, ranked by closeness to "
              f"{query!r}:\n")
        preview(rows)
        write_results(path, rows, query, model, searched,
                      SEMSEARCH_TOP_K if top_k is None else top_k,
                      min_score, jsonl=jsonl)
        print(f"\n  full result set with its manifest: '{path}'")


    def do_filter(self, arg):
        """
        Generate a filtered list of video IDs based on search terms.
        Syntax: filter [--and|--or] [--words] term1 term2 ...
        Matching is by substring unless --words is given for whole-word mode.
        """

        if not arg:
            print("Syntax: filter [--and|--or] [--words] term1 term2 ...")
            return

        mode = "and"
        parts = arg.strip().split()
        if reject_unknown_flags(parts, ("--and", "--or", "--words"),
                                "Syntax: filter [--and|--or] [--words] term1 term2 ..."):
            return

        lowered = [part.lower() for part in parts]
        if "--and" in lowered and "--or" in lowered:
            print("You can't have --and AND --or.")
            print("Syntax: filter [--and|--or] [--words] term1 term2 ...")
            return

        words = any(part.lower() == "--words" for part in parts)
        for flag in ("--and", "--or"):
            if flag in lowered:
                mode = flag[2:]
        terms = [part for part in parts
                 if part.lower() not in ("--words", "--and", "--or")]

        if not terms:
            print("No search terms provided.")
            return

        if words:
            unbroken = repo.terms_without_word_boundaries([term.lower() for term in terms])
            if unbroken:
                print(f"Whole-word matching doesn't apply to {', '.join(unbroken)}. "
                      f"Searched as substrings.")

        if self.filtered_list is None:
            # First filter: matched in the database, so only the matches are built.
            result = repo.filter_as_nested(terms, mode, words=words)
        else:
            # Narrowing an existing result
            result = filter_data(self.filtered_list, terms, mode, words=words)

        if not result:
            print("No matches found.")
        else:
            print(f"{len(result):,} matching {plural(len(result), 'video')} found.")

        self.filtered_list = result
        self.last_query = terms
        self.last_words = words


    def do_unfilter(self, arg):
        """
        Remove the previously applied filter.
        Syntax: unfilter
        """

        self.filtered_list = None
        self.last_query = None
        self.last_words = False


    def do_hits(self, arg):
        """
        Show the hits for the last filter.
        Syntax: hits
        """

        if self.filtered_list is None:
            print("No filter applied.")
        elif self.last_query is None:
            print("No search terms stored for snippet highlighting.")
            hits(self.filtered_list, [])
        else:
            hits(self.filtered_list, self.last_query, self.last_words)


    def do_filtersave(self, arg):
        """
        Save the filtered list to a file.
        Syntax: filtersave [filename or default "Filtered.json"]
        """

        if self.filtered_list is None:
            print("No filter applied. Nothing to save.")
            return

        filename = arg.strip() or FILTER_JSON

        try:
            refuse_outside(filename)
            with open(filename, "w", encoding="utf-8") as handle:
                json.dump(self.filtered_list, handle, indent=2, ensure_ascii=False)
            print(f"Filtered data saved to '{filename}'.")
        except ValueError as e:
            logger.error(f"filtersave: {e}.")
            print(str(e))
        except IOError as e:
            logger.error(f"Error saving file '{filename}': {e}.")
            print("Couldn't save file.")


class YTCMSearchCommands:
    def do_primary(self, arg):
        """
        Change the primary search terms.
        Syntax: primary term1 term2 ...
        term1 are separated by spaces or commas; sublists are separated by semicolons.
        "primary" without arguments shows the current primary search terms.
        "primary --clear" empties them.
        """

        if not arg:
            print(f"Current primary search terms: {self.primary_terms}.")
            return

        if reject_unknown_flags(arg.split(), ("--clear",),
                                "Syntax: primary term1 term2 ... | primary --clear"):
            return

        if arg.strip() == "--clear":
            self.primary_terms = []
            print("Primary search terms cleared.")
            return

        sublists = arg.split(';')
        result = []

        for sublist in sublists:
            if sublist.strip():
                parts = re.split('[, ]+', sublist)
                parts = [part for part in parts if part]
                result.append(parts)

        self.primary_terms = result
        print(f"Primary search terms set to: {self.primary_terms}.")


    def do_secondary(self, arg):
        """
        Change the secondary search terms.
        Syntax: secondary term1 term2 ...
        Terms are separated by spaces or commas.
        "secondary" without arguments shows the current secondary search terms.
        "secondary --clear" empties them.
        """

        if not arg:
            print(f"Current secondary search terms: {self.secondary_terms}.")
            return

        if reject_unknown_flags(arg.split(), ("--clear",),
                                "Syntax: secondary term1 term2 ... | secondary --clear"):
            return

        if arg.strip() == "--clear":
            self.secondary_terms = []
            print("Secondary search terms cleared.")
            return

        result = re.split("[, ]+", arg)

        self.secondary_terms = result
        print(f"Secondary search terms set to: {self.secondary_terms}.")


    def do_exclude(self, arg):
        """
        Change the excluded terms.
        Syntax: exclude term1 term2 ...
        Terms are separated by spaces or commas.
        "exclude" without arguments shows the current excluded terms.
        "exclude --clear" empties them.
        """

        if reject_unknown_flags(arg.split(), ("--clear",),
                                "Syntax: exclude term1 term2 ... | exclude --clear"):
            return

        if arg.strip() == "--clear":
            self.excluded_terms = []
            print("Excluded terms cleared.")
            return

        if not arg:
            print(f"Current excluded search terms: {self.excluded_terms}.")
            return

        result = [part for part in re.split("[, ]+", arg) if part]

        self.excluded_terms = result
        print(f"Excluded search terms set to: {self.excluded_terms}.")


    def do_year(self, arg):
        """
        Change the search year.
        Syntax: year YYYY
        "year" without arguments shows the current search year.
        """

        if not arg:
            print(f"Current search year: {self.search_year}.")
            return

        try:
            year = int(arg.strip())
        except ValueError:
            print("That is not a whole number.")
            return

        current_year = datetime.now().year
        if 2005 <= year <= current_year:
            self.search_year = year
            print(f"Search year set to {self.search_year}.")
        else:
            print(f"Please enter a valid year (range: 2005 until {current_year}).")


    def do_search(self, arg):
        """
        Search for videos and store the resulting IDs in a file.
        Syntax: search [filename]
        """

        if arg:
            self.video_ids_file = arg.strip()

        if not self.primary_terms:
            print("Error: No primary search terms. Use 'primary' to define them.")
            return

        if not self.search_year:
            print("No search year. Use 'year' to set one.")
            return

        if not self.youtube:
            print("No API connection. Use 'connect' first.")
            return

        search_terms = generate_search_list(self.primary_terms, self.secondary_terms)

        print("\nSearching YouTube.com for relevant video IDs ... ", end="")
        self.video_ids = get_video_ids(search_terms, self.youtube, part="snippet",
                                       maxResults=SEARCH_PAGE_SIZE, year=self.search_year,
                                       excluded_terms=self.excluded_terms)
        total_videos = len(self.video_ids)
        print(f"done.\n{total_videos} video IDs found for current search parameters.")

        num = check_pending_downloads(self.video_ids_file)

        if num > 0 and total_videos > 0:
            print(f"There are {num} video IDs pending download in '{self.video_ids_file}'.")
            choice = input(
                "Add the new IDs to the ones on file, or replace them? (a/r) ").strip().lower()
            if not choice or choice.startswith("q"):
                print("Aborted.")
                return
            if choice == "r":
                print("Overwriting existing video IDs on file.")
                mode = "w"
            else:
                print("Adding new video IDs to existing ones.")
                mode = "a"
        else:
            mode = "w"

        if total_videos > 0:  # save search results
            try:
                with open(self.video_ids_file, mode, encoding="utf-8") as id_file:
                    for video_id in self.video_ids:
                        id_file.write(f"{video_id}\n")
                print(f"Saved video IDs to '{self.video_ids_file}'.")

                if mode == "a":
                    with open(self.video_ids_file, "r", encoding="utf-8") as handle:
                        self.video_ids = [line.strip() for line in handle if line.strip()]

            except OSError as e:
                logger.error(f"Error: can't save video IDs to '{self.video_ids_file}': {e}.")
        else:
            print("No videos found.")


    def do_update(self, arg):
        """
        Put every video already in the corpus back on the download list.
        This will re-download all of them with potentially updated content. Beware of your API quota!
        Syntax: update [filename] [--stale-days N]
        """

        parts = arg.split()
        if reject_unknown_flags(parts, ("--stale-days",),
                                "Syntax: update [filename] [--stale-days N]"):
            return
        stale_days = None
        if "--stale-days" in parts:
            position = parts.index("--stale-days")
            try:
                stale_days = int(parts[position + 1])
                if stale_days < 0:
                    raise ValueError
            except (IndexError, ValueError):
                print("--stale-days needs a whole number of days, for example: update --stale-days 30")
                return
            parts = parts[:position] + parts[position + 2:]

        if parts:
            self.video_ids_file = parts[0].strip()

        stale = "" if stale_days is None else \
            " AND (ds.updated_at IS NULL OR ds.updated_at < datetime('now', ?))"
        age = [] if stale_days is None else [f"-{stale_days} days"]
        try:
            connection = get_conn()
            try:
                everything = [row[0] for row in connection.execute(
                    "SELECT video_id FROM video_corpora WHERE corpus = ? ORDER BY position",
                    (DEFAULT_CORPUS,))]
                if everything:
                    video_ids = [row[0] for row in connection.execute(
                        "SELECT vc.video_id FROM video_corpora vc "
                        "LEFT JOIN download_state ds ON ds.video_id = vc.video_id "
                        f"WHERE vc.corpus = ?{stale} ORDER BY vc.position",
                        [DEFAULT_CORPUS] + age)]
                else:
                    everything = [row[0] for row in connection.execute(
                        "SELECT video_id FROM videos ORDER BY position")]
                    video_ids = [row[0] for row in connection.execute(
                        "SELECT v.video_id FROM videos v "
                        "LEFT JOIN download_state ds ON ds.video_id = v.video_id "
                        f"WHERE 1=1{stale} ORDER BY v.position", age)]
                undated = connection.execute(
                    "SELECT COUNT(*) FROM videos v LEFT JOIN download_state ds "
                    "ON ds.video_id = v.video_id WHERE ds.updated_at IS NULL").fetchone()[0]
            finally:
                connection.close()
        except Exception as e:
            logger.error(f"CouldnÄt read the corpus: {e}.")
            return

        total_videos = len(video_ids)
        if len(everything) == 0:
            print(f"No videos in '{DB_PATH}'. Search and download first.")
            return

        if stale_days is not None:
            print(f"{total_videos:,} of {len(everything):,} videos have not been "
                  f"refreshed in {stale_days} {plural(stale_days, 'day')}.")
            if undated:
                print(f"  {undated:,} have no recorded download time and are queued for "
                      f"that. Imported corpora have none.")
            if total_videos == 0:
                print("Nothing to update.")
                return

        num = check_pending_downloads(self.video_ids_file)

        if num > 0:
            print(f"There are {num} video IDs pending download in '{self.video_ids_file}'.")
            choice = input(
                "Add the updated IDs to the list, or replace it? (a/r) ").strip().lower()
            if not choice or choice.startswith("q"):
                print("Aborted.")
                return
            if choice == "r":
                print("Overwriting existing video IDs on file.")
                mode = "w"
            else:
                print("Adding updated video IDs to existing ones.")
                mode = "a"
        else:
            mode = "w"

        connection = get_conn()
        try:
            requeue(connection, video_ids, DEFAULT_CORPUS)
        finally:
            connection.close()
        print(f"{total_videos:,} {plural(total_videos, 'video')} put back on the download "
              f"list in '{DB_PATH}'. 'download' picks them up from there.")

        try:
            with open(self.video_ids_file, mode, encoding="utf-8") as id_file:
                for video_id in video_ids:
                    id_file.write(f"{video_id}\n")
            print(f"Saved {total_videos} video IDs to '{self.video_ids_file}'.")

            if mode == "a":
                with open(self.video_ids_file, "r", encoding="utf-8") as handle:
                    self.video_ids = [line.strip() for line in handle if line.strip()]
            else:
                self.video_ids = video_ids

        except OSError as e:
            logger.error(f"Error: can't save video IDs to '{self.video_ids_file}': {e}.")


    def do_load(self, arg):
        """
        Load video IDs from file to memory.
        Syntax: load [filename]
        """

        filename = arg.strip() if arg else self.video_ids_file

        try:
            with open(filename, "r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
            self.video_ids = [line for line in lines if is_youtube_video_id(line)]
            number_of_ids = len(self.video_ids)
            skipped = len(lines) - number_of_ids
            if number_of_ids == 0:
                print(f"There are no video IDs contained in '{filename}'.")
            else:
                print(f"Loaded {number_of_ids} video IDs from '{filename}'.")
            if skipped:
                print(f"Skipped {skipped} lines that are no YouTube video IDs.")
        except FileNotFoundError:
            logger.error(f"Error: file '{filename}' doesn't exist.")
        except Exception as e:
            logger.error(f"An unexpected error occurred while loading video IDs from '{filename}': {e}.")


    def do_addid(self, arg):
        """
        Manually add a video ID to the list of video IDs to be downloaded.
        Syntax: addid [video_id]
        """
  
        if not arg:
            video_id = input("Enter video ID: ").strip()
        else:
            video_id = arg.strip()

        if is_youtube_video_id(video_id):
            self.video_ids.append(video_id)
            try:
                with open(self.video_ids_file, "a", encoding="utf-8") as id_file:
                    id_file.write(video_id + "\n")
            except Exception as e:
                logger.error(f"Error adding '{video_id}' to '{self.video_ids_file}': {e}.")
                return
            if os.path.exists(DB_PATH):
                connection = get_conn()
                try:
                    mark_pending(connection, [video_id], DEFAULT_CORPUS)
                    known = connection.execute(
                        "SELECT corpus, status FROM download_state WHERE video_id = ?",
                        (video_id,)).fetchone()
                finally:
                    connection.close()
                if known and known[1] == "pending":
                    print(f"'{video_id}' added to the download list of corpus "
                          f"'{known[0]}'.")
                elif known:
                    print(f"'{video_id}' is already in corpus '{known[0]}' with status "
                          f"'{known[1]}'. It was not queued again.")
            else:
                print(f"'{video_id}' added to '{self.video_ids_file}'. There is no "
                      f"corpus yet. Load it before the first download.")
        else:
            print(f"'{video_id}' is no valid YouTube video ID.")


class YTCMTubeConnectCommands:
    def do_tubeconnect(self, arg):
        """
        Call TubeConnect for a series of network intersection analyses.
        Syntax: tubeconnect [overlaps|interaction|graph|plots|null|all] [--activity-null]
        """

        parts = arg.split()
        syntax = "Syntax: tubeconnect [overlaps|interaction|graph|plots|null|all] [--activity-null]"
        if reject_unknown_flags(parts, ("--activity-null",), syntax):
            return

        activity_null = "--activity-null" in parts
        names = [part for part in parts if not part.startswith("--")]
        mode = (names[0] if names else "all").strip().lower()
        if mode not in TUBECONNECT_MODES:
            print(f"Unknown mode '{mode}'. Choose one of: {', '.join(TUBECONNECT_MODES)}.")
            return
        with results.collect("tubeconnect", adopt=results.OUTPUT_ROOT):
            tubeconnect(mode, activity_null=activity_null)


class YTCMTubeGraphCommands:
    def do_tubegraph(self, arg):
        """
        Call TubeGraph for a series of network analyses on the downloaded comments.
        Syntax: tubegraph
        """

        with results.collect("tubegraph"):
            tubegraph()


    def do_tubegraphfull(self, arg):
        """
        Call TubeGraph for the long run, including the parameter sweep.
        Beware, this may take a (very) long time! Syntax: tubegraphfull
        """

        with results.collect("tubegraphfull"):
            tubegraphfull()


    def do_interactiongraph(self, arg):
        """
        Show an undirected interaction graph between uploader/commenter/replier.
        Syntax: interactiongraph
        """


        try:
            settings = ask_graph_settings()
            graph = build_interaction_graph(**settings)

            top_n_in = input("Subgraph of top-N nodes by degree (empty = all)? ").strip()
            top_n = int(top_n_in) if top_n_in else None
            layout = (input("Layout (spring/kamada_kawai/circular/random) [spring]: ") or "spring").strip()

            plot_network_graph(graph, top_n=top_n, layout=layout)
        except Exception as e:
            logger.error(f"Error while creating the interaction graph: {e}.")
            print("Error while creating the interaction graph.")


    def do_degreedist(self, arg):
        """
        Show the degree distribution for the undirected interaction graph.
        Syntax: degreedist
        """

        try:
            graph = build_interaction_graph(**ask_graph_settings())
            cap = int(input("Cap bars at which frequency [100]: ") or 100)
            bins = int(input("Number of bins [30]: ") or 30)
            x_min_in = input("Min degree [none]: ").strip()
            x_max_in = input("Max degree [none]: ").strip()
            x_min = int(x_min_in) if x_min_in else None
            x_max = int(x_max_in) if x_max_in else None

            plot_channel_degree_distribution(graph, cap_height=cap, bin_count=bins, x_min=x_min, x_max=x_max)

        except Exception as e:
            logger.error(f"Error while creating degree distribution: {e}.")
            print("Error while creating the degree distribution.")


    def do_centralities(self, arg):
        """
        Show the centrality measures for the undirected interaction graph.
        Syntax: centralities
        """

        try:
            graph = build_interaction_graph(**ask_graph_settings())
            slow = not (input("Compute the slow measures too "
                              "(betweenness, eigenvector) (y/n)? [y]: ")
                        or "y").strip().lower().startswith("n")
            centrality_df = compute_centrality_measures(graph, skip_slow=not slow, speed_up=True)
            top = int(input("Top-N for bar chart [10]: ") or 10)
            both_ans = (input("Also show scatter overview (y/n)? [y]: ") or "y").strip().lower()
            both = False if both_ans.startswith("n") else True
            plot_centrality_results(centrality_df, top_n=top, both=both)
        except Exception as e:
            logger.error(f"Error while computing/plotting centrality: {e}.")
            print("Error while computing/plotting centrality.")


    def do_structure(self, arg):
        """
        Describe the shape of the interaction graph and of the reply network.
        Syntax: structure
        """

        try:
            settings = ask_graph_settings()
            graph = build_interaction_graph(**settings)
            slow = (input("Also compute the diameter and the average shortest path? "
                          "They cost one search per node (y/n)? [n]: ")
                    or "n").strip().lower().startswith("y")
            budget = 30.0
            if slow:
                try:
                    budget = float(input("Give up if the projection exceeds how many "
                                         "seconds? [30]: ") or 30)
                except ValueError:
                    logger.info("Couldn't read a number from the input, using the default.")
            graph_structure(graph, expensive=slow, budget_seconds=budget)

            if (input("\nAlso describe the directed reply network (y/n)? [y]: ")
                    or "y").strip().lower().startswith("y"):
                include_self = (input("Include self-replies (y/n)? [n]: ")
                                or "n").strip().lower().startswith("y")
                try:
                    min_weight = int(input("Drop reply edges with weight below [1]: ") or 1)
                except ValueError:
                    logger.info("Couldn't read a number from the input, using the default.")
                    min_weight = 1
                print()
                directed_structure(build_reply_network(include_self=include_self,
                                                       min_weight=min_weight))
        except Exception as e:
            logger.error(f"Error while describing the graph: {e}.")
            print("Error while describing the graph.")


    def do_communities(self, arg):
        """
        Find communities in the interaction graph and say what chance would give.
        Syntax: communities
        """

        try:
            settings = ask_graph_settings()
            method = (input(f"Detection method ({'/'.join(COMMUNITY_METHODS)}) [louvain]: ")
                      or "louvain").strip().lower()
            if method not in COMMUNITY_METHODS:
                print(f"Unknown method '{method}'. Choose one of: "
                      f"{', '.join(COMMUNITY_METHODS)}.")
                return
            try:
                draws = int(input("Draws from the null model [20]: ") or 20)
            except ValueError:
                logger.info("Couldn't read a number from the input, using the default.")
                draws = 20
            try:
                budget = float(input("Give up on the null above how many minutes? [10]: ") or 10)
            except ValueError:
                logger.info("Couldn't read a number from the input, using the default.")
                budget = 10.0

            table, membership = community_structure(method=method, draws=draws,
                                                    budget_seconds=budget * 60, **settings)
            if not len(membership):
                return
            path = input("Write the channel-to-community table where [e.g. 'communities.csv'; leave empty to skip]? ").strip()
            if path:
                membership.to_csv(path, index=False)
                print(f"{len(membership):,} channels written to '{path}'.")
        except Exception as e:
            logger.error(f"Error while finding communities: {e}.")
            print("Error while finding communities.")


    def do_replygraph(self, arg):
        """
        Show a directed reply graph between channels.
        Syntax: replygraph
        """

        try:
            include_self_ans = (input("Include self-replies (y/n)? [n]: ") or "n").strip().lower()
            include_self = True if include_self_ans.startswith("y") else False
            min_weight = int(input("Drop edges with weight below [1]: ") or 1)
            graph = build_reply_network(include_self=include_self, min_weight=min_weight)
            top_n_in = input("Subgraph of top-N nodes by degree (empty = all)? ").strip()
            top_n = int(top_n_in) if top_n_in else None
            plot_reply_network(graph, top_n=top_n)
        except Exception as e:
            logger.error(f"Error while creating the reply graph: {e}.")
            print("Error while creating the reply graph.")


    def do_cooccurrence(self, arg):
        """
        Show a channel co-occurrence heatmap/clustermap.
        Syntax: cooccurrence
        """

        try:
            role_str = input("Include which roles (comma-separated: uploader,commenter,replier)? [all] ").strip()
            roles = tuple([row.strip() for row in role_str.split(",") if row.strip()]) if role_str else ("uploader", "commenter", "replier")

            cap_in = input("Build the matrix over the top N channels (empty = all, slower): ").strip()
            matrix_channels = int(cap_in) if cap_in else None
            maxch = int(input("Max channels to plot [500]: ") or 500)
            both_ans = (input("Also show clustermap (y/n)? [y]: ") or "y").strip().lower()
            both = False if both_ans.startswith("n") else True

            matrix = channel_video_participation_matrix(roles=roles, dtype=bool,
                                                        top_channels=matrix_channels)
            print(f"  matrix: {matrix.shape[0]:,} channels x {matrix.shape[1]:,} videos"
                  f"{'' if matrix_channels else ' (every channel; the first question caps it)'}"
                  f", plotting at most {maxch:,}")

            plot_channel_clustering_heatmap(matrix, max_channels=maxch, both=both)
        except Exception as e:
            logger.error(f"Error while creating the channel heatmap: {e}.")
            print("Error while creating the channel heatmap.")


    def do_channelstats(self, arg):
        """
        Show occurrence stats per channel.
        Syntax: channelstats
        """

        try:
            df = channel_occurrence_stats()
            if df is None or df.empty:
                print("No channel stats could be computed (empty dataset).")
                return

            role_in = (input("Role (uploader/commenter/replier/total) [total]: ") or "total").strip().lower()
            if role_in not in {"uploader", "commenter", "replier", "total"}:
                print("Unknown role, defaulting to 'total'.")
                role_in = "total"

            try:
                top = int((input("Top-N channels [20]: ") or "20").strip())
            except ValueError:
                top = 20
            top = max(1, min(top, len(df)))

            print(f"Plotting role: {role_in} | Top-N: {top}")

            plot_top_channels(df, role=role_in, top_n=top)

        except Exception as e:
            logger.error(f"Error while computing/plotting channel stats: {e}.")
            print("Error while computing/plotting channel stats.")

    def do_pairgraph(self, arg):
        """
        Show frequent channel pairs.
        Syntax: pairgraph
        """

        try:
            threshold = int(input("Minimum co-occurrence threshold [3]: ") or 3)
            exclude_ans = (input("Exclude uploader channel from pairs (y/n)? [n]: ") or "n").strip().lower()
            exclude_uploader = True if exclude_ans.startswith("y") else False
            roles_str = input("Restrict roles (comma-separated; uploader,commenter,replier) [all]: ").strip()
            roles = tuple([row.strip() for row in roles_str.split(",") if row.strip()]) if roles_str else ("uploader", "commenter", "replier")
            top_in = input("Only draw top-N pairs (empty = all): ").strip()
            top_n = int(top_in) if top_in else None
            minimum = int(input("Ignore channels seen on fewer than how many videos? [2]: ") or 2)

            pairs_df = frequent_channel_pairs(threshold=threshold, roles=roles, exclude_uploader=exclude_uploader,
                                              top_n=top_n, min_videos_per_channel=minimum)
            print(f"    {len(pairs_df):,} pairs"
                  f"{f', drawing the top {top_n:,}' if top_n else ', drawing all of them'}")
            plot_channel_pair_network(pairs_df, top_n=top_n)
        except Exception as e:
            logger.error(f"Error while computing/plotting channel pairs: {e}.")
            print("Error while computing/plotting channel pairs.")


    def do_roleproportions(self, arg):
        """
        Show how each channel's activity divides between uploading, commenting and replying.
        Syntax: roleproportions
        """

        try:
            top = int(input("How many channels to show [30]: ") or 30)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            top = 30
        share = (input("Show shares instead of counts (y/n)? [n]: ")
                 or "n").strip().lower().startswith("y")
        try:
            df = channel_occurrence_stats()
            plot_channel_role_proportions(df, top_n=top, normalize=share)
        except Exception as e:
            logger.error(f"Error while plotting role proportions: {e}.")
            print("Error while plotting role proportions.")


    def do_topdegree(self, arg):
        """
        Show the channels with highest degree in the undirected interaction graph.
        Syntax: topdegree
        """

        try:
            graph = build_interaction_graph(**ask_graph_settings())
            top = int(input("How many top-degree channels to list [10]: ") or 10)
            df = top_connected_channels(graph, top_n=top)
            if df.empty:
                print("No channels in the graph.")
            else:
                print(df.to_string(index=False))
        except Exception as e:
            logger.error(f"Error while listing top degree channels: {e}.")
            print("Error while listing top degree channels.")


class YTCMTubeScopeCommands:
    def do_tubescope(self, arg):
        """
        Call TubeScope for a series of statistical analyses on the downloaded comments.
        Syntax: tubescope
        """

        with results.collect("tubescope"):
            tubescope()


    def do_likesgraph(self, arg):
        """
        Plot a histogram of likes distribution in the comments.
        Syntax: likesgraph
        """

        df, _ = get_df_comments()
        if df is None or df.empty:
            print("No comments to analyze.")
            return

        try:
            cap = int(input("Cap bars at which number [100]: ") or 100)
            bins = int(input("Create how many bins [30]: ") or 30)
            x_min = input("Start at which number of likes [none]: ").strip()
            x_max = input("Stop at which number of likes [none]: ").strip()
            x_min = int(x_min) if x_min else None
            x_max = int(x_max) if x_max else None
            if x_min and x_max and x_max <= x_min:
                print("Max number must be larger than min number. Ignoring input.")
                x_min, x_max = None, None
        except Exception as e:
            logger.warning(f"Error at data entry: {e}.")
            print("Data validation error.")
            return

        try:
            plot_comment_likes_distribution(df, cap_height=cap, bin_count=bins, x_min=x_min, x_max=x_max)
        except Exception as e:
            logger.error(f"Error while creating likes distribution plot: {e}.")
            print("Error while creating the likes distribution plot.")


    def do_moodline(self, arg):
        """
        Plot a graph of sentiment scores over time.
        Syntax: moodline
        """

        df, _ = get_df_comments()
        if df is None or df.empty:
            print("No comments to analyze.")
            return

        try:
            sentiment = analyze_sentiment_over_time(df)
            plot_sentiment_over_time(sentiment)
        except Exception as e:
            logger.error(f"Error while creating sentiment over time plot: {e}.")
            print("Error creating the sentiment over time plot.")


    def do_activityline(self, arg):
        """
        Plot a graph of comment numbers over time.
        Syntax: activityline
        """

        df, _ = get_df_comments()
        if df is None or df.empty:
            print("No comments to analyze.")
            return

        try:
            timeline = group_comments_by_date(df)
            plot_comments_over_time(timeline)
        except Exception as e:
            logger.error(f"Error while creating comment number over time plot: {e}.")
            print("Error creating the comment number over time plot.")


    def do_topcomments(self, arg):
        """
        Show a list of the top n most-liked comments.
        Syntax: topcomments
        """

        df, _ = get_df_comments()
        if df is None or df.empty:
            print("No comments to analyze.")
            return

        try:
            how_many = int(input("Number of comments to show [5]: ") or 5)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            how_many = 5

        if how_many <= 0:
            print("That is not a positive number. Using 5.")
            how_many = 5

        try:
            top_comments = get_most_liked_comments(df, top_n=how_many)
            print(f"Top {how_many} most-liked comments:")
            for i, row in top_comments.iterrows():
                text = row["text"][:100] + ("…" if len(row["text"]) > 100 else "")
                print(f"- [{row['likes']} Likes] {text}")
        except Exception as e:
            logger.error(f"Error while creating the top {how_many} comments list: {e}.")
            print(f"Error while creating the top {how_many} comments list.")


    def do_sentimentmean(self, arg):
        """
        Show average sentiment over all comments.
        Syntax: sentimentmean
        """

        df, _ = get_df_comments()
        if df is None or df.empty:
            print("No comments to analyze.")
            return
        try:
            score, scored, total = calculate_average_sentiment(df)
            if score is None:
                print(f"No {mode_label().lower()} carry a sentiment score. VADER scores "
                      f"English only; see 'langdist'.")
                return
            print(f"Average sentiment score (LLM, VADER as fallback) over all "
                  f"{mode_label().lower()}: {score:.3f}")
            print(f"  computed on {scored:,} of {total:,} records "
                  f"({scored / total * 100:.1f}%); the rest have no score.")
        except Exception as e:
            logger.error(f"Error while calculating the average comments sentiment: {e}.")
            print("Error while creating average comments sentiment.")


    def do_replyquote(self, arg):
        """
        Calculate the quote of comments with replies.
        Syntax: replyquote
        """

        df, _ = get_df_comments()
        if df is None or df.empty:
            print("No comments to analyze.")
            return

        try:
            ratio = analyze_replies(df)
            if ratio is None:
                modus = resolve_mode()
                fehlt = "replies" if modus == "c" else "top-level comments"
                print(f"Not measurable: TUBESCOPE_MODE is {modus!r}. The frame holds no "
                      f"{fehlt}. Both are needed to continue.")
            else:
                print(f"Quote of comments with replies: {ratio:.2f} %")
        except Exception as e:
            logger.error(f"Error while calculating the reply quote: {e}.")
            print("Error while calculating the reply quote.")


    def do_interactiondensity(self, arg):
        """
        Calculate an experimental score to measure and plot the interaction density distribution.
        Syntax: interactiondensity
        """

        try:
            bins = int(input("How many bins [30]: ") or 30)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            bins = 30

        if bins <= 0:
            print("That is not a positive number. Using 30.")
            bins = 30

        try:
            plot_interaction_density_distribution(bin_count=bins)
        except Exception as e:
            logger.error(f"Error while creating the interaction density plot: {e}.")
            print("Error while creating the interaction density plot.")


    def do_sentimentdist(self, arg):
        """
        Plot a graph of sentiment distribution in comments data.
        Syntax: sentimentdist
        """

        df, _ = get_df_comments()
        if df is None or df.empty:
            print("No comments to analyze.")
            return

        try:
            plot_sentiment_distribution(df)
        except Exception as e:
            logger.error(f"Error while creating the sentiment distribution graph: {e}.")
            print("Error while creating the sentiment distribution graph.")


    def do_channelline(self, arg):
        """
        Show a timeline of channels participating in the discussion
        Syntax: channelline
        """

        plot_participation_timeline()


    def do_weekdays(self, arg):
        """
        Show a plot of publication numbers per weekday.
        Syntax: weekdays
        """

        share = (input("Show shares instead of counts (y/n)? [n]: ")
                 or "n").strip().lower().startswith("y")
        plot_interactions_by_weekday(normalize=share)


    def do_viewscorrelation(self, arg):
        """
        Plot how views and comments correlate.
        Syntax: viewscorrelation
        """

        replies = (input("Count replies as well as comments (y/n)? [y]: ")
                   or "y").strip().lower().startswith("y")
        logs = not (input("Logarithmic axes (y/n)? [y]: ")
                    or "y").strip().lower().startswith("n")
        plot_views_vs_comments(include_replies=replies, logx=logs, logy=logs)


class YTCMTubeTalkCommands:
    def do_tubetalk(self, arg):
        """
        Call TubeTalk for a series of NLP-related analyses on the downloaded comments.
        Syntax: tubetalk
        """
  
        with results.collect("tubetalk"):
            tubetalk()


    def do_langdist(self, arg):
        """
        Show the language distribution of videos, comments, or replies.
        Syntax: langdist
        """


        level = (input("Level (video/comment/reply) [comment]: ") or "comment").strip().lower()
        if level not in ("video", "comment", "reply"):
            print("Invalid level. Use: video, comment, or reply.")
            return

        try:
            top_n = int(input("Top-N [12]: ") or 12)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            top_n = 12

        norm_ans = (input("Normalize (y/n) [y]: ") or "y").strip().lower()
        normalize = False if norm_ans.startswith("n") else True

        try:
            plot_language_distribution(data=None, level=level, top_n=top_n, normalize=normalize)
        except Exception as e:
            logger.error(f"Error in langdist: {e}.")
            print(f"Error in langdist: {e}")

    def do_langconf(self, arg):
        """
        Show language conflicts (e.g. comment vs. video language).
        Syntax: langconf
        """

        try:
            top_n = int(input("Top-N [12]: ") or 12)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            top_n = 12

        norm_ans = (input("Normalize (y/n) [y]: ") or "y").strip().lower()
        normalize = False if norm_ans.startswith("n") else True

        infer_ans = (input("Infer video language if missing? (y/n) [y]: ") or "y").strip().lower()
        infer_video_lang = False if infer_ans.startswith("n") else True

        try:
            min_support = float(input("Min support for inference [5]: ") or 5)
        except ValueError:
            logger.info("Couldn't read a number from the input; using the default indtead.")
            min_support = 5

        try:
            majority_threshold = float(input("Majority threshold [0.5]: ") or 0.5)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            majority_threshold = 0.5

        try:
            plot_language_conflicts(data=None, top_n=top_n, normalize=normalize, infer_video_lang=infer_video_lang,
                                    min_support=min_support, majority_threshold=majority_threshold)
        except Exception as e:
            logger.error(f"Error in langconf: {e}.")
            print(f"Error in langconf: {e}")


    def do_wordcloud(self, arg):
        """
        Show a wordcloud visualization.
        Syntax: wordcloud
        """

        start_date = input("Start date (YYYY-MM-DD) [none=beginning]: ").strip() or None
        end_date   = input("End date (YYYY-MM-DD) [none=end]: ").strip() or None

        try:
            n_min = int(input("Min n-gram [1]: ") or 1)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            n_min = 1
        try:
            n_max = int(input("Max n-gram [1]: ") or 1)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default instead.")
            n_max = 1

        extra_sw = input("Extra stopwords (comma-separated) [none]: ").strip() or None
        lang_filter = input("Language filter (ISO code) [none]: ").strip() or None

        min_df_raw = input("Min df (abs int or 0..1 float) [2]: ").strip()
        if not min_df_raw:
            min_df = 2
        else:
            try:
                if "." in min_df_raw:
                    min_df = float(min_df_raw)
                else:
                    min_df = int(min_df_raw)
            except ValueError:
                logger.info("Couldn't read a number from the input, using the default.")
                min_df = 2

        max_df_raw = input("Max df (0..1 float or int) [0.95]: ").strip()
        if not max_df_raw:
            max_df = 0.95
        else:
            try:
                if "." in max_df_raw:
                    max_df = float(max_df_raw)
                else:
                    max_df = int(max_df_raw)
            except ValueError:
                logger.info("Couldn't read a number from the input, using the default instead.")
                max_df = 0.95

        try:
            max_features = int(input("Max features [5000]: ") or 5000)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            max_features = 5000

        try:
            run_wordcloud(data=None, start_date=start_date, end_date=end_date, ngram_range=(n_min, n_max),
                          extra_stopwords=[word.strip() for word in extra_sw.split(",")] if extra_sw else None,
                          lang_filter=lang_filter, min_df=min_df, max_df=max_df, max_features=max_features)
        except Exception as e:
            logger.error(f"Error in wordcloud: {e}.")
            print(f"Error in wordcloud: {e}")


    def do_topics(self, arg):
        """
        Perform a LDA topic modeling analysis.
        Syntax: topics
        """

        try:
            n_topics = int(input("Number of topics [6]: ") or 6)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            n_topics = 6
        try:
            n_words = int(input("Top-N words per topic [10]: ") or 10)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            n_words = 10

        min_df_raw = input("Min df (abs int or 0..1 float) [5]: ").strip()
        if not min_df_raw:
            min_df = 5
        else:
            try:
                min_df = float(min_df_raw) if "." in min_df_raw else int(min_df_raw)
            except ValueError:
                logger.info("Couldn't read a number from the input, using the default.")
                min_df = 5

        max_df_raw = input("Max df (0..1 float or int) [0.6]: ").strip()
        if not max_df_raw:
            max_df = 0.6
        else:
            try:
                max_df = float(max_df_raw) if "." in max_df_raw else int(max_df_raw)
            except ValueError:
                logger.info("Couldn't read a number from the input, using the default.")
                max_df = 0.6

        try:
            n_min = int(input("Min n-gram [1]: ") or 1)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            n_min = 1
        try:
            n_max = int(input("Max n-gram [2]: ") or 2)
        except ValueError:
            logger.info("Couldn't read a number from the input, using the default.")
            n_max = 2

        try:
            run_topics(data=None, n_topics=n_topics, n_words=n_words, min_df=min_df, max_df=max_df,
                       ngram_range=(n_min, n_max))
        except Exception as e:
            logger.error(f"Error in topics: {e}.")
            print(f"Error in topics: {e}")
