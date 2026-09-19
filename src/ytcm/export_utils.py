import csv
import html
import json
import logging
import re
import unicodedata

import networkx as nx

from ytcm.helper_utils import safe_write_json

from ytcm import llm_fields

logger = logging.getLogger(__name__)
CTRL_CHARS_PATTERN = re.compile(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]")


def read_videos(path):

    from ytcm.bridge import MalformedCorpus, read_corpus

    try:
        videos, _, _ = read_corpus(path)
        return videos
    except (IOError, json.JSONDecodeError, MalformedCorpus) as e:
        logger.error(f"Could not read '{path}': {e}.")
        return None

def save_comments_to_json(all_comments, filename):

    safe_write_json(all_comments, filename)


def without_control_characters(value):
    return CTRL_CHARS_PATTERN.sub("", str(value))


def esc(value):
    if value is None:
        return "N/A"
    return html.escape(without_control_characters(value), quote=True)


def convert_json_to_html(json_file, output_html):
    videos = read_videos(json_file)
    if videos is None:
        return

    header = """
    <html>
    <head>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            h2 { color: #2c3e50; }
            .video-section { margin-bottom: 30px; }
            .comments { margin-left: 20px; margin-top: 10px; }
            .comment, .reply { margin-bottom: 15px; }
            .comment p, .reply p { margin: 0; }
            .comment-date, .comment-language, .comment-score, .reply-date, .reply-language, .reply-score {
                font-size: 0.9em; color: #7f8c8d;
            }
            .comment-author, .reply-author { font-weight: bold; }
            .divider { border-top: 1px solid #ecf0f1; margin-top: 20px; }
            summary { font-size: 1.2em; font-weight: bold; cursor: pointer; }
            details { margin-bottom: 20px; }
            .replies { margin-left: 20px; border-left: 2px solid #ccc; padding-left: 10px; }
        </style>
    </head>
    <body>
    <h1>Video Report</h1>
    """

    with open(output_html, "w", encoding="utf-8") as handle:
        handle.write(header)

        for video_id, details in videos:
            video_info = details.get("video_info", {})
            comments = details.get("comments", [])

            handle.write(f"""
            <details class="video-section">
                <summary>{esc(video_info.get("title"))}</summary>
                <p><strong>Video ID:</strong> {esc(video_id)}</p>
                <p><strong>Description:</strong> {esc(video_info.get("description"))}</p>
                <p><strong>Duration:</strong> {esc(video_info.get("duration"))}</p>
                <p><strong>Language (title):</strong> {esc(llm_fields.video_language_of(video_info, "N/A"))}</p>
                <p><strong>Language (description):</strong> {esc(llm_fields.video_description_language_of(video_info, "N/A"))}</p>
                <p><strong>Published At:</strong> {esc(video_info.get("published_at"))}</p>
                <p><strong>Channel Name:</strong> {esc(video_info.get("channel_name"))}</p>
                <p><strong>Channel ID:</strong> {esc(video_info.get("channel_id"))}</p>
                <p><strong>Likes:</strong> {esc(video_info.get("likes"))}</p>
                <p><strong>Dislikes:</strong> {esc(video_info.get("dislikes"))}</p>
                <p><strong>Views:</strong> {esc(video_info.get("views"))}</p>
                <h3>Comments:</h3>
                <div class="comments">
            """)

            for comment in comments:
                handle.write(f"""
                    <div class="comment">
                        <p class="comment-author">{esc(comment.get("author_name"))}</p>
                        <p class="comment-date">{esc(comment.get("date"))}</p>
                        <p class="comment-language">{esc(llm_fields.language_of(comment, "N/A"))}</p>
                        <p>{esc(comment.get("text"))}</p>
                        <p class="comment-score">Sentiment: {esc(llm_fields.sentiment_of(comment))} ({esc(llm_fields.sentiment_label_of(comment, "N/A"))}), Blob: {esc(comment.get("blob_sentiment"))}, VADER: {esc(comment.get("vader_sentiment"))}</p>
                        <p><strong>Likes:</strong> {esc(comment.get("likes"))}</p>
                        <p><strong>YouTube Comment ID:</strong> {esc(comment.get("youtube_comment_id"))}</p>
                    </div>
                """)

                replies = comment.get("replies", [])
                if replies:
                    handle.write("<div class='replies'>")
                    for reply in replies:
                        handle.write(f"""
                        <div class="reply">
                            <p class="reply-author">{esc(reply.get("author_name"))}</p>
                            <p class="reply-date">{esc(reply.get("date"))}</p>
                            <p class="reply-language">{esc(llm_fields.language_of(reply, "N/A"))}</p>
                            <p>{esc(reply.get("text"))}</p>
                            <p class="reply-score">Sentiment: {esc(llm_fields.sentiment_of(reply))} ({esc(llm_fields.sentiment_label_of(reply, "N/A"))}), Blob: {esc(reply.get("blob_sentiment"))}, VADER: {esc(reply.get("vader_sentiment"))}</p>
                            <p><strong>Likes:</strong> {esc(reply.get("likes"))}</p>
                            <p><strong>YouTube Reply ID:</strong> {esc(reply.get("youtube_reply_id"))}</p>
                            <p><strong>Parent ID:</strong> {esc(reply.get("youtube_parent_id"))}</p>
                        </div>
                        """)
                    handle.write("</div>")

            handle.write("""
                </div>
            </details>
            <div class="divider"></div>
            """)

        handle.write("""
        </body>
        </html>
        """)



def gephi_safe(string):
    """This avoids unicode encoding errors that prevent Gephi from loading the file."""

    if string is None:
        return ""

    string = str(string)
    string = unicodedata.normalize("NFC", string)
    string = CTRL_CHARS_PATTERN.sub("", string)

    return string


def convert_json_to_gephi(json_name, gephi_name, include_replies=False):

    if not json_name or not gephi_name:
        return

    videos = read_videos(json_name)
    if videos is None:
        return

    G = nx.DiGraph()

    for video_id, video_data in videos:
        video_info = video_data.get("video_info", {})
        comments = video_data.get("comments", [])

        channel_id = gephi_safe(video_info.get("channel_id"))
        channel_name = gephi_safe(video_info.get("channel_name"))

        if not channel_id:
            continue

        if not G.has_node(channel_id):
            G.add_node(channel_id, name=channel_name, label=channel_name)

        for comment in comments:
            author_id = gephi_safe(comment.get("author_channel_id"))
            author_name = gephi_safe(comment.get("author_name"))

            if not author_id:
                continue

            if not G.has_node(author_id):
                G.add_node(author_id, name=author_name, label=author_name)

            if not G.has_edge(channel_id, author_id):
                G.add_edge(channel_id, author_id, weight=1, edge_type="comment")
            else:
                G[channel_id][author_id]["weight"] += 1

            if include_replies:
                for reply in comment.get("replies", []):
                    reply_id = gephi_safe(reply.get("author_channel_id", ""))
                    reply_name = gephi_safe(reply.get("author_name", ""))

                    if not reply_id:
                        continue

                    if not G.has_node(reply_id):
                        G.add_node(reply_id, name=reply_name, label=reply_name)

                    if not G.has_edge(author_id, reply_id):
                        G.add_edge(author_id, reply_id, weight=1, edge_type="reply")
                    else:
                        G[author_id][reply_id]["weight"] += 1

    try:
        nx.write_gexf(G, gephi_name, encoding="utf-8")
    except IOError as e:
        logger.error(f"Could not create network data file: {e}.")


def convert_json_to_csv(json_file, csv_file):

    videos = read_videos(json_file)
    if videos is None:
        return

    def row_for(video_id, video_info, record, kind):
        is_reply = kind == "reply"
        return {
            "video_id": video_id,
            "video_title": video_info.get("title", ""),
            "video_description": video_info.get("description", ""),
            "video_title_language": llm_fields.video_language_of(video_info, ""),
            "video_description_language": llm_fields.video_description_language_of(video_info, ""),
            "video_duration": video_info.get("duration", ""),
            "video_views": video_info.get("views", 0),
            "video_likes": video_info.get("likes", 0),
            "video_dislikes": video_info.get("dislikes", 0),
            "published_at": video_info.get("published_at", ""),
            "channel_name": video_info.get("channel_name", ""),
            "channel_id": video_info.get("channel_id", ""),
            "comment_type": kind,
            "comment_id": record.get("youtube_reply_id" if is_reply else "youtube_comment_id", ""),
            "reply_to_comment_id": record.get("youtube_parent_id", "") if is_reply else "",
            "youtube_comment_id": "" if is_reply else record.get("youtube_comment_id", ""),
            "youtube_reply_id": record.get("youtube_reply_id", "") if is_reply else "",
            "youtube_parent_id": record.get("youtube_parent_id", "") if is_reply else "",
            "author_name": record.get("author_name", ""),
            "author_channel_id": record.get("author_channel_id", ""),
            "author_subscribers": record.get("author_subscribers", "N/A"),
            "text": record.get("text", ""),
            "date": record.get("date", ""),
            "likes": record.get("likes", 0),
            "language": llm_fields.language_of(record, ""),
            "sentiment": llm_fields.sentiment_of(record, ""),
            "llm_sentiment_label": llm_fields.sentiment_label_of(record, ""),
            "llm_sentiment_conf": llm_fields.sentiment_confidence_of(record, ""),
            "langdetect_language": record.get("language", ""),
            "blob_sentiment": record.get("blob_sentiment", 0),
            "vader_sentiment": record.get("vader_sentiment", 0)
        }

    def rows():
        for video_id, content in videos:
            video_info = content.get("video_info", {})
            for comment in content.get("comments", []):
                yield row_for(video_id, video_info, comment, "comment")
                for reply in comment.get("replies", []):
                    yield row_for(video_id, video_info, reply, "reply")

    fieldnames = [
        "video_id", "video_title", "video_description",
        "video_title_language", "video_description_language", "video_duration",
        "video_views", "video_likes", "video_dislikes", "published_at",
        "channel_name", "channel_id",
        "comment_type", "comment_id", "reply_to_comment_id",
        "youtube_comment_id", "youtube_reply_id", "youtube_parent_id",
        "author_name", "author_channel_id", "author_subscribers",
        "text", "date", "likes", "language",
        "sentiment", "llm_sentiment_label", "llm_sentiment_conf",
        "langdetect_language", "blob_sentiment", "vader_sentiment"
    ]

    try:
        with open(csv_file, "w", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            writer.writeheader()
            for row in rows():
                writer.writerow(row)
    except IOError as e:
        logger.error(f"Error writing CSV: {e}.")
