import logging
import re

from ytcm.platform_utils import printable

logger = logging.getLogger(__name__)


def filter_data(data, query, mode="or", words=False):
    from ytcm import repo

    if isinstance(query, str):
        query_list = [query.casefold()]
    else:
        query_list = [q.casefold() for q in query]

    if mode not in ("and", "or"):
        raise ValueError(f"Unknown mode {mode!r}. Use 'and' or 'or'.")

    patterns = [repo.word_pattern(q) for q in query_list] if words else []

    def matches_query(text):
        if not text:
            return False
        if words:
            found = [bool(pattern.search(text)) for pattern in patterns]
        else:
            text_folded = text.casefold()
            found = [q in text_folded for q in query_list]
        return any(found) if mode == "or" else all(found)

    filtered_data = {}

    for video_id, content in data.items():
        include = False
        new_comments = []

        description = content.get("video_info", {}).get("description", "")
        if matches_query(description):
            include = True

        for comment in content.get("comments", []):
            comment_matches = matches_query(comment.get("text", ""))
            new_replies = []

            for reply in comment.get("replies", []):
                if matches_query(reply.get("text", "")):
                    new_replies.append(reply)

            if comment_matches or new_replies:
                new_comment = comment.copy()
                new_comment["replies"] = new_replies
                new_comments.append(new_comment)
                include = True

        if include:
            new_entry = {
                "video_info": content["video_info"],
                "comments": new_comments
            }
            filtered_data[video_id] = new_entry

    return filtered_data


def folded_offsets(text):
    folded, origins = [], []
    for index, character in enumerate(text):
        piece = character.casefold()
        folded.append(piece)
        origins.extend([index] * len(piece))
    origins.append(len(text))
    return "".join(folded), origins


def match_spans(text, query_list, words=False):
    from ytcm import repo

    spans = []
    if words:
        for query in query_list:
            for found in repo.word_pattern(query).finditer(text):
                spans.append(found.span())
        return spans

    folded, origins = folded_offsets(text)
    for query in query_list:
        needle = query.casefold()
        if not needle:
            continue
        start = 0
        while True:
            index = folded.find(needle, start)
            if index == -1:
                break
            spans.append((origins[index], origins[index + len(needle)]))
            start = index + len(needle)
    return spans


def hits(data, query_list, words=False):

    def extract_snippets(text, query_list, context=50):
        text = re.sub(r'\s+', ' ', text.strip())  # delete \n and similar stuff
        snippets = []

        COLOR = "\033[34m"  # nice blue highlight just for showing off :)
        RESET = "\033[0m"

        for begin, end in match_spans(text, query_list, words):
            snippet_start = max(0, begin - context)
            snippet_end = min(len(text), end + context)
            highlighted = COLOR + text[begin:end] + RESET
            snippet = text[snippet_start:begin] + highlighted + text[end:snippet_end]
            snippets.append("..." + snippet.strip() + "...")

        return snippets

    for video_id, content in data.items():
        info = content.get("video_info", {})
        title = info.get("title", "[no title]").strip()
        description = info.get("description", "").strip()
        comments = content.get("comments", [])

        header = f"{video_id} | {title}"
        print(printable(f"\n{video_id} | {title}"))
        print("=" * len(header))

        description_snippets = extract_snippets(description, query_list)
        if description_snippets:
            print("desc:")
            for snippet in description_snippets:
                print(printable(f"  {snippet}"))

        for comment in comments:
            author = comment.get("author_name", "[unknown]")
            c_text = comment.get("text", "")
            comment_snippets = extract_snippets(c_text, query_list)
            if comment_snippets:
                print(printable(f"{author}:"))
                for snippet in comment_snippets:
                    print(printable(f"  {snippet}"))

            for reply in comment.get("replies", []):
                r_author = reply.get("author_name", "[unknown]")
                r_text = reply.get("text", "")
                r_snips = extract_snippets(r_text, query_list)
                if r_snips:
                    print(printable(f"  ↳ {r_author}:"))
                    for snippet in r_snips:
                        print(printable(f"    {snippet}"))
