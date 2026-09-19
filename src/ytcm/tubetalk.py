""" TubeTalk contains a set of language-related analysis functions for the YouTube Comment Miner.
Naturally, most of this code is heavily inspired by existing solutions. I took some ideas about what to integrate
from these books:

Chris Biemann, Gerhard Heyer, and Uwe Quasthoff: "Wissensrohstoff Text: Eine Einführung in das Text Mining",
2nd ed., Wiesbaden: Springer, 2022, and

Melanie Andresen: "Computerlinguistische Methoden für die Digital Humanities: Eine Einführung für die
Geisteswissenschaften", Tübingen: Narr Francke Attempto, 2024.

My code uses the LDA implementation from sklearn (which is accessible to use, but possibly not very efficient),

and I found a primer on its usage in

Dipanjan Sarkar, "Text Analytics with Python: A Practitioner's Guide to Natural Language Processing",
2nd ed., s.l.: Apress, 2019.
"""

import logging
import re
import matplotlib.pyplot as plt
import numpy             as np
import pandas            as pd

from collections                     import Counter, defaultdict
from sklearn.decomposition           import LatentDirichletAllocation           # scikit-learn
from sklearn.feature_extraction.text import CountVectorizer
from urllib.parse                    import urlparse
from wordcloud                       import WordCloud, STOPWORDS

from ytcm.config import MARKERS_TXT, PRIMARY_SEARCH_TERMS, SECONDARY_SEARCH_TERMS
from ytcm.font_utils import configure_cjk_fonts, get_wordcloud_font_path
from ytcm import repo as repo

from ytcm import llm_fields
from ytcm.results import write_table

logger = logging.getLogger(__name__)

configure_cjk_fonts()




def build_comments_df(data=None):

    if data is None:
        frame = repo.records_df(mode="cr", fields=("llm_language",))
        unresolved = {None, "", "unknown", "N/A", "n/a"}
        frame["language"] = [
            llm if llm not in unresolved else (plain if plain not in unresolved else None)
            for llm, plain in zip(frame["llm_language"], frame["language"])]
        frame = frame[["video_id", "date", "text", "language", "kind"]].copy()
        frame["text"] = frame["text"].fillna("")
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce", format="ISO8601", utc=True)
        try:
            frame["date"] = frame["date"].dt.tz_localize(None)
        except (TypeError, AttributeError):
            pass
        return frame

    rows = []

    for video, content in (data or {}).items():
        comments = content.get("comments", []) or []
        for comment in comments:
            rows.append({
                "video_id": video,
                "date"    : pd.to_datetime(comment.get("date"), errors="coerce", format="ISO8601", utc=True),
                "text"    : comment.get("text", ""),
                "language": llm_fields.language_of(comment),
                "kind"    : "comment"
            })

            for reply in comment.get("replies", []) or []:
                rows.append({
                    "video_id": video,
                    "date"    : pd.to_datetime(reply.get("date"), errors="coerce", format="ISO8601", utc=True),
                    "text"    : reply.get("text", ""),
                    "language": llm_fields.language_of(reply),
                    "kind"    : "reply"
                })

    df = pd.DataFrame(rows)

    if not df.empty and "date" in df:
        try:
            df["date"] = df["date"].dt.tz_localize(None)            # remove timezone information
        except (TypeError, AttributeError):
            pass

    return df


def language_view(corpus=None, years=None):

    videos = repo.videos_df(corpus=corpus, years=years,
                            fields=["llm_title_language", "title_language"])
    records = repo.records_df(mode="cr", corpus=corpus, years=years,
                              fields=["llm_language"])

    view = {}
    for row in videos.itertuples(index=False):
        view[row.video_id] = {
            "video_info": {"llm_title_language": getattr(row, "llm_title_language", None),
                           "title_language": getattr(row, "title_language", None)},
            "comments": []}

    comments = {}
    for row in records.itertuples(index=False):
        entry = view.get(row.video_id)
        if entry is None:
            continue
        record = {"llm_language": getattr(row, "llm_language", None),
                  "language": row.language}
        if row.kind == "comment":
            record["replies"] = []
            comments[row.message_id] = record
            entry["comments"].append(record)
        else:
            parent = comments.get(row.parent_id)
            if parent is not None:
                parent["replies"].append(record)
    return view


def infer_video_language(info, min_support=5, majority_threshold=0.4):
    """Guess a video's language from its title, else from its comments. Inferred results are speculative!
    Prefers the LLM verdict (llm_title_language, llm_language); langdetect stays as the fallback.
    Returns a language code or None if confidence is too low.
    """

    stated = llm_fields.llm_video_language_of(info) or info.get("language")
    if stated:
        return stated

    comments = info.get("comments", []) or []
    languages = [llm_fields.language_of(comment) for comment in comments]
    counts = Counter(language for language in languages if language and language != "unknown")

    if not counts:
        return None

    lang, count = counts.most_common(1)[0]
    total_labeled = sum(counts.values())

    if count >= min_support and count / max(1, total_labeled) >= majority_threshold:
        return lang

    return None


def resolve_video_language(info, min_support=5, majority_threshold=0.4):
    """
    Best available language for a video, in order of trustworthiness:
    the LLM verdict on the title, then inference from the (LLM-labeled) comments,
    then langdetect's guess at the title as a last resort.
    """

    return (llm_fields.llm_video_language_of(info)
            or infer_video_language(info, min_support=min_support, majority_threshold=majority_threshold)
            or llm_fields.video_language_of(info))


def count_comments():
    """
    Provides basic counts and distributions for videos, comments, and replies.
    """

    stats = repo.video_stats_df()
    total_videos = len(stats)
    comments_per_video = [int(n) for n in stats["n_comments"]]
    total_comments = sum(comments_per_video)
    total_replies = int(stats["n_replies"].sum())
    videos_with_comments = int((stats["n_comments"] > 0).sum())
    videos_without_comments = total_videos - videos_with_comments
    replies_per_comment = repo.replies_per_comment()

    result = {
        "total_videos"           : total_videos,
        "total_comments"         : total_comments,
        "total_replies"          : total_replies,
        "videos_with_comments"   : videos_with_comments,
        "videos_without_comments": videos_without_comments
    }

    if comments_per_video:
        comment_series = pd.Series(comments_per_video)
        result.update({
            "comments_per_video_mean"  : float(comment_series.mean()),
            "comments_per_video_median": float(comment_series.median()),
            "comments_per_video_std"   : float(comment_series.std(ddof=1)) if len(comment_series) > 1 else 0.0,
            "comments_per_video_min"   : int(comment_series.min()),
            "comments_per_video_max"   : int(comment_series.max())
        })

    if replies_per_comment:
        reply_series = pd.Series(replies_per_comment)
        result.update({
            "replies_per_comment_mean"  : float(reply_series.mean()),
            "replies_per_comment_median": float(reply_series.median())
        })

    return result


def count_languages():
    """
    Calculate language distributions for videos, comments, and replies.
    """


    video_counts = Counter()
    videos = repo.videos_df(fields=("llm_title_language", "title_language"))
    for row in videos.itertuples():
        language = getattr(row, "llm_title_language", None) or getattr(row, "title_language", None)
        if language and language != "unknown":
            video_counts[language] += 1

    comment_counts = repo.effective_language_counts(mode="c")
    reply_counts = repo.effective_language_counts(mode="r")

    return {
        "video"  : video_counts,
        "comment": comment_counts,
        "reply"  : reply_counts,
    }


def count_language_mismatches(data, infer_video_lang=True, min_support=5, majority_threshold=0.4):
    """
    Return full mismatch tables (no top-N cut). A language mismatch occurs when the language of a comment or reply
    differs from its parent language.
    """

    comment_vs_video_counts = defaultdict(int)
    reply_vs_comment_counts = defaultdict(int)
    total_comment_vs_video = 0
    total_reply_vs_comment = 0

    if data is None:
        data = language_view()

    for _, info in data.items():
        video_language = llm_fields.llm_video_language_of(info) or info.get("language")
        if not video_language and infer_video_lang:
            video_language = resolve_video_language(info, min_support=min_support, majority_threshold=majority_threshold)

        comments = info.get("comments", []) or []

        for comment in comments:                  # get mismatch number
            comment_language = llm_fields.language_of(comment)

            if comment_language and video_language and comment_language != video_language:
                comment_vs_video_counts[f"{video_language} > {comment_language}"] += 1
                total_comment_vs_video += 1

            reply_languages = [
                language for language in
                (llm_fields.language_of(reply) for reply in comment.get("replies", []) or [])
                if language
            ]

            for reply_language in reply_languages:
                if comment_language and reply_language != comment_language:
                    reply_vs_comment_counts[f"{comment_language} > {reply_language}"] += 1
                    total_reply_vs_comment += 1

    df_comment_vs_video = (
        pd.DataFrame(sorted(comment_vs_video_counts.items(), key=lambda pair: (-pair[1], pair[0])),
                     columns=["pair", "count"])
        if comment_vs_video_counts else pd.DataFrame(columns=["pair", "count"])
    )

    df_reply_vs_comment = (
        pd.DataFrame(sorted(reply_vs_comment_counts.items(), key=lambda pair: (-pair[1], pair[0])),
                     columns=["pair", "count"])
        if reply_vs_comment_counts else pd.DataFrame(columns=["pair", "count"])
    )

    return df_comment_vs_video, df_reply_vs_comment, total_comment_vs_video, total_reply_vs_comment


def plot_language_distribution(data, level="comment", top_n=None, normalize=False):
    """
    Find out language distribution on level "video", "comment", or "reply".
    Normalize to add values to 1.
    """

    count = Counter()

    if data is None:
        data = language_view()

    for _, info in data.items():
        if level == "video":
            language = resolve_video_language(info)
            if language:
                count[language] += 1

        else:
            for comment in info.get("comments", []) or []:
                if level == "comment":
                    language = llm_fields.language_of(comment)
                    if language:
                        count[language] += 1

                elif level == "reply":
                    for reply in comment.get("replies", []) or []:
                        language = llm_fields.language_of(reply)
                        if language:
                            count[language] += 1

    items = count.most_common(top_n) if top_n else count.items()

    df = pd.DataFrame(items, columns=["language", "count"]).sort_values("count", ascending=False)

    if normalize and not df.empty:
        total = df["count"].sum()
        df["count"] = df["count"] / total

    fig, ax = plt.subplots(figsize=(10, 5))

    if not df.empty:
        ax.bar(df["language"], df["count"], color="steelblue")
        ax.set_title(f"Language Distribution ({level.title()})" + (" - normalized" if normalize else ""))
        ax.set_xlabel("Language")
        ax.set_ylabel("Share" if normalize else "Count")
        plt.xticks(rotation=45, ha="right")
    else:
        ax.text(0.5, 0.5, "No data", ha="center", va="center")
        ax.axis("off")

    plt.tight_layout()
    plt.show()


def plot_language_conflicts(data, top_n=20, normalize=True, infer_video_lang=True, min_support=5, majority_threshold=0.4):
    """
    Show graphs of language conflicts (bars + heatmap)
    """

    df_comment_vs_video_all, df_reply_vs_comment_all, total_comment_vs_video, total_reply_vs_comment = count_language_mismatches(
        data, infer_video_lang=infer_video_lang, min_support=min_support, majority_threshold=majority_threshold
    )

    def top_n_ranking(df, total):
        if df.empty:
            return pd.DataFrame(columns=["pair", "count", "percent"])

        top = df.sort_values("count", ascending=False).head(top_n).copy()
        top["percent"] = (top["count"] / total * 100.0) if total else 0.0

        return top

    df_comment_vs_video = top_n_ranking(df_comment_vs_video_all, total_comment_vs_video)
    df_reply_vs_comment = top_n_ranking(df_reply_vs_comment_all, total_reply_vs_comment)

    bar_fig, axes = plt.subplots(1, 2, figsize=(16,6))      # Plot bar charts, two panels

    ax = axes[0]                                                        # Left: comment versus video
    if not df_comment_vs_video.empty:
        values = df_comment_vs_video["percent"] if normalize else df_comment_vs_video["count"]
        ax.barh(df_comment_vs_video["pair"], values, color="tomato")
        ax.invert_yaxis()
        ax.set_title("Language Mismatch: Comment vs Video")
        ax.set_xlabel("Percent" if normalize else "Count")
        for bar_index, bar_value in enumerate(values):
            ax.text(
                bar_value, bar_index,
                f"{bar_value:.1f}%" if normalize else str(int(bar_value)),
                va="center", ha="left", fontsize=8
            )

    else:
        ax.text(0.5, 0.5, "No language mismatches found", ha="center", va="center")
        ax.axis("off")

    ax = axes[1]                                                        # Right: reply versus comment
    if not df_reply_vs_comment.empty:
        values = df_reply_vs_comment["percent"] if normalize else df_reply_vs_comment["count"]
        ax.barh(df_reply_vs_comment["pair"], values, color="steelblue")
        ax.invert_yaxis()
        ax.set_title("Language Mismatch: Reply vs. Comment")
        ax.set_xlabel("Percent" if normalize else "Count")
        for bar_index, bar_value in enumerate(values):
            ax.text(
                bar_value, bar_index,
                f"{bar_value:.1f}%" if normalize else str(int(bar_value)),
                va="center", ha="left", fontsize=8
            )
    else:
        ax.text(0.5, 0.5, "No mismatches found", ha="center", va="center")
        ax.axis("off")

    plt.tight_layout()
    plt.show()

    heatmap_fig = plt.figure(figsize=(10,6))                          # Heatmap for Reply versus Comment (top languages)

    if df_reply_vs_comment.empty:
        ax = heatmap_fig.add_subplot(111)
        ax.text(0.5, 0.5, "No data", ha="center", va="center")
        ax.axis("off")
        plt.tight_layout()
        plt.show()

    else:
        pairs = [pairs.split(" > ") for pairs in df_reply_vs_comment["pair"].tolist() if " > " in pairs]

        source_languages = [a for a, _ in pairs]
        destination_languages = [b for _, b in pairs]

        top_source_languages = [
            language for language, _ in Counter(source_languages).most_common(min(10, len(set(source_languages))))
        ]
        top_destination_languages = [
            language for language, _ in Counter(destination_languages).most_common(min(10, len(set(destination_languages))))
        ]
        matrix = np.zeros((len(top_source_languages), len(top_destination_languages)), dtype=int)

        for pair, count in zip(df_reply_vs_comment["pair"], df_reply_vs_comment["count"]):
            if " > " not in pair:
                continue

            a, b = pair.split(" > ")

            if a in top_source_languages and b in top_destination_languages:
                bar_index = top_source_languages.index(a)
                col_index = top_destination_languages.index(b)
                matrix[bar_index, col_index] = count

        ax = heatmap_fig.add_subplot(1, 1, 1)
        image = ax.imshow(matrix, cmap="Blues")
        ax.set_title("Reply vs. Comment (Top Languages)")
        ax.set_xlabel("Reply language")
        ax.set_ylabel("Comment language")
        ax.set_xticks(range(len(top_destination_languages)))
        ax.set_yticks(range(len(top_source_languages)))
        ax.set_xticklabels(top_destination_languages, rotation=45, ha="right")
        ax.set_yticklabels(top_source_languages)

        for row_index in range(matrix.shape[0]):
            for col_index in range(matrix.shape[1]):
                if matrix[row_index, col_index] > 0:
                    ax.text(
                        col_index, row_index,
                        str(matrix[row_index, col_index]), ha="center", va="center", fontsize=9
                    )

        heatmap_fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)

        plt.tight_layout()
        plt.show()


def tokenized_stopwords(words):

    analyzer = CountVectorizer(ngram_range=(1, 1)).build_analyzer()
    expanded = set(words)
    for word in words:
        expanded.update(analyzer(word))
    return sorted(expanded)


def search_terms_as_stopwords():

    terms = set()
    for alternative in PRIMARY_SEARCH_TERMS or []:
        for term in alternative:
            terms.update(word for word in str(term).lower().split() if word)
    for term in SECONDARY_SEARCH_TERMS or []:
        terms.update(word for word in str(term).lower().split() if word)
    return terms


def word_frequencies(df, start_date=None, end_date=None, ngram_range=(1,1), extra_stopwords=None, language_filter=None,
                     min_frequency=2, max_frequency=0.95, max_features=5000, drop_search_terms=True):
    """
    Builds a frequency dictionary for word cloud and top-n list.
    """

    filtered_df = df.copy()

    if start_date is not None:
        filtered_df = filtered_df[filtered_df["date"] >= pd.to_datetime(start_date)]
    if end_date is not None:
        filtered_df = filtered_df[filtered_df["date"] <= pd.to_datetime(end_date)]
    if language_filter:
        if isinstance(language_filter, str):
            language_filter = [code.strip() for code in language_filter.split(",") if code.strip()]
        filtered_df = filtered_df[filtered_df["language"].isin(language_filter)]

    if filtered_df.empty:
        return {}

    default_stopwords = set(STOPWORDS)
    custom_stopwords = {
        stopword.strip().lower() for stopword in (extra_stopwords or []) if stopword.strip()
    }
    searched_for = search_terms_as_stopwords() if drop_search_terms else set()
    if searched_for:
        print(f"  Dropped as search terms: {', '.join(sorted(searched_for))}. All videos "
              f"collected have them by default.")
    all_stopwords = default_stopwords | custom_stopwords | searched_for

    vectorizer = CountVectorizer(
        ngram_range=ngram_range,
        stop_words=tokenized_stopwords(all_stopwords),
        min_df=min_frequency,
        max_df=max_frequency,
        max_features=max_features,
    )

    texts_list = filtered_df["text"].fillna("").astype(str).tolist()
    if not any(texts_list):             # stop if all is empty
        return {}

    try:
        word_matrix = vectorizer.fit_transform(texts_list)  # convert words to matrix
    except ValueError as e:
        logger.info(f"No usable vocabulary for the word cloud: {e}.")
        return {}

    terms = vectorizer.get_feature_names_out()              # calculate occurrence sums
    counts = np.asarray(word_matrix.sum(axis=0)).ravel()

    return dict(zip(terms, counts))


def plot_wordcloud(freqs):
    """ Create a word cloud image. Not really useful """

    if not freqs:
        logger.info("Word Cloud: Nothing to plot (no tokens after filtering).")
        plt.figure()
        plt.text(0.5, 0.5, "No word cloud data to plot", ha="center", va="center")
        plt.axis("off")
        plt.tight_layout()
        plt.show()
        return

    wordcloud = WordCloud(width=1200, height=800, background_color="white",
                          font_path=get_wordcloud_font_path()).generate_from_frequencies(freqs)

    plt.figure(figsize=(12, 8))
    plt.imshow(wordcloud, interpolation="bilinear")
    plt.axis("off")
    plt.title("Word Cloud", fontsize=16)

    plt.tight_layout()
    plt.show()


def lda_fit(df, n_topics=8, min_frequency=5, max_frequency=0.5, ngram_range=(1, 2), stopwords=None, random_state=42):
    """
    Fit the LDA model. Cf. Blei, D. M., Ng, A. Y. & Jordan, M. I. (2003). "Latent Dirichlet Allocation", in: JMLR 3, 993-1022; https://jmlr.org/papers/v3/blei03a.html; https://scikit-learn.org/stable/auto_examples/applications/plot_topics_extraction_with_nmf_lda.html
    """

    texts = df["text"].fillna("").astype(str).tolist()
    vectorizer = CountVectorizer(
        stop_words=tokenized_stopwords(stopwords or list(STOPWORDS)),
        min_df=min_frequency,
        max_df=max_frequency,
        ngram_range=ngram_range,
    )
    try:
        X = vectorizer.fit_transform(texts)
    except ValueError as e:
        logger.info(f"No usable vocabulary for topic modeling: {e}.")
        return None, vectorizer, None, np.array([])

    if X.shape[0] == 0 or X.shape[1] == 0:
        return None, vectorizer, X, np.array([])

    lda = LatentDirichletAllocation(n_components=n_topics, learning_method="batch", random_state=random_state)
    lda.fit(X)
    terms = np.array(vectorizer.get_feature_names_out())

    return lda, vectorizer, X, terms


def lda_topics(df, n_topics=8, n_words=12, min_frequency=5, max_frequency=0.5, stopwords=None, ngram_range=(1,2),
               random_state=42, fitted=None):
    """
    Return a table with the top terms per identified topic.
    """

    lda, vectorizer, X, terms = fitted or lda_fit(
        df, n_topics, min_frequency, max_frequency, ngram_range, stopwords, random_state
    )

    if lda is None or terms.size == 0:
        return pd.DataFrame(columns=["topic", "term", "weight"])

    rows = []

    for topic_index, topic_weights in enumerate(lda.components_):
        top_term_indices = topic_weights.argsort()[::-1][:n_words]
        for term_index in top_term_indices:
            rows.append({
                "topic" : f"Topic {topic_index + 1}",
                "term"  : terms[term_index],
                "weight": float(topic_weights[term_index])
            })

    return pd.DataFrame(rows)


def lda_doc_topics(df, n_topics=8, min_frequency=5, max_frequency=0.5, ngram_range=(1, 2), stopwords=None,
                   random_state=42, fitted=None):
    """
    Choose the dominant topic per document (each row in df).
    """

    lda, vectorizer, X, _ = fitted or lda_fit(df, n_topics, min_frequency, max_frequency, ngram_range, stopwords, random_state)
    if lda is None or X is None or X.shape[0] == 0:
        return pd.DataFrame(columns=["doc_id", "date", "topic", "weight"])

    # Topic distribution per document
    theta = lda.transform(X)                # shape: (n_docs, n_topics)
    if theta.size == 0:
        return pd.DataFrame(columns=["doc_id", "date", "topic", "weight"])

    dominants = theta.argmax(axis=1)        # choose the most probable one
    weights = theta[np.arange(theta.shape[0]), dominants]   # get the probability score

    rows = []

    for doc_index, (topic_index, topic_weight) in enumerate(zip(dominants, weights)):
        rows.append({
            "doc_id": doc_index,
            "date"  : pd.to_datetime(df.iloc[doc_index]["date"], errors="coerce", format="ISO8601", utc=True),
            "topic" : f"Topic {int(topic_index) + 1}",
            "weight": float(topic_weight),
        })

    out = pd.DataFrame(rows)
    return out


def plot_topics_bar(topics_dataframe, number_of_columns=2, document_topics_dataframe=None, date_column_name="date",
                    weight_column_name="weight"):
    """
    Create one multipart plot for the topic modeling results:
    - a bar plot (top terms per topic)
    - another bar plot (procentual contributions of the topics to the whole corpus)
    - date plot, if data allows
    """

    if topics_dataframe.empty:                              # nothing to plot
        empty_figure = plt.figure(figsize=(6, 3))
        plt.text(0.5, 0.5, "Nothing to display", ha="center", va="center")
        plt.axis("off")
        empty_figure.show()
        return

    unique_topics_list = sorted(                            # sort topics alphabetically
        topics_dataframe["topic"].unique(),
        key=lambda topic_label_sort: int(topic_label_sort.split()[1])
    )
    number_of_topics = len(unique_topics_list)

    number_of_rows_for_topics = int(np.ceil(number_of_topics / number_of_columns))  # find out the number of rows

    has_monthly_trend_data = (                              # prepare additional rows for corpus shares and monthly trends
        document_topics_dataframe is not None
        and not document_topics_dataframe.empty
        and date_column_name in document_topics_dataframe.columns
    )
    number_of_extra_rows = 2 if has_monthly_trend_data else 1

    height_ratios_for_grid = [3] * number_of_rows_for_topics + [2] * number_of_extra_rows

    # Prepare the main plot
    figure = plt.figure(figsize=(14, 4 * number_of_rows_for_topics + 5 * number_of_extra_rows))
    grid_spec = figure.add_gridspec(
        nrows=number_of_rows_for_topics + number_of_extra_rows,
        ncols=number_of_columns,
        height_ratios=height_ratios_for_grid,
        wspace=0.25,
        hspace=0.5
    )

    # Top terms per topic
    for topic_index, topic_label in enumerate(unique_topics_list):
        grid_row_index, grid_column_index = divmod(topic_index, number_of_columns)
        axis = figure.add_subplot(grid_spec[grid_row_index, grid_column_index])

        topic_terms_dataframe = topics_dataframe[topics_dataframe["topic"] == topic_label]
        topic_terms_dataframe = topic_terms_dataframe.sort_values("weight", ascending=True)

        axis.barh(topic_terms_dataframe["term"], topic_terms_dataframe["weight"], color="slateblue")
        axis.set_title(topic_label)
        axis.set_xlabel("Weight")
        axis.tick_params(axis="y", labelsize=8)

    # Hide unused cells
    last_used_index = topic_index
    for unused_index in range(last_used_index + 1, number_of_rows_for_topics * number_of_columns):
        grid_row_index, grid_column_index = divmod(unused_index, number_of_columns)
        axis = figure.add_subplot(grid_spec[grid_row_index, grid_column_index])
        axis.axis("off")

    # Corpus topic shares
    if document_topics_dataframe is not None and not document_topics_dataframe.empty:
        document_topics_copy = document_topics_dataframe.copy()
        if weight_column_name not in document_topics_copy.columns:
            document_topics_copy[weight_column_name] = 1.0
        shares_dataframe = (
            document_topics_copy
            .groupby("topic", as_index=False)[weight_column_name]
            .sum()
            .rename(columns={weight_column_name: "total_weight"})
        )
    else:
        shares_dataframe = (
            topics_dataframe.groupby("topic", as_index=False)["weight"].sum().rename(columns={"weight": "total_weight"})
        )

    shares_dataframe["percent"] = (
        shares_dataframe["total_weight"] / shares_dataframe["total_weight"].sum() * 100.0
    )
    shares_dataframe = shares_dataframe.sort_values("percent", ascending=True)

    axis_share = figure.add_subplot(grid_spec[number_of_rows_for_topics, :])
    axis_share.barh(shares_dataframe["topic"], shares_dataframe["percent"], color="steelblue")
    axis_share.set_title("Corpus Topic Shares (%)")
    axis_share.set_xlabel("Percent")

    for bar_index, percent_value in enumerate(shares_dataframe["percent"].to_numpy()):
        axis_share.text(percent_value + 0.2, bar_index, f"{percent_value:.1f}%", va="center", fontsize=8)
    axis_share.set_xlim(left=0)

    # Monthly topic share trend, if data is available
    if has_monthly_trend_data:
        monthly_trend_copy = document_topics_dataframe.copy()
        monthly_trend_copy[date_column_name] = pd.to_datetime(
            monthly_trend_copy[date_column_name], errors="coerce", format="ISO8601", utc=True
        )
        monthly_trend_copy = monthly_trend_copy.dropna(subset=[date_column_name])

        if monthly_trend_copy.empty:
            axis_trend = figure.add_subplot(grid_spec[number_of_rows_for_topics + 1, :])
            axis_trend.text(0.5, 0.5, "No valid dates for monthly trend", ha="center", va="center")
            axis_trend.axis("off")
        else:
            if weight_column_name not in monthly_trend_copy.columns:
                monthly_trend_copy[weight_column_name] = 1.0

            monthly_trend_copy["month"] = (
                monthly_trend_copy[date_column_name]
                .dt.tz_localize(None)
                .dt.to_period("M")
                .dt.to_timestamp()
            )

            month_topic_totals = (
                monthly_trend_copy.groupby(["month", "topic"], as_index=False)[weight_column_name].sum().rename(columns={weight_column_name: "weight"})
            )

            pivot_table = month_topic_totals.pivot(
                index="month",
                columns="topic",
                values="weight"
            ).fillna(0.0)

            # calculate percentages per month
            percentage_table = pivot_table.div(
                pivot_table.sum(axis=1).where(pivot_table.sum(axis=1) != 0, 1.0),
                axis=0
            ) * 100.0

            axis_trend = figure.add_subplot(grid_spec[number_of_rows_for_topics + 1, :])
            for topic_label in unique_topics_list:
                if topic_label in percentage_table.columns:
                    axis_trend.plot(percentage_table.index, percentage_table[topic_label],
                                    label=topic_label, linewidth=1.8)

            axis_trend.set_title("Monthly Topic Share Trend (%)")
            axis_trend.set_ylabel("Percent")
            axis_trend.set_xlabel("Month")
            axis_trend.grid(alpha=0.25)
            axis_trend.legend(
                ncol=min(len(unique_topics_list), 4),
                loc="upper center",
                bbox_to_anchor=(0.5, -0.15),
                frameon=False
            )

    figure.show()


def run_wordcloud(data, start_date=None, end_date=None, ngram_range=(1,1), extra_stopwords=None,
                  lang_filter=None, min_df=2, max_df=0.95, max_features=5000,
                  drop_search_terms=True):
    """
    Prepare & check data and build word cloud.
    """

    df_all = build_comments_df(data)
    if df_all.empty:
        logger.info("No comments found in dataset.")
        return

    freqs = word_frequencies(
        df_all, start_date=start_date, end_date=end_date,
        ngram_range=ngram_range, extra_stopwords=extra_stopwords,
        language_filter=lang_filter, min_frequency=min_df, max_frequency=max_df,
        max_features=max_features, drop_search_terms=drop_search_terms,
    )

    plot_wordcloud(freqs)


def run_topics(data, n_topics=6, n_words=10, min_df=5, max_df=0.6, ngram_range=(1,2)):
    """
    Check & prepare data, then run the topic modeling.
    """

    df_all = build_comments_df(data)
    if df_all.empty:
        plt.figure()
        plt.text(0.5, 0.5, "No data", ha="center", va="center")
        plt.axis("off")
        plt.tight_layout()
        plt.show()
        return

    fitted = lda_fit(df_all, n_topics, min_df, max_df, ngram_range)
    df_topics = lda_topics(
        df_all, n_topics=n_topics, n_words=n_words, min_frequency=min_df, max_frequency=max_df,
        ngram_range=ngram_range, fitted=fitted
    )
    df_doc_topics = lda_doc_topics(
        df_all, n_topics=n_topics, min_frequency=min_df, max_frequency=max_df,
        ngram_range=ngram_range, fitted=fitted
    )
    plot_topics_bar(df_topics, number_of_columns=2, document_topics_dataframe=df_doc_topics)
    return df_topics


def clean_social_media_markers(data, text_col="text"):

    df = build_comments_df(data)

    if text_col not in df.columns:
        print(f"Column '{text_col}' not in DataFrame.")
        return

    PATTERN_URL       = re.compile(r'(https?://\S+|www\.\S+)', flags=re.IGNORECASE)
    PATTERN_EMAIL     = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
    PATTERN_HASHTAG   = re.compile(r'(?<!\w)#([A-Za-z0-9_]{2,})')
    PATTERN_MENTION   = re.compile(r'(?<!\w)@([A-Za-z0-9_]{2,})')

    def to_lower_list(items):
        return [item.lower() for item in items]

    def extract_links(text):
        return PATTERN_URL.findall(text or "")

    def extract_emails(text):
        return PATTERN_EMAIL.findall(text or "")

    def extract_domains(url_list):
        domains = []
        for url in url_list:
            normalized = url if url.startswith(("http://", "https://")) else "http://" + url
            try:
                host = urlparse(normalized).netloc.lower()
                if host.startswith("www."):
                    host = host[4:]
                if host:
                    domains.append(host)
            except ValueError:
                pass
        return domains

    def extract_hashtags(text):
        tags = PATTERN_HASHTAG.findall(text or "")
        tags = to_lower_list(tags)
        seen, result = set(), []
        for tag in tags:
            if tag not in seen:
                seen.add(tag)
                result.append(tag)
        return result

    def extract_mentions(text):
        handles = PATTERN_MENTION.findall(text or "")
        handles = to_lower_list(handles)
        seen, result = set(), []
        for handle in handles:
            if handle not in seen:
                seen.add(handle)
                result.append(handle)
        return result

    result_df = df.copy()
    result_df["links"]        = result_df[text_col].apply(extract_links)
    result_df["emails"]       = result_df[text_col].apply(extract_emails)
    result_df["hashtags"]     = result_df[text_col].apply(extract_hashtags)
    result_df["mentions"]     = result_df[text_col].apply(extract_mentions)
    result_df["link_domains"] = result_df["links"].apply(extract_domains)

    def flatten(col):
        return (item for xs in result_df[col] for item in (xs or []))

    markers = {
        "links"   : set(flatten("links")),
        "emails"  : set(flatten("emails")),
        "hashtags": set(flatten("hashtags")),
        "mentions": set(flatten("mentions")),
        "domains" : set(flatten("link_domains"))
    }

    return markers


HASHED_MARKER = re.compile(r"(?:mentioned|mail)?u[0-9a-f]{20}$", re.IGNORECASE)


def smmarkers(markers, path=MARKERS_TXT):
    print("Extracted social media markers:")
    hashes = set()
    for key, value in markers.items():
        if not value:
            continue
        hashes |= {item for item in value if HASHED_MARKER.fullmatch(str(item))}
        plain = sorted(item for item in value if not HASHED_MARKER.fullmatch(str(item)))
        if plain:
            print(f"-> {key} ({len(plain)}): {plain}")
    if hashes:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(sorted(hashes)) + "\n")
        print(f"{len(hashes):,} hashes saved to '{path}'.")


def tubetalk():

    data = None

    cowsay = """
┌───────────────────────────────────────────────────────────┐
│          ___________________                              │
│         ╱                   ╲      ^__^                   │
│        │      Welcome to     │╲    (oo)╲───────           │
│         ╲     TubeTalk!!    ╱   -- (__)╲       )╲╱╲       │
│          ╲_________________╱          ││─────││           │
│                                      _││    _││           │
└───────────────────────────────────────────────────────────┘
"""
    print(cowsay)

    print("Some of these functions take a while (several minutes).")

    print("Extracting social media markers ...")
    markers = clean_social_media_markers(data)
    smmarkers(markers)

    print("Plotting language distribution ...")
    plot_language_distribution(data, level="comment", top_n=20, normalize=False)

    print("Plotting language mismatches ...")
    plot_language_conflicts(data, top_n=20, normalize=True)

    print("Plotting word cloud, this may take a while ...")
    run_wordcloud(data, ngram_range=(1,2), extra_stopwords=["youtube", "http", "www"])

    print("Running Topic Modeling (LDA), this may take a while ...")
    write_table("tubetalk_topics",
                run_topics(data, n_topics=6, n_words=10, min_df=5, max_df=0.6, ngram_range=(1,2)))
