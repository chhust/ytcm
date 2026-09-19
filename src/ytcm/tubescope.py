""" TubeScope contains a set of statistical analysis functions for the YouTube Comment Miner.
Originally, it was a standalone script, but I decided to move it directly into YTCM.

Ideas and code snippets for this came from a lot of books in addition to the matplotlib, numpy, pandas, and seaborn
documentation:

Alby, Tom:        "Data Science in der Praxis", Bonn:       Rheinwerk,       2022,
McKinney, Wes:    "Datenanalyse mit Python",    Heidelberg: O'Reilly,        2023,
Sarkar, Dipanjan: "Text Analytics with Python", New York:   Springer/Apress, 2019,
VanderPlas, Jake: "Data Science mit Python",    Frechen:    mitp,            2018,
"""

import logging
import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

from ytcm.font_utils import configure_cjk_fonts

from ytcm import llm_fields
from ytcm import repo as repo
from ytcm.results import write_table

logger = logging.getLogger(__name__)

configure_cjk_fonts()


VALID_MODES = repo.VALID_MODES

MODE_LABELS = {
    "c" : {"plural": "Comments",             "singular": "Comment",          "attributive": "Comment"},
    "r" : {"plural": "Replies",              "singular": "Reply",            "attributive": "Reply"},
    "cr": {"plural": "Comments and Replies", "singular": "Comment or Reply", "attributive": "Comment and Reply"},
}


PLOTTABLE_FROM = pd.Timestamp("2005-01-01")


def daily_series(series, fill):

    index = pd.DatetimeIndex(
        pd.to_datetime(pd.Index(series.index), utc=True)).tz_localize(None).normalize()
    series = pd.Series(list(series.values), index=index).sort_index()

    latest = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
    outside = (series.index < PLOTTABLE_FROM) | (series.index > latest)
    if outside.any():
        stray = series.index[outside]
        print(f"{int(outside.sum()):,} of {len(series):,} days fall outside "
              f"{PLOTTABLE_FROM.date()} to {latest.date()} and are left here. "
              f"Earliest is {stray.min().date()}, latest {stray.max().date()}.")
        series = series[~outside]

    if series.empty:
        return series

    span = pd.date_range(series.index.min(), series.index.max(), freq="D")
    return series.reindex(span) if fill is None else series.reindex(span, fill_value=fill)


def resolve_mode(mode=None):

    return repo.resolve_mode(mode)


def mode_label(mode=None, form="plural"):
    """
    The human-readable name of what is being measured, for plot titles and printed output.
    "plural" counts things ("Number of Comments and Replies"), "singular" names one record
    ("Likes per Comment or Reply"), "attributive" modifies a noun ("Comment and Reply Activity").
    """

    return MODE_LABELS[resolve_mode(mode)][form]


LLM_RECORD_FIELDS = ("llm_sentiment", "llm_sentiment_label", "llm_language")


def collect_all_records_from_db(mode=None, corpus=None, years=None):

    return repo.records_df(mode=mode, corpus=corpus, years=years,
                           fields=LLM_RECORD_FIELDS)


def collect_records(video_data, mode=None):
    """
    Flatten one video's comments and/or replies into a single list, according to the mode.
    Every record gets a "kind" field ("comment" or "reply") so the two remain distinguishable.
    """

    mode = resolve_mode(mode)
    records = []

    for comment in video_data.get("comments", []) or []:
        if "c" in mode:
            records.append(dict(comment, kind="comment"))

        if "r" in mode:
            for reply in comment.get("replies", []) or []:
                records.append(dict(reply, kind="reply"))

    return records


def collect_all_records(data=None, mode=None):

    if data is None:
        return collect_all_records_from_db(mode)

    records = []
    for video_data in (data or {}).values():
        records.extend(collect_records(video_data, mode))
    return records




def interaction_density_from_counts(total_comments, total_replies, comments_with_replies, views):

    valid_views = views if views is not None and views == views and views > 0 else None

    replies_per_comment = total_replies / total_comments if total_comments else 0
    reply_trigger_ratio = comments_with_replies / total_comments if total_comments else 0
    replies_per_view = total_replies / valid_views if valid_views else None

    depth = replies_per_comment / (1 + replies_per_comment)
    score = 0.5 * reply_trigger_ratio + 0.5 * depth

    return {
        "replies_per_comment": replies_per_comment,
        "comments_with_replies_ratio": reply_trigger_ratio,
        "replies_per_view": replies_per_view,
        "interaction_density_score": score
    }


def interaction_density(df_comments, video_info):
    """
    Calculates an interaction density score for one video, consisting of:
    - replies per comment
    - share of comments that triggered replies
    This shoud be normalized by views in future.
    This approach is totally experimental and has not a lot of sense currently!
    """

    return interaction_density_from_counts(
        len(df_comments),
        sum(len(replies) for replies in df_comments["replies"]),
        (df_comments["replies"].apply(len) > 0).sum(),
        video_info.get("views", 1))


def plot_interaction_density_distribution(bin_count=30):
    """
    Compute and plot the distribution of interaction density scores across all videos.
    Includes mean, median, and top-5 videos as legend.
    """

    scores = []
    labels = []

    for row in repo.video_stats_df().itertuples():
        if not row.n_comments:
            continue

        try:
            result = interaction_density_from_counts(
                row.n_comments, row.n_replies, row.comments_with_replies, row.views)
            score = result["interaction_density_score"]

            scores.append(score)
            labels.append({
                "id": row.video_id,
                "title": row.title or "[No Title]",
                "score": score
            })

        except Exception as e:
            logger.error(f"Skipping video {row.video_id} due to error: {e}.")
            print(f"Skipping video {row.video_id}.")
            continue

    if not scores:
        print("No interaction density scores found.")
        return

    scores_series = pd.Series(scores)
    mean_val = scores_series.mean()
    median_val = scores_series.median()

    # Sort Top 5 videos by score in descending order
    top_videos = sorted(labels, key=lambda x: x["score"], reverse=True)[:5]

    plt.figure(figsize=(10, 6))
    sns.histplot(scores_series, bins=bin_count, color="cornflowerblue", kde=False)

    plt.axvline(mean_val, color="red", linestyle="--", linewidth=1.5, label=f"Mean: {mean_val:.4f}")
    plt.axvline(median_val, color="orange", linestyle="--", linewidth=1.5, label=f"Median: {median_val:.4f}")

    top_lines = ["Top 5 Videos by Interaction Density Score:"]
    for entry in top_videos:
        short_title = entry["title"][:60] + "…" if len(entry["title"]) > 60 else entry["title"]
        top_lines.append(f"- {entry['id']}: {short_title} ({entry['score']:.4f})")

    top_text = mlines.Line2D([], [], color="white", label="\n".join(top_lines))

    plt.legend(handles=[top_text], loc="upper right", fontsize=9)
    plt.title("Interaction Density Scores")
    plt.xlabel("Interaction Density Score")
    plt.ylabel("Number of Videos")
    plt.tight_layout()
    plt.show()


def extract_video_info(video_id, video_data):
    """
    Separate video info and comments and return them as a tuple.
    """

    try:
        return video_data["video_info"], video_data["comments"]
    except Exception as e:
        logger.error(f"Error extracting video info for video {video_id}: {e}.")
        print(f"Error extracting video info for {video_id}.")
        return None, None


def analyze_comments(comments):
    """
    Process the sentiment analysis for all comments.
    Prefers the LLM score (llm_sentiment); VADER stays as the fallback.
    Both are on the same -1..+1 scale.
    """

    df_comments = comments.copy() if isinstance(comments, pd.DataFrame) else pd.DataFrame(comments)
    df_comments["date"] = pd.to_datetime(df_comments["date"], errors="coerce", format="ISO8601", utc=True)
    df_comments["sentiment"] = df_comments.apply(
        lambda row: llm_fields.sentiment_of(row),
        axis=1
    )

    return df_comments


def get_most_liked_comments(df_comments, top_n=5):
    """
    Return the top n liked comments.
    """

    return df_comments.sort_values("likes", ascending=False).head(top_n)


def calculate_average_sentiment(df_comments):

    sentiment_numeric = pd.to_numeric(df_comments["sentiment"], errors="coerce")
    scored = sentiment_numeric.dropna()
    return (float(scored.mean()) if len(scored) else None), len(scored), len(sentiment_numeric)


def group_comments_by_date(df_comments):

    return df_comments.groupby(df_comments["date"].dt.date).size()


def plot_comment_likes_distribution(df_comments, cap_height=100, bin_count=30, x_min=None, x_max=None):
    """
    Plot like distribution in dynamic buckets.
    The plot will be capped at cap_height.
    bin_count is the max number of bins/buckets. (30 is a good default value.)
    x_min and x_max can define left and right borders of slices.
    """

    total_comments = len(df_comments)
    zero_likes_count = (df_comments["likes"] == 0).sum()
    non_zero_df = df_comments[df_comments["likes"] > 0]
    non_zero_count = len(non_zero_df)
    zero_percent = zero_likes_count / total_comments * 100
    non_zero_percent = non_zero_count / total_comments * 100

    if non_zero_count == 0:
        print("No comments containing likes in the dataset.")
        return

    # Generate the buckets
    min_likes = max(1, non_zero_df["likes"].min())  # 1 excludes the 0-like comments
    max_likes = non_zero_df["likes"].max()

    x_min = x_min if x_min is not None else min_likes
    x_max = x_max if x_max is not None else max_likes

    filtered_df = non_zero_df[(non_zero_df["likes"] >= x_min) & (non_zero_df["likes"] <= x_max)]

    if filtered_df.empty:
        print(f"No comments with >0 likes in range {x_min}–{x_max}.")
        return

    # np.linspace collapses to repeated edges when the range is too narrow for bin_count
    # buckets (in the extreme, every record has the same like count). pd.cut rejects
    # duplicate edges, so widen the range to one bucket per whole like instead.
    bin_edges = np.unique(np.linspace(x_min, x_max, bin_count + 1))

    if len(bin_edges) < 2:
        bin_edges = np.array([x_min - 1, x_max], dtype=float)

    bins = pd.cut(filtered_df["likes"], bins=bin_edges, right=True, include_lowest=True)
    bucket_counts = bins.value_counts().sort_index()

    fig, ax = plt.subplots(figsize=(12, 6))
    outliers = {}

    for i, (bucket, count) in enumerate(bucket_counts.items()):
        label = f"{int(bucket.left)}–{int(bucket.right)}"

        if count > cap_height:
            ax.bar(label, cap_height, color="tomato")
            ax.plot(i, cap_height, marker="^", color="black", markersize=8)
            ax.text(i, cap_height + 1, str(count), ha="center", va="bottom", fontsize=8)
            outliers[label] = count
        else:
            ax.bar(label, count, color="steelblue")
            ax.text(i, count + 0.5, str(count), ha="center", va="bottom", fontsize=8)

    ax.set_title(f"{mode_label(form='attributive')} Likes")
    ax.set_xlabel(f"Likes per {mode_label(form='singular')}")
    ax.set_ylabel(f"Number of {mode_label()}")
    plt.xticks(rotation=45, ha="right")

    legend_lines = [
        f"{zero_likes_count} comments without likes ({zero_percent:.2f} %)",
        f"{non_zero_count} comments with 1 or more likes ({non_zero_percent:.2f} %)",
        "",
    ]

    if outliers:
        legend_lines.append("Absolute Counts for Clipped Bins:")
        for label, count in outliers.items():
            legend_lines.append(f"  - {label}: {count} comments")

    text_legend = mlines.Line2D([], [], color="white", label="\n".join(legend_lines))
    capped_patch = mpatches.Patch(color="tomato", label="Capped Bins")
    normal_patch = mpatches.Patch(color="steelblue", label="Uncapped Bins")

    ax.legend(
        handles=[normal_patch, capped_patch, text_legend],
        loc="upper right",
        fontsize=9
    )

    plt.tight_layout()
    plt.show()


def plot_sentiment_distribution(df_comments):
    """
    Plot the distribution of sentiment scores. Filters out non-numeric "N/A"s.
    Adds additional info about mean and median.
    """

    # Convert to float and filter out N/A scores
    scored = df_comments.copy()
    scored["sentiment"] = pd.to_numeric(scored["sentiment"], errors="coerce")
    total_count = len(scored)
    valid_scores = scored["sentiment"].dropna()
    valid_count = len(valid_scores)
    na_count = total_count - valid_count
    na_percent = na_count / total_count * 100

    if valid_scores.empty:
        print("No sentiment scores to plot.")
        return

    mean_val = valid_scores.mean()
    median_val = valid_scores.median()

    plt.figure(figsize=(10, 6))
    sns.histplot(valid_scores, kde=True, bins=100, color="steelblue")

    plt.axvline(mean_val, color="red", linestyle="--", linewidth=1.5, label=f"Mean  : {mean_val:.2f}")
    plt.axvline(median_val, color="orange", linestyle="--", linewidth=1.5, label=f"Median: {median_val:.2f}")

    na_text = f"{na_count} of {total_count} sentiment values were N/A ({na_percent:.1f} %)."
    plt.gca().text(0.99, 0.95, na_text, transform=plt.gca().transAxes,
                   fontsize=9, ha="right", va="top",
                   bbox=dict(boxstyle="round", facecolor="white", edgecolor="gray")
                   )

    plt.title(f"Distribution of {mode_label(form='attributive')} Sentiment Scores")
    plt.xlabel("Sentiment Score (LLM, VADER as fallback)")
    plt.ylabel("Number")
    plt.legend()
    plt.tight_layout()
    plt.show()


def plot_comments_over_time(comments_per_day):
    """
    Plot comment activity over time with raw counts, rolling average over 1/50th of duration,
    and Top 10 comment days.
    """

    comments_per_day = daily_series(comments_per_day, 0)
    if comments_per_day.empty:
        return
    total_days = (comments_per_day.index[-1] - comments_per_day.index[0]).days  # calculate rolling avg. window
    rolling_window = max(3, total_days // 50)
    rolling_avg = comments_per_day.rolling(window=rolling_window, min_periods=1).mean()

    top_days = comments_per_day.sort_values(ascending=False).head(10)  # prepare top comment days

    fig, ax_left = plt.subplots(figsize=(14, 7))
    raw_line, = ax_left.plot(
        comments_per_day.index,
        comments_per_day.values,
        label = "Raw",
        color = "cornflowerblue",
        linewidth = 1,
        alpha = 0.9
    )
    ax_left.set_ylabel(f"Number of {mode_label()}", color="cornflowerblue")
    ax_left.tick_params(axis="y", labelcolor="cornflowerblue")
    ax_left.grid(True, linestyle="--", alpha=0.3)

    ax_right = ax_left.twinx()
    rolling_line, = ax_right.plot(
        rolling_avg.index,
        rolling_avg.values,
        label = f"Rolling Avg. ({rolling_window} days)",
        color = "midnightblue",
        linewidth = 2
    )
    ax_right.set_ylabel("Rolling Average", color="midnightblue")
    ax_right.tick_params(axis="y", labelcolor="midnightblue")

    top_lines = ["Top Comment Days:"]
    for date, count in top_days.items():
        top_lines.append(f"- {date.strftime('%Y-%m-%d')}: {count} comments")
    text_box = mlines.Line2D([], [], color='white', label="\n".join(top_lines))

    plt.legend(handles=[raw_line, rolling_line, text_box], loc="upper right", fontsize=9)

    handles_left, labels_left = ax_left.get_legend_handles_labels()
    handles_right, labels_right = ax_right.get_legend_handles_labels()
    ax_left.legend(
        handles_left + handles_right + [text_box],
        labels_left + labels_right + [text_box.get_label()],
        loc = "upper right",
        fontsize = 9
    )

    plt.title(f"{mode_label(form='attributive')} Activity Over Time")
    plt.xlabel("Date")
    plt.ylabel(f"Number of {mode_label()}")
    plt.grid(True, linestyle="--", alpha=0.3)
    plt.tight_layout()

    plt.show()


def analyze_replies(df_comments):
    """
    Calculate the percentage of comments with replies.
    This might indicate how much actual 'discussion' takes place.
    Only top-level comments can carry replies, so reply rows are excluded here whatever
    the mode is. Returns None if the frame holds no comments at all (mode "r").
    """

    if "replies" not in df_comments.columns:
        if "kind" not in df_comments.columns or "parent_id" not in df_comments.columns:
            return None
        if resolve_mode() == "c":
            return None
        comment_rows = df_comments[df_comments["kind"] == "comment"]
        if comment_rows.empty:
            return None
        answered = set(df_comments.loc[df_comments["kind"] == "reply", "parent_id"].dropna())
        return comment_rows["message_id"].isin(answered).mean() * 100

    # The lambda creates a True/False boolean indicating if there are replies.
    # This is added as a new column to the DataFrame. Reply rows have no "replies" of their
    # own and show up as NaN, which is not a list and must not be counted as "no replies".
    df_comments["has_replies"] = df_comments["replies"].apply(
        lambda x: len(x) > 0 if isinstance(x, (list, tuple)) else None
    )

    comments_only = df_comments["has_replies"].dropna()
    if comments_only.empty:
        return None

    # As each True is 1 and each False is 0, calculating the average will be a value between 0 and 1.
    # This is converted to a percentage and then returned.
    return comments_only.mean() * 100


def analyze_sentiment_over_time(df_comments):
    """
    Extract sentiment scores over time, preferring llm_sentiment over VADER.
    Missing values are skipped; only valid numeric sentiment scores are averaged.
    """

    df_comments["sentiment"] = df_comments.apply(
        lambda row: llm_fields.sentiment_of(row), axis=1
    )

    df_filtered = df_comments[df_comments["sentiment"] != "N/A"].copy()
    df_filtered["sentiment"] = pd.to_numeric(df_filtered["sentiment"], errors="coerce")

    sentiment_per_day = df_filtered.groupby(df_filtered["date"].dt.date)["sentiment"].mean()  # group per date

    return sentiment_per_day


def plot_sentiment_over_time(sentiment_per_day):
    """
    Plot daily average sentiment over time, with dynamic rolling average, mean, and median lines.
    """

    if sentiment_per_day.empty:
        print("No sentiment data available to plot.")
        return

    sentiment_per_day = daily_series(sentiment_per_day, None)
    if sentiment_per_day.empty:
        return
    total_days = (sentiment_per_day.index[-1] - sentiment_per_day.index[0]).days
    rolling_window = max(3, total_days // 50)
    rolling_avg = sentiment_per_day.rolling(window=rolling_window, min_periods=1).mean()

    mean_sentiment = sentiment_per_day.mean()
    median_sentiment = sentiment_per_day.median()

    plt.figure(figsize=(14, 7))

    raw_line, = plt.plot(
        sentiment_per_day.index,
        sentiment_per_day.values,
        label="Raw",
        color="mediumseagreen",
        linewidth=1.2,
        alpha=0.8
    )

    rolling_line, = plt.plot(
        rolling_avg.index,
        rolling_avg.values,
        label=f"Rolling Avg. ({rolling_window} days)",
        color="darkgreen",
        linewidth=2
    )

    plt.axhline(mean_sentiment, color="gray", linestyle="--", linewidth=1.2, label=f"Mean: {mean_sentiment:.2f}")
    plt.axhline(median_sentiment, color="black", linestyle=":", linewidth=1.2, label=f"Median: {median_sentiment:.2f}")

    text_lines = [
        "Sentiment Trends:",
        "- Raw = daily average sentiment",
        f"- Rolling = smoothed over {rolling_window} days",
        "",
        f"Mean  : {mean_sentiment:.2f}",
        f"Median: {median_sentiment:.2f}",
        f"Total Days: {len(sentiment_per_day)}"
    ]
    text_box = mlines.Line2D([], [], color="white", label="\n".join(text_lines))

    plt.legend(handles=[raw_line, rolling_line, text_box], loc="upper right", fontsize=9)

    plt.title(f"Sentiment Scores over Time ({mode_label()})")
    plt.xlabel("Date")
    plt.ylabel("Average Sentiment Score")
    plt.grid(True, linestyle="--", alpha=0.3)
    plt.tight_layout()

    plt.show()


def plot_participation_timeline():
    """
    Plot the timeline of participating channels in the discussion,
    both as rolling average over time and cumulative unique count.
    """

    events = repo.participation_events()

    if not events:
        print("No participation data available.")
        return

    events_frame = pd.DataFrame(events, columns=["date", "channel_id"])
    events_frame["date"] = pd.to_datetime(
        events_frame["date"], errors="coerce", format="ISO8601",
        utc=True).dt.tz_localize(None).dt.normalize()
    events_frame = events_frame.dropna(subset=["date", "channel_id"])

    daily = daily_series(events_frame.groupby("date")["channel_id"].nunique(), 0)
    if daily.empty:
        print("No participation data available.")
        return

    total_days = (daily.index[-1] - daily.index[0]).days
    rolling_window = max(3, total_days // 50)
    rolling = daily.rolling(window=rolling_window, min_periods=1).mean()

    first_seen = events_frame.groupby("channel_id")["date"].min()
    cumulative_unique = (
        first_seen.value_counts()
        .sort_index()
        .reindex(pd.date_range(daily.index.min(), daily.index.max(), freq="D"), fill_value=0)
        .cumsum()
    )

    fig, ax1 = plt.subplots(figsize=(14, 7))

    ax1.plot(daily.index, rolling, label=f"Rolling Avg. ({rolling_window} days)",
             color="steelblue", linewidth=2)
    ax1.set_xlabel("Date")
    ax1.set_ylabel("Daily Active Channels (Rolling Avg.)", color="steelblue")
    ax1.tick_params(axis="y", labelcolor="steelblue")
    ax1.grid(True, linestyle="--", alpha=0.3)

    ax2 = ax1.twinx()
    ax2.plot(cumulative_unique.index, cumulative_unique.values,
             label="Cumulative Unique Channels",
             color="orange", linestyle="--", linewidth=2)
    ax2.set_ylabel("Cumulative Unique Channels", color="orange")
    ax2.tick_params(axis="y", labelcolor="orange")

    lines, labels = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines + lines2, labels + labels2, loc="upper left", fontsize=9)

    plt.title("Active Channels Over Time")
    plt.tight_layout()
    plt.show()


def plot_interactions_by_weekday(figsize=(12,6), normalize=False):
    """
    Interactions by weekday, broken down by uploads, comments, replies, plus total.
    'normalize=True' would show each series as shares, summing to 1 within series.
    """

    upload_dates, comment_dates, reply_dates = repo.dates_by_kind()

    if not (upload_dates or comment_dates or reply_dates):
        print("No uploads/comments/replies with valid dates found.")
        return

    def to_weekday(dates):
        if not dates:
            return pd.Series(dtype=int)                                 # empty
        parsed = pd.to_datetime(pd.Series(dates), errors="coerce", format="ISO8601", utc=True).dropna()
        return parsed.dt.dayofweek                                           # 0 = Mon, and so on

    wd_uploads = to_weekday(upload_dates)
    wd_comments = to_weekday(comment_dates)
    wd_replies = to_weekday(reply_dates)

    order = [0, 1, 2, 3, 4, 5, 6]
    day_names = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    days = [day_names[day] for day in order]

    def count_per_day(weekdays):
        return (
            weekdays.map(day_names)
            .value_counts()
            .reindex(days)
            .fillna(0)
            .astype(int)
        ) if len(weekdays) else pd.Series(0, index=days, dtype=int)

    uploads_per_day = count_per_day(wd_uploads)
    comments_per_day = count_per_day(wd_comments)
    replies_per_day = count_per_day(wd_replies)
    total_per_day = uploads_per_day + comments_per_day + replies_per_day

    if normalize:
        def as_share(counts):
            total = counts.sum()
            return counts / total if total else counts

        uploads_per_day, comments_per_day, replies_per_day, total_per_day = map(
            as_share, [uploads_per_day, comments_per_day, replies_per_day, total_per_day])

    fig, ax_left = plt.subplots(figsize=figsize)

    positions = np.arange(len(days))
    width = 0.35
    ax_left.bar(positions - width / 2, comments_per_day.values, width, label="Comments")
    ax_left.bar(positions + width / 2, replies_per_day.values, width, label="Replies")

    ax_left.plot(positions, total_per_day.values, linewidth=2, linestyle="-", marker="o", label="Total")

    ax_left.set_xticks(positions)
    ax_left.set_xticklabels(days)
    ax_left.set_xlabel("Weekday")
    ax_left.set_ylabel("Count" if not normalize else "Share")
    ax_left.grid(True, axis="y", linestyle="--", alpha=0.3)

    ax_right = ax_left.twinx()
    line_up, = ax_right.plot(positions, uploads_per_day.values, linestyle="--", marker="s",
                             label="Uploads", color="tab:red")
    ax_right.set_ylabel("Uploads" if not normalize else "Uploads (share)")

    handles_left, labels_left = ax_left.get_legend_handles_labels()
    ax_left.legend(handles_left + [line_up], labels_left + ["Uploads"], loc="upper left", fontsize=9)

    plt.title("Interactions by Weekday")
    plt.tight_layout()
    plt.show()


def plot_views_vs_comments(include_replies=None, color_by="year", annotate_top=5, figsize=(8,7), cmap="viridis",
                           alpha=0.6, edgecolor="white", linewidth=0.4, logx=True, logy=True):
    """
    View counts vs. discussion volume.
    Coloring can be done by year or by discussion-per-view ratio.
    "annotate_top" refers to outliers that will be labeled.
    
    Actually, this looks like a "Hertzsprung Russell Diagram" in astronomy, when stars move on the "main sequence".
    
    It would be super cool having an animated version, showing how videos climb upwards during their "lifetime"
    until they finally stop. However, YT does not provide the view count history.
    """

    mode = resolve_mode()
    if include_replies is None:
        include_replies = "r" in mode
    include_comments = "c" in mode

    rows = []

    for row in repo.video_stats_df().itertuples():
        video_id, views, published = row.video_id, row.views, row.published_at
        if views is None:
            continue

        comment_count, reply_count = int(row.n_comments), int(row.n_replies)
        discussion = ((comment_count if include_comments else 0)
                      + (reply_count if include_replies else 0))

        try:
            year = pd.to_datetime(published, format="ISO8601", utc=True).year if published else None
        except Exception:
            year = None

        rows.append({
            "video_id": video_id,
            "views": views,
            "discussion": discussion,
            "year": year
        })

    videos = pd.DataFrame(rows)
    if videos.empty:
        print("No data for correlation plot.")
        return

    eps = 1e-9
    videos["ratio"] = videos["discussion"] / (videos["views"] + eps)

    q90 = np.percentile(videos["discussion"], 90) if (videos["discussion"] > 0).any() else 1
    videos["size"] = 20 + 120 * np.clip(videos["discussion"] / (q90 if q90 else 1), 0, 1)

    if color_by == "ratio":
        colors = videos["ratio"]
        cbar_label = "Discussion per View"
    else:
        min_year = int(videos["year"].dropna().min()) if videos["year"].notna().any() else 0
        colors = videos["year"].fillna(min_year - 1)
        cbar_label = "Upload Year"

    fit_df = videos[(videos["views"] > 0) & (videos["discussion"] > 0)].copy()
    if not fit_df.empty:
        xlog = np.log10(fit_df["views"])
        ylog = np.log10(fit_df["discussion"])
        slope, intercept = np.polyfit(xlog, ylog, 1)
        trend_views = np.logspace(np.log10(videos["views"].replace(0, np.nan).min()),
                         np.log10(videos["views"].max()), 200)
        trend_discussion = 10 ** (intercept + slope * np.log10(trend_views))
    else:
        trend_views = trend_discussion = None
        slope = intercept = np.nan

    videos["discussion_plot"] = videos["discussion"] + 1e-3     # create a minimal offset to avoid 0s disappearing in log scale

    plt.figure(figsize=figsize)
    scatter = plt.scatter(
        videos["views"], videos["discussion_plot"],             # discussion/discussion_plot: switch 0-comment vids off/on
        c=colors, cmap=cmap, s=videos["size"],
        alpha=alpha, edgecolors=edgecolor, linewidths=linewidth
    )

    if trend_views is not None:
        plt.plot(trend_views, trend_discussion, linestyle="--", linewidth=1.8, color="black",
                 label=f"Trend ~ views^{slope:.2f}\n"
                       f"fitted on {len(fit_df):,} of {len(videos):,} videos "
                       f"({len(fit_df) / len(videos) * 100:.0f}%) with views and discussion above zero")

    if logx: plt.xscale("log")
    if logy: plt.yscale("log")

    plt.xlabel("Views")
    plt.ylabel(mode_label(mode))
    plt.title("Views vs. Discussion Volume")

    cbar = plt.colorbar(scatter)
    cbar.set_label(cbar_label)

    if annotate_top and len(videos) > 0:
        top = videos.sort_values("ratio", ascending=False).head(annotate_top)
        for _, row in top.iterrows():
            plt.annotate(
                row["video_id"],
                xy=(row["views"], row["discussion"]),
                xytext=(5, 5),
                textcoords="offset points",
                fontsize=8,
                alpha=0.9
            )

    if trend_views is not None:
        plt.legend(loc="lower right", bbox_to_anchor=(1, 0.1), frameon=True)    # bbox: move a bit upwards, stay right

    plt.tight_layout()
    plt.show()


def plot_uploads_over_time(freq="ME"):
    """
    Plot the number of uploaded videos over time.
    freq: "D" = daily, "W" = weekly, "M" = monthly, "Y" = yearly
    """

    aliases = {
        "M": "ME",
        "Y": "YE",
    }
    freq = aliases.get(freq, freq)

    if freq not in ["D", "W", "ME", "YE"]:
        freq = "ME"

    dates = [date for date in repo.video_stats_df()["published_at"].tolist() if date]

    if not dates:
        print("No data on video update.")
        return

    series = pd.to_datetime(pd.Series(dates), errors="coerce", format="ISO8601", utc=True).dropna().dt.normalize()
    if series.empty:
        print("Could not parse dates.")
        return

    counts = series.value_counts().sort_index()
    full_index = pd.date_range(counts.index.min(), counts.index.max(), freq="D")
    daily = counts.reindex(full_index, fill_value=0)

    if freq != "D":
        series = daily.resample(freq).sum()
    else:
        series = daily

    total_span = (series.index[-1] - series.index[0]).days or 1     # rolling average as usual
    rolling_window = max(3, total_span // 50)
    rolling = series.rolling(window=rolling_window, min_periods=1).mean()

    top = series.sort_values(ascending=False).head(10)

    plt.figure(figsize=(14, 7))

    raw_line, = plt.plot(series.index, series.values, label="Raw", linewidth=1.2, alpha=0.9)
    roll_line, = plt.plot(rolling.index, rolling.values, label=f"Rolling Avg. ({rolling_window} units)", linewidth=2)

    top_lines = [f"Top Periods ({freq}):"]
    if freq == "D":
        fmt = "%Y-%m-%d"
    elif freq == "W":
        fmt = "CW %G-%V"
    elif freq == "ME":
        fmt = "%Y-%m"
    elif freq == "YE":
        fmt = "%Y"
    else:
        fmt = "%Y-%m-%d"

    for idx, val in top.items():
        label = idx.strftime(fmt) if hasattr(idx, "strftime") else str(idx)
        top_lines.append(f"- {label}: {int(val)} uploads")

    text_box = mlines.Line2D([], [], color="white", label="\n".join(top_lines))

    plt.legend(handles=[raw_line, roll_line, text_box], loc="upper right", fontsize=9)

    if freq == "D":
        title_suffix = "Daily"
        x_label = "Date"
    elif freq == "W":
        title_suffix = "Weekly"
        x_label = "Week"
    elif freq == "ME":
        title_suffix = "Monthly"
        x_label = "Month"
    elif freq == "YE":
        title_suffix = "Yearly"
        x_label = "Year"
    else:
        title_suffix = f"Resampled ({freq})"
        x_label = "Time"

    plt.title(f"Video Uploads Over Time ({title_suffix})")
    plt.xlabel(x_label)
    plt.ylabel("Number of Uploads")
    plt.grid(True, linestyle="--", alpha=0.3)
    plt.tight_layout()
    plt.show()


def collect_views_dataframe():
    """
    Extract a tuple of (video_id, title, views), return DataFrame video_id, title, views.
    """

    stats = repo.video_stats_df()
    stats = stats[stats["views"].notna()]
    views = pd.DataFrame({"video_id": stats["video_id"],
                       "title"   : stats["title"].fillna("[No Title]"),
                       "views"   : stats["views"]})
    if views.empty:
        print("No valid views counts in data.")
        return views

    return views.sort_values("views", ascending=False).reset_index(drop=True)


def shorten(text, n=60):
    return (text[:n] + "…") if isinstance(text, str) and len(text) > n else (text if text else "")


def plot_top_videos_by_views(df, top_n=15, title_suffix=""):
    top = df.head(top_n).copy()
    if top.empty:
        print("No data for top-n views plots.")
        return

    top = top.iloc[::-1]            #   largest count on top
    labels = [f"{shorten(title)}" for title in top["title"]]

    plt.figure(figsize=(12, max(6, 0.45 * len(top))))
    plt.barh(labels, top["views"].values)
    plt.xlabel("Views")
    plt.title(f"Top {len(top)} Viewed Videos" + (f" — {title_suffix}" if title_suffix else ""))

    for i, views_value in enumerate(top["views"].values):
        plt.text(views_value, i, f" {int(views_value):,}", va="center", fontsize=9)
    plt.tight_layout()
    plt.show()


def plot_views_distribution(df, log_hist=True, bins=50):

    views = df["views"].astype(float).values

    if views.size == 0:
        print("No data for view plots.")
        return

    finite_mask = np.isfinite(views)
    views_finite = views[finite_mask]
    if views_finite.size == 0:
        print("No finite data for plots.")
        return

    pos_mask = (views_finite > 0)
    vpos = views_finite[pos_mask]
    if vpos.size == 0:
        print("No positive values; skipping log-space plots.")
        return

    plt.figure(figsize=(12, 6))
    if log_hist:
        plt.hist(vpos, bins=np.logspace(np.log10(vpos.min()), np.log10(vpos.max()), bins))
        plt.xscale("log")
    else:
        plt.hist(vpos, bins=bins)
    plt.xlabel("Views (log scale)" if log_hist else "Views")
    plt.ylabel("Number of Videos")
    plt.title("Video View Counts")
    plt.tight_layout()
    plt.show()

    logv = np.log10(vpos)

    median = np.median(logv)
    q1 = np.percentile(logv, 25)
    q3 = np.percentile(logv, 75)
    geometric_mean = 10 ** np.mean(logv)
    mean = float(np.mean(vpos))

    plt.figure(figsize=(10, 4))
    parts = plt.violinplot(dataset=[logv], vert=False, showmeans=True, showextrema=False, showmedians=True)
    plt.yticks([])
    plt.xlabel("log10(Views)")
    plt.title("Video View Counts")

    stats_text = (
        f"Mean: {mean:,.0f}\n"
        f"Geometric mean: {geometric_mean:,.0f}\n"
        f"Median: {10 ** median:,.0f}\n"
        f"Q1: {10 ** q1:,.0f}\n"
        f"Q3: {10 ** q3:,.0f}"
    )

    plt.legend([parts['bodies'][0]], [stats_text], loc="upper right", fontsize=9, frameon=True)
    plt.tight_layout()
    plt.show()


def analyze_views_static(top_n=15, log_hist=True, bins=50):
    """
    View counts plots
    """

    views = collect_views_dataframe()
    if views.empty:
        return
    write_table("tubescope_views", views)
    plot_top_videos_by_views(views, top_n=top_n)
    plot_views_distribution(views, log_hist=log_hist, bins=bins)


def tubescope():


    logo = """
┌───────────────────────────────────────────────────────────────────┐
│ Welcome to ______      __        _____                         __ │
│           ╱_  __╱_  __╱ ╱_  ___ ╱ ___╱_________  ____  ___    ╱╱╱ │
│            ╱ ╱ ╱ ╱ ╱ ╱ __ ╲╱ _ ╲╲__ ╲╱ ___╱ __ ╲╱ __ ╲╱ _ ╲  ╱╱╱  │
│           ╱ ╱ ╱ ╱_╱ ╱ ╱_╱ ╱  __╱__╱ ╱ ╱__╱ ╱_╱ ╱ ╱_╱ ╱  __╱       │
│          ╱_╱  ╲__,_╱_.___╱╲___╱____╱╲___╱╲____╱ .___╱╲___╱ ╱╱╱    │
│                                          ╱_╱                      │
└───────────────────────────────────────────────────────────────────┘
"""

    print(logo)

    all_records = collect_all_records()

    if all_records.empty if hasattr(all_records, "empty") else not all_records:
        print(f"No {mode_label().lower()} to analyze (TUBESCOPE_MODE = {resolve_mode()!r}).")
        return

    df_comments = analyze_comments(all_records)

    print(f"Generating cumulative analysis plots for {len(repo.video_stats_df()):,} videos "
          f"with {len(df_comments):,} {mode_label().lower()} (TUBESCOPE_MODE = {resolve_mode()!r}).")

    plot_comments_over_time(group_comments_by_date(df_comments))
    plot_participation_timeline()

    plot_comment_likes_distribution(df_comments)
    plot_sentiment_over_time(analyze_sentiment_over_time(df_comments))
    plot_sentiment_distribution(df_comments)

    plot_interaction_density_distribution()
    plot_interactions_by_weekday()

    plot_uploads_over_time()

    plot_views_vs_comments()

    analyze_views_static()
    write_table("tubescope_most_liked", get_most_liked_comments(df_comments, top_n=15))

    return
