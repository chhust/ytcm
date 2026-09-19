"""
TubeConnect compares several fandoms (or, broadly, comments corpora) with each other.
It shows who is active in more than one of these fandoms, how the overlap develops over time,
and if this is statistically "more than pure chance".

This draws on a lot of existing research and literature, which is cited in the functions.
The most important were Strona et al. for curveball, Kool et al. for the null model,
Benjamini/Hochberg for the false discovery correction, Efron/Tibshirani for the bootstrap interval,
and Phipson/Smyth for the p-value flooring. I am also indebted to the matplotlib and seaborn
documentations for visualizations and ideas, and the NetworkX documentation for the G/Gephi export.
"""

import json
import os
import sys
import networkx          as nx
import matplotlib.pyplot as plt
import numpy             as np

from collections import Counter
from dataclasses import dataclass
from tqdm        import tqdm
from datetime    import datetime, timezone
from typing      import Optional

from ytcm.config import (TUBECONNECTSETTINGS, TUBECONNECT_CAP_BASIS,
                         TUBECONNECT_BURN_IN_MULTIPLE, TUBECONNECT_THIN_MULTIPLE)
from ytcm.db import get_conn
from ytcm import platform_utils as platform
from ytcm import repo as repo
from ytcm.font_utils import configure_cjk_fonts

configure_cjk_fonts()

@dataclass
class Edge:
    source_channel_id: str
    target_video_id  : str
    event_type       : str
    weight           : int
    fandom_label     : str
    comment_id       : Optional[str]
    reply_id         : Optional[str]
    comment_datetime : Optional[str]
    reply_datetime   : Optional[str]


def parse_dt(timestamp):
    """
    Parse and convert to UTC timestamps (all YouTube info should be in UTC when downloaded).
    """

    if not timestamp:
        return None

    try:
        if timestamp.endswith("Z"):
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(timezone.utc)

        return datetime.fromisoformat(timestamp).astimezone(timezone.utc)

    except (ValueError, TypeError, AttributeError):
        try:
            base = timestamp.split(".")[0]
            return datetime.fromisoformat(base).replace(tzinfo=timezone.utc)

        except (ValueError, TypeError, AttributeError):
            return None


def ensure_channel_exists(channel_map, channel_id):
    """
    Make sure that a channel dict with role flags really exists.
    """

    if channel_id not in channel_map:
        channel_map[channel_id] = {
            "uploader" : False,
            "commenter": False,
            "replier"  : False,
        }

    return channel_map[channel_id]


def provide_roles_set(role_dict):
    result = set()

    if role_dict.get("uploader"):
        result.add("uploader")
    if role_dict.get("commenter"):
        result.add("commenter")
    if role_dict.get("replier"):
        result.add("replier")

    return result


def provide_roles_string(role_dict):
    return ",".join(sorted(provide_roles_set(role_dict)))




def load_fandom(spec, label, name=None):

    language = spec.get("language")
    corpus = spec.get("corpus")
    name = name or label

    fandom = {"label": label, "name": name, "raw": None, "videos": {}, "channels": {},
              "edges": [], "duplicate_comments": 0, "duplicate_replies": 0}

    conn = get_conn()
    try:
        joins, where, params = repo.filters(None, corpus, None)
        clause = where or "WHERE 1=1"
        if language:
            joins = f"{joins} {repo.LANGUAGE_JOIN}"
            clause = f"{clause} AND {repo.EFFECTIVE_LANGUAGE} = ?"
            params = params + [language]

        rows = list(conn.execute(
            f"SELECT m.video_id, m.message_id, m.kind, m.parent_id, m.author_channel_id, m.date "
            f"FROM messages m {joins} {clause} "
            f"ORDER BY m.video_id, m.kind, m.position, m.message_id", params))

        by_video = {}
        for video_id, message_id, kind, parent_id, author, date in rows:
            entry = by_video.setdefault(video_id, {"comments": {}, "order": [], "replies": {}})
            if kind == "comment":
                if message_id not in entry["comments"]:
                    entry["order"].append(message_id)
                entry["comments"][message_id] = (author, date)
            else:
                entry["replies"].setdefault(parent_id, []).append((message_id, author, date))

        shells = {}
        missing = [(video_id, parent_id) for video_id, entry in by_video.items() for parent_id in entry["replies"]
                   if parent_id not in entry["comments"]]
        for start in range(0, len(missing), 500):
            chunk = missing[start:start + 500]
            ids = [parent_id for _, parent_id in chunk]
            for message_id, date in conn.execute(
                    f"SELECT message_id, date FROM messages "
                    f"WHERE message_id IN ({','.join('?' * len(ids))})", ids):
                shells[message_id] = date

        video_ids = list(by_video)
        for start in range(0, len(video_ids), 500):
            chunk = video_ids[start:start + 500]
            for row in conn.execute(
                    f"SELECT video_id, title, published_at, channel_id, channel_name, duration "
                    f"FROM videos WHERE video_id IN ({','.join('?' * len(chunk))})", chunk):
                video_id = row[0]
                fandom["videos"][video_id] = {
                    "video_id": video_id, "fandom_label": label, "title": row[1],
                    "published_at": row[2], "channel_id": row[3], "channel_name": row[4],
                    "duration": row[5], "raw": {}}
    finally:
        conn.close()

    for video_id in fandom["videos"]:
        entry = by_video[video_id]
        channel_id = fandom["videos"][video_id]["channel_id"]
        if channel_id:
            channel = ensure_channel_exists(fandom["channels"], channel_id)
            channel["uploader"] = True
            fandom["edges"].append(Edge(channel_id, video_id, "uploader", 1, label,
                                        None, None, None, None))

        order = list(entry["order"])
        order += [parent_id for parent_id in entry["replies"] if parent_id not in entry["comments"]]

        for comment_id in order:
            author, date = entry["comments"].get(comment_id, (None, shells.get(comment_id)))
            if author:
                channel = ensure_channel_exists(fandom["channels"], author)
                channel["commenter"] = True
                fandom["edges"].append(Edge(author, video_id, "commenter", 1, label,
                                            comment_id, None, date, None))
            for reply_id, reply_author, reply_date in entry["replies"].get(comment_id, []):
                if reply_author:
                    channel = ensure_channel_exists(fandom["channels"], reply_author)
                    channel["replier"] = True
                    fandom["edges"].append(Edge(reply_author, video_id, "replier", 1, label,
                                                comment_id, reply_id, None, reply_date))

    return fandom


def get_overlap(fandoms, basis):
    """
    Calculate the data overlap period between fandom datasets.
    """

    if basis == "none":
        return None, None

    overlap_start, overlap_end = None, None

    for fandom_data in fandoms:
        fandom_start, fandom_end = None, None

        if basis == "video":
            for video in fandom_data.get("videos", {}).values():
                timestamp = video.get("published_at")
                if timestamp:
                    date = parse_dt(timestamp)
                else:
                    date = None

                if date:
                    fandom_start = date if fandom_start is None else min(fandom_start, date)
                    fandom_end = date if fandom_end is None else max(fandom_end, date)

        elif basis == "comment":
            for edge in fandom_data.get("edges", []):
                timestamp = None
                if edge.event_type == "commenter":
                    timestamp = edge.comment_datetime
                elif edge.event_type == "replier":
                    timestamp = edge.reply_datetime

                if timestamp:
                    date = parse_dt(timestamp)
                else:
                    date = None

                if date:
                    fandom_start = date if fandom_start is None else min(fandom_start, date)
                    fandom_end = date if fandom_end is None else max(fandom_end, date)

        if fandom_start is not None and fandom_end is not None:
            overlap_start = fandom_start if overlap_start is None else max(overlap_start, fandom_start)
            overlap_end = fandom_end if overlap_end is None else min(overlap_end, fandom_end)

    if overlap_start is None or overlap_end is None or overlap_start > overlap_end:
        return None, None

    return overlap_start, overlap_end


def update_channel_roles(fandom):
    """
    Look through the edges of a fandom and reevaluate the channel roles.
    Updates may be necessary if time caps were applied.
    """

    new_roles = {}

    for edge in fandom["edges"]:
        source_id = edge.source_channel_id
        event_type = edge.event_type
        if source_id not in new_roles:
            new_roles[source_id] = {
                "uploader" : False,
                "commenter": False,
                "replier"  : False,
            }

        if event_type == "uploader":
            new_roles[source_id]["uploader"] = True
        elif event_type == "commenter":
            new_roles[source_id]["commenter"] = True
        elif event_type == "replier":
            new_roles[source_id]["replier"] = True

    fandom["channels"] = new_roles


def cap_fandoms_to_overlap_time(fandoms, start, end, basis):
    """
    Extract the overlap period from the fandom datasets.
    """

    if start is None or end is None or basis == "none":
        return

    for fandom in fandoms:
        if basis == "video":
            keep_video_ids = set()
            for video_id, video in list(fandom["videos"].items()):
                ts = video.get("published_at")
                dt = parse_dt(ts) if ts else None
                if dt and (start <= dt <= end):
                    keep_video_ids.add(video_id)
                else:
                    del fandom["videos"][video_id]

            # Only keep edges that connect to still-existing nodes
            fandom["edges"] = [edge for edge in fandom["edges"]
                               if edge.target_video_id in keep_video_ids]

        elif basis == "comment":
            kept_edges = []
            videos_with_edges = set()
            upload_edges_buffer = []

            # Collect comments & replies
            for edge in fandom["edges"]:

                if edge.event_type == "commenter" and edge.comment_datetime:
                    dt = parse_dt(edge.comment_datetime)
                    if dt and (start <= dt <= end):
                        kept_edges.append(edge)
                        videos_with_edges.add(edge.target_video_id)

                elif edge.event_type == "replier" and edge.reply_datetime:
                    dt = parse_dt(edge.reply_datetime)
                    if dt and (start <= dt <= end):
                        kept_edges.append(edge)
                        videos_with_edges.add(edge.target_video_id)

                elif edge.event_type == "uploader":
                    upload_edges_buffer.append(edge)

            # Keep edges if video was either published or commented/replied during the overlap time
            for edge in upload_edges_buffer:

                keep = False
                if edge.target_video_id in videos_with_edges:
                    keep = True
                else:
                    video = fandom["videos"].get(edge.target_video_id)
                    if video and video.get("published_at"):
                        dt = parse_dt(video["published_at"])
                        keep = bool(dt and (start <= dt <= end))

                if keep:
                    kept_edges.append(edge)

            fandom["edges"] = kept_edges

        update_channel_roles(fandom)


def compare_sets(set_a, set_b):

    intersection = set_a & set_b
    union = set_a | set_b
    smaller = min(len(set_a), len(set_b))

    return {
        "A_count"            : len(set_a),
        "B_count"            : len(set_b),
        "overlap_count"      : len(intersection),
        "jaccard"            : (len(intersection) / len(union)) if union else None,
        "overlap_coefficient": (len(intersection) / smaller) if smaller else None,
    }


def compute_pairwise_overlaps(fandoms):
    """
    Calculate metrics showing how two fandom datasets overlap.
    """

    summary = {}
    channels_by_fandom = {}
    pairwise_results = []

    role_names = ["uploader", "commenter", "replier"]

    summary["fandoms"] = []
    for fandom in fandoms:
        fandom_info = {
            "label"     : fandom["label"],
            "name"      : fandom["name"],
            "n_videos"  : len(fandom["videos"]),
            "n_channels": len(fandom["channels"]),
        }
        summary["fandoms"].append(fandom_info)

    for fandom in fandoms:
        label = fandom["label"]
        channel_ids = set(fandom["channels"].keys())
        channels_by_fandom[label] = channel_ids

    for index_a in range(len(fandoms)):
        for index_b in range(index_a + 1, len(fandoms)):
            fandom_a = fandoms[index_a]
            fandom_b = fandoms[index_b]

            channels_a = channels_by_fandom[fandom_a["label"]]
            channels_b = channels_by_fandom[fandom_b["label"]]

            pair_entry = {
                "pair"        : [fandom_a["label"], fandom_b["label"]],
                "role_overlap": {}
            }
            pair_entry.update(compare_sets(channels_a, channels_b))

            def channels_with_role(fandom, role):
                return {channel_id for channel_id, roles in fandom["channels"].items() if roles.get(role)}

            # Role-based overlaps
            for role in role_names:
                pair_entry["role_overlap"][role] = compare_sets(
                    channels_with_role(fandom_a, role),
                    channels_with_role(fandom_b, role),
                )

            pair_entry["audience_overlap"] = compare_sets(
                channels_with_role(fandom_a, "commenter") | channels_with_role(fandom_a, "replier"),
                channels_with_role(fandom_b, "commenter") | channels_with_role(fandom_b, "replier"),
            )

            pairwise_results.append(pair_entry)

    summary["pairwise"] = pairwise_results

    return summary


def build_labels_and_index(fandoms):
    """
    Build a list of fandom labels and a reference from label to index position.
    """

    labels = []
    for fandom in fandoms:
        labels.append(fandom["label"])

    label_to_index = {}
    for position in range(len(labels)):
        label_to_index[labels[position]] = position

    return labels, label_to_index


def initialize_matrices(labels, role_names):
    """Initialize the total interaction matrix and one interaction matrix for each role."""

    size = len(labels)

    total_matrix = np.zeros((size, size), dtype=int)

    role_matrices = {}
    for role in role_names:
        role_matrices[role] = np.zeros((size, size), dtype=int)

    return total_matrix, role_matrices


def map_channel_to_fandom_labels(fandoms):
    """
    Build a reference from channel IDs to fandom labels in which each channel appears.
    """

    channel_to_fandom_labels = {}

    for fandom in fandoms:
        current_label = fandom["label"]
        for channel_id in fandom["channels"].keys():
            if channel_id not in channel_to_fandom_labels:
                channel_to_fandom_labels[channel_id] = set()

            channel_to_fandom_labels[channel_id].add(current_label)

    return channel_to_fandom_labels


def accumulate_interaction_matrices(fandoms, label_to_index, channel_to_fandom_labels, total_matrix, role_matrices):

    for fandom in fandoms:
        for edge in fandom["edges"]:

            target_label = edge.fandom_label
            target_index = label_to_index[target_label]

            source_labels_for_channel = channel_to_fandom_labels.get(edge.source_channel_id, set())
            for source_label in source_labels_for_channel:
                source_index = label_to_index[source_label]
                weight_value = int(edge.weight)

                total_matrix[source_index, target_index] += weight_value
                if edge.event_type in role_matrices:
                    role_matrices[edge.event_type][source_index, target_index] += weight_value


def compute_cross_share(labels, label_to_index, total_matrix):
    """
    Calculate the share of outgoing interactions from each source fandom toward the other fandoms ("cross-share").
    """

    result = {}

    for source_label in labels:
        source_index = label_to_index[source_label]

        total_outgoing = int(total_matrix[source_index, :].sum())
        diagonal_value = int(total_matrix[source_index, source_index])
        outgoing_to_other = int(total_outgoing - diagonal_value)

        if total_outgoing > 0:
            share_value = outgoing_to_other / total_outgoing
        else:
            share_value = 0.0

        result[source_label] = {
            "out_total": total_outgoing,
            "out_cross": outgoing_to_other,
            "cross_share": share_value
        }

    return result


def compute_cross_share_by_role(labels, label_to_index, role_names, role_matrices):
    """
    As above, but based on the role-specific interactions.
    """

    result = {}
    for role in role_names:
        result[role] = {}

    for source_label in labels:
        source_index = label_to_index[source_label]

        for role in role_names:
            row_sum = int(role_matrices[role][source_index, :].sum())
            diagonal_value = int(role_matrices[role][source_index, source_index])
            outgoing_to_other = int(row_sum - diagonal_value)

            if row_sum > 0:
                share_value = outgoing_to_other / row_sum
            else:
                share_value = 0.0

            result[role][source_label] = {
                "out_total": row_sum,
                "out_cross": outgoing_to_other,
                "cross_share": share_value
            }

    return result


def compute_cross_channels(fandoms, labels, role_names, channel_to_fandom_labels):
    """
    Count the unique channels from each fandom interacting with videos in other fandoms (overall and by role).
    """

    cross_channels = {}
    for label in labels:
        cross_channels[label] = set()

    cross_channels_by_role = {}
    for role in role_names:
        cross_channels_by_role[role] = {}
        for label in labels:
            cross_channels_by_role[role][label] = set()

    for fandom in fandoms:
        for edge in fandom["edges"]:

            target_label = edge.fandom_label
            source_labels_for_channel = channel_to_fandom_labels.get(edge.source_channel_id, set())

            for source_label in source_labels_for_channel:
                if source_label != target_label:
                    cross_channels[source_label].add(edge.source_channel_id)
                    if edge.event_type in cross_channels_by_role:
                        cross_channels_by_role[edge.event_type][source_label].add(edge.source_channel_id)

    cross_channel_counts = {}
    for label in labels:
        cross_channel_counts[label] = len(cross_channels[label])

    cross_channel_counts_by_role = {}
    for role in role_names:
        cross_channel_counts_by_role[role] = {}
        for label in labels:
            cross_channel_counts_by_role[role][label] = len(cross_channels_by_role[role][label])

    return cross_channel_counts, cross_channel_counts_by_role


def compute_interaction_attribution(fandoms, channel_to_fandom_labels):

    result = {}
    role_names = ["uploader", "commenter", "replier"]

    for fandom in fandoms:
        counts = {"exclusive": 0, "shared": 0, "total": 0, "by_role": {}}
        for role in role_names:
            counts["by_role"][role] = {"exclusive": 0, "shared": 0, "total": 0}

        for edge in fandom["edges"]:
            memberships = channel_to_fandom_labels.get(edge.source_channel_id, set())
            bucket = "shared" if len(memberships) > 1 else "exclusive"
            weight = int(edge.weight)

            counts[bucket] += weight
            counts["total"] += weight

            if edge.event_type in counts["by_role"]:
                counts["by_role"][edge.event_type][bucket] += weight
                counts["by_role"][edge.event_type]["total"] += weight

        if counts["total"]:
            counts["shared_share"] = counts["shared"] / counts["total"]
        else:
            counts["shared_share"] = None

        result[fandom["label"]] = counts

    return result


def compute_edges_per_video(fandoms):
    """
    Calculate mean and median incoming discussion edges per video for each fandom.
    Only commenter and replier edges count: every video carries exactly one uploader edge,
    which would otherwise put a floor of 1 under a video that without comments.
    """

    result = {}

    for fandom in fandoms:
        target_label = fandom["label"]

        incoming_counts_by_video = Counter()
        for edge in fandom["edges"]:

            if edge.fandom_label == target_label and edge.event_type in ("commenter", "replier"):
                incoming_counts_by_video[edge.target_video_id] += int(edge.weight)

        counts = []
        for video_id in list(fandom["videos"].keys()):
            counts.append(incoming_counts_by_video.get(video_id, 0))

        if len(counts) > 0:
            values_array = np.array(counts, dtype=float)
            mean_value = float(values_array.mean())
            median_value = float(np.median(values_array))
            number_of_videos = int(len(values_array))
        else:
            mean_value = 0.0
            median_value = 0.0
            number_of_videos = 0

        result[target_label] = {
            "mean": mean_value,
            "median": median_value,
            "n_videos": number_of_videos
        }

    return result


def matrices_to_lists(role_names, total_matrix, role_matrices):
    """
    Convert matrices to lists.
    """

    interaction_matrix = total_matrix.tolist()

    interaction_matrix_by_role = {}
    for role in role_names:
        interaction_matrix_by_role[role] = role_matrices[role].tolist()

    return interaction_matrix, interaction_matrix_by_role


def compute_cross_fandom_interactions(fandoms):
    """
    Calculate cross-fandom interaction info.
    """

    role_names = ["uploader", "commenter", "replier"]

    # Preparation & accumulation
    labels, label_to_index = build_labels_and_index(fandoms)
    total_matrix, role_matrices = initialize_matrices(labels, role_names)
    channel_to_fandom_labels = map_channel_to_fandom_labels(fandoms)

    accumulate_interaction_matrices(
        fandoms,
        label_to_index,
        channel_to_fandom_labels,
        total_matrix,
        role_matrices,
    )

    # Calculate metrics
    cross_share = compute_cross_share(labels, label_to_index, total_matrix)
    cross_share_by_role = compute_cross_share_by_role(labels, label_to_index, role_names, role_matrices)

    # Process cross-fandom channel links
    cross_channel_counts, cross_channel_counts_by_role = compute_cross_channels(
        fandoms, labels, role_names, channel_to_fandom_labels
    )

    # Calculate edges per video and convert data
    edges_per_video = compute_edges_per_video(fandoms)
    interaction_attribution = compute_interaction_attribution(fandoms, channel_to_fandom_labels)
    interaction_matrix, interaction_matrix_by_role = matrices_to_lists(role_names, total_matrix, role_matrices)

    return {
        "labels"                            : labels,
        "interaction_matrix"                : interaction_matrix,
        "interaction_matrix_by_role"        : interaction_matrix_by_role,
        "interaction_matrix_note"           : ("Rows are interactions performed by members of that fandom. "
                                               "Channels belonging to several fandoms are counted in each of "
                                               "their rows, so cells must not be summed; see "
                                               "interaction_attribution for an exact partition."),
        "interaction_attribution"           : interaction_attribution,
        "cross_share"                       : cross_share,
        "cross_share_by_role"               : cross_share_by_role,
        "cross_fandom_channel_count"        : cross_channel_counts,
        "cross_fandom_channel_count_by_role": cross_channel_counts_by_role,
        "edges_per_video"                   : edges_per_video
    }


def compute_summary(fandoms, overlap_time):
    """
    Calculate the combined summary of pairwise overlaps and cross-fandom interactions, including the overlap time info.
    """

    base = compute_pairwise_overlaps(fandoms)
    cross = compute_cross_fandom_interactions(fandoms)
    base.update(cross)

    start, end, basis = overlap_time

    if start and end:
        base["overlap_window"] = {
            "basis"    : basis,
            "start_utc": start.isoformat(),
            "end_utc"  : end.isoformat(),
            "days"     : (end - start).days
        }
    else:
        base["overlap_window"] = {
            "basis"    : basis,
            "start_utc": None,
            "end_utc"  : None,
            "days"     : None
        }

    return base


def add_video_nodes(graph, fandoms):
    """
    Add all video nodes to the graph with basic metadata.
    """

    for fandom in fandoms:
        fandom_label = fandom["label"]

        for video_id, video_meta in fandom["videos"].items():
            graph.add_node(
                video_id,
                label=video_meta.get("title") or video_id,
                node_type="video",
                fandoms=fandom_label,
                roles="",
                fandom_count=1,
                in_multiple=False,
                degree_est=0,
                video_title=video_meta.get("title") or "",
                video_published_at=video_meta.get("published_at") or "",
                video_channel_name=video_meta.get("channel_name") or "",
            )

    return graph


def ensure_channel_node(graph, fandom, channel_id):
    """
    Ensure that a channel node exists; if it already exists, merge metadata.
    """

    if not graph.has_node(channel_id):
        roles_info = fandom["channels"].get(channel_id, {})
        graph.add_node(
            channel_id,
            label=channel_id,
            node_type="channel",
            fandoms=fandom["label"],
            roles=provide_roles_string(roles_info),
            fandom_count=1,
            in_multiple=False,
            degree_est=0,
            video_title="",
            video_published_at="",
            video_channel_name="",
        )

        return graph

    node_data = graph.nodes[channel_id]
    if node_data.get("node_type") == "channel":
        existing_labels = set(
            [label for label in (node_data.get("fandoms") or "").split(",") if label]
        )
        if fandom["label"] not in existing_labels:
            existing_labels.add(fandom["label"])
            node_data["fandoms"] = ",".join(sorted(existing_labels))
            node_data["fandom_count"] = len(existing_labels)
            if node_data["fandom_count"] >= 2:
                node_data["in_multiple"] = True
            else:
                node_data["in_multiple"] = False

        previous_roles = set(
            [role for role in (node_data.get("roles") or "").split(",") if role]
        )
        current_roles = provide_roles_set(fandom["channels"].get(channel_id, {}))
        node_data["roles"] = ",".join(sorted(previous_roles | current_roles))

    return graph


def add_edge_and_update_degrees(graph, source_channel_id, target_video_id, event_type, weight, fandom_origin,
                                comment_id, reply_id):
    """
    Add an edge from channel to video, update degrees for both nodes.
    """

    graph.add_edge(
        source_channel_id,
        target_video_id,
        edge_type=event_type,
        weight=int(weight),
        fandom_origin=fandom_origin,
        video_id=target_video_id if event_type in {"uploader", "commenter", "replier"} else "",
        comment_id=comment_id or "",
        reply_id=reply_id or ""
    )

    # Update degree estimate: sum of all edge weights connected to the node
    graph.nodes[source_channel_id]["degree_est"] = int(graph.nodes[source_channel_id].get("degree_est", 0)) + int(weight)
    graph.nodes[target_video_id]["degree_est"] = int(graph.nodes[target_video_id].get("degree_est", 0)) + int(weight)

    return graph


def build_graph(fandoms):
    """
    Build a graph (as a "MultiDiGraph)" from the given fandom datasets.
    """

    graph = nx.MultiDiGraph()

    graph = add_video_nodes(graph, fandoms)                 # add video nodes plus metadata

    seen_interactions = set()
    duplicate_interactions = 0

    for fandom in fandoms:                                  # add channels & edge info
        for edge in fandom["edges"]:
            source_channel_id = edge.source_channel_id
            target_video_id = edge.target_video_id
            event_type = edge.event_type
            weight = edge.weight
            fandom_origin = edge.fandom_label
            comment_id = edge.comment_id
            reply_id = edge.reply_id

            # Ensure channel node exists, combine the metadata
            graph = ensure_channel_node(graph, fandom, source_channel_id)

            # Skip if the target video node was filtered out earlier
            if not graph.has_node(target_video_id):
                continue

            interaction_id = reply_id or comment_id or ("uploader", source_channel_id, target_video_id)
            if interaction_id in seen_interactions:
                duplicate_interactions += 1
                continue
            seen_interactions.add(interaction_id)

            # Add edge, update degrees
            graph = add_edge_and_update_degrees(graph, source_channel_id, target_video_id, event_type, weight,
                                                fandom_origin, comment_id, reply_id)

    if duplicate_interactions:
        print(f"Graph: {duplicate_interactions:,} interactions appeared in more than one fandom file "
              f"and were added once.")

    return graph


def export_gexf(G, out_path, graph_label=None):
    """
    Save the graph as GEXF for Gephi.
    """

    if graph_label:
        G.graph["label"] = graph_label

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    nx.write_gexf(G, out_path, encoding="utf-8")

    return out_path


def save_summary_json(summary, out_path):
    """
    Save general summary info as JSON.
    """

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    return out_path


def plot_interaction_matrix(fandom_labels, matrix, title):
    array = np.array(matrix, dtype=float)

    fig, ax = plt.subplots()
    im = ax.imshow(array)

    ticks = np.arange(len(fandom_labels))
    ax.set_xticks(ticks)
    ax.set_yticks(ticks)
    ax.set_xticklabels(fandom_labels)
    ax.set_yticklabels(fandom_labels)

    ax.set_xlabel("Target fandom")
    ax.set_ylabel("Source fandom")
    ax.set_title(title)

    cmap = im.get_cmap()
    norm = im.norm

    n_rows, n_cols = array.shape
    for row in range(n_rows):
        for col in range(n_cols):
            val = array[row, col]

            rgba = cmap(norm(val))
            r, g, b, _ = rgba
            brightness = 0.299*r + 0.587*g + 0.114*b
            text_color = "black" if brightness > 0.5 else "white"

            ax.text(col, row, str(int(val)),
                    ha="center", va="center",
                    color=text_color, fontsize=8)

    fig.colorbar(im, ax=ax)
    plt.tight_layout()
    plt.show()


def plot_bar(data_dict, title, ylabel):
    """
    Plot a bar chart from a dictionary.
    """

    labels = list(data_dict.keys())
    values = [data_dict[label] for label in labels]
    positions = np.arange(len(labels))

    figure = plt.figure()
    axes = figure.gca()
    axes.bar(positions, values)

    axes.set_xticks(positions)
    axes.set_xticklabels(labels, rotation=45, ha="right")
    axes.set_ylabel(ylabel)
    axes.set_title(title)

    plt.tight_layout()
    plt.show()


def generate_plots(summary):
    """
    Create overview plots.
    """

    labels = summary.get("labels", [])
    interaction_matrix = summary.get("interaction_matrix", [])
    plot_interaction_matrix(labels, interaction_matrix,
                            "Interactions by members of (row) toward videos of (column)\n"
                            "shared channels count in every row they belong to; do not sum cells")

    interaction_matrix_by_role = summary.get("interaction_matrix_by_role", {})
    for role in interaction_matrix_by_role:
        role_matrix = interaction_matrix_by_role[role]
        plot_interaction_matrix(labels, role_matrix, f"Interaction matrix by role: {role}")

    cross_share = summary.get("cross_share", {})
    cross_share_map = {}
    for label in labels:
        value = 0.0
        if label in cross_share and "cross_share" in cross_share[label]:
            value = cross_share[label]["cross_share"]
        cross_share_map[label] = value
    plot_bar(cross_share_map, "Cross-share by source fandom", "share")

    cross_share_by_role = summary.get("cross_share_by_role", {})
    for role in cross_share_by_role:
        role_map = {}
        per_label = cross_share_by_role[role]
        for label in labels:
            value = 0.0
            if label in per_label and "cross_share" in per_label[label]:
                value = per_label[label]["cross_share"]
            role_map[label] = value
        plot_bar(role_map, f"Cross-share by source fandom ({role})", "share")

    cross_fandom_channel_count = summary.get("cross_fandom_channel_count", {})
    overall_counts = {}
    for label in labels:
        overall_counts[label] = cross_fandom_channel_count.get(label, 0)
    plot_bar(overall_counts, "Unique channels interacting cross-fandom", "count")

    cross_fandom_channel_count_by_role = summary.get("cross_fandom_channel_count_by_role", {})
    for role in cross_fandom_channel_count_by_role:
        role_counts_map = {}
        per_label_counts = cross_fandom_channel_count_by_role[role]
        for label in labels:
            role_counts_map[label] = per_label_counts.get(label, 0)
        plot_bar(role_counts_map, f"Unique channels cross-fandom ({role})", "count")

    edges_per_video = summary.get("edges_per_video", {})
    mean_map = {}
    median_map = {}
    for label in labels:
        stats = edges_per_video.get(label, {})
        mean_map[label] = stats.get("mean", 0.0)
        median_map[label] = stats.get("median", 0.0)

    plot_bar(mean_map, "Discussion edges per video (mean) by fandom", "comments+replies/video")
    plot_bar(median_map, "Discussion edges per video (median) by fandom", "comments+replies/video")

    interaction_attribution = summary.get("interaction_attribution", {})
    if interaction_attribution:
        shared_share_map = {}
        for label in labels:
            share = interaction_attribution.get(label, {}).get("shared_share")
            shared_share_map[label] = share if share is not None else 0.0
        plot_bar(shared_share_map, "Share of interactions made by channels active in several fandoms", "share")


OVERLAP_PERMUTATIONS = 1000
OVERLAP_BOOTSTRAPS   = 2000
OVERLAP_RANDOM_SEED  = 20260731
SIGNIFICANCE_LEVEL   = 0.05

MEMBERSHIP_BASES = ("audience", "all")

POPULATION_FRAME = "corpus"


def interaction_identity(edge):
    """
    A stable id for each interaction, so a record shared by two fandom files is counted once.
    """

    return edge.reply_id or edge.comment_id or ("uploader", edge.source_channel_id, edge.target_video_id)


def event_year(edge, video_year):
    """
    The year an interaction happened. Comments and replies carry their own timestamp;
    an upload is dated by the video's publication.
    """

    if edge.event_type == "commenter":
        parsed = parse_dt(edge.comment_datetime) if edge.comment_datetime else None
        return parsed.year if parsed else None

    if edge.event_type == "replier":
        parsed = parse_dt(edge.reply_datetime) if edge.reply_datetime else None
        return parsed.year if parsed else None

    return video_year.get(edge.target_video_id)


def yearly_membership(fandoms):

    video_year = {}
    for fandom in fandoms:
        for video_id, video in fandom.get("videos", {}).items():
            timestamp = video.get("published_at")
            parsed = parse_dt(timestamp) if timestamp else None
            if parsed:
                video_year[video_id] = parsed.year

    members  = {basis: {fandom["label"]: {} for fandom in fandoms} for basis in MEMBERSHIP_BASES}
    activity = {basis: {} for basis in MEMBERSHIP_BASES}
    counted  = set()

    for fandom in fandoms:
        label = fandom["label"]
        for edge in fandom.get("edges", []):
            year = event_year(edge, video_year)
            if year is None:
                continue

            bases = ["all"] if edge.event_type == "uploader" else ["all", "audience"]
            identity = interaction_identity(edge)

            for basis in bases:
                members[basis][label].setdefault(year, set()).add(edge.source_channel_id)

                if (basis, identity) not in counted:
                    counted.add((basis, identity))
                    per_year = activity[basis].setdefault(year, {})
                    per_year[edge.source_channel_id] = per_year.get(edge.source_channel_id, 0) + 1

    return members, activity


def bootstrap_jaccard_interval(both, a_only, b_only, population_size, rng, draws=OVERLAP_BOOTSTRAPS):
    """
    Percentile confidence interval for the Jaccard index, by resampling channels.
    Cf. Efron & Tibshirani (1993), "An Introduction to the Bootstrap"; https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html
    """

    union = both + a_only + b_only
    if union == 0 or population_size == 0:
        return None, None

    neither = max(0, population_size - union)
    probabilities = np.array([both, a_only, b_only, neither], dtype=float) / population_size
    samples = rng.multinomial(population_size, probabilities, size=draws)

    resampled_union = samples[:, 0] + samples[:, 1] + samples[:, 2]
    usable = resampled_union > 0
    if not usable.any():
        return None, None

    ratios = samples[usable, 0] / resampled_union[usable]
    return float(np.percentile(ratios, 2.5)), float(np.percentile(ratios, 97.5))


def membership_patterns(members_by_label, labels, year):
    """
    One integer per channel, with bit f set when that channel belongs to fandom f this year.
    Channels in no fandom at all are left out: they carry no information and can't be swapped.
    """

    patterns = {}
    for position, label in enumerate(labels):
        for channel in members_by_label[label].get(year, set()):
            patterns[channel] = patterns.get(channel, 0) | (1 << position)

    return np.array(list(patterns.values()), dtype=np.int64)


def curveball_trade(patterns, first, second, bit_choice):
    """
    cf. Strona, G., Nappo, D., Boccacci, F., Fattorini, S. & San-Miguel-Ayanz, J. (2014). "A fast and unbiased procedure to randomize ecological binary matrices with fixed row and column totals", in: Nature Communications 5, 4114, https://www.nature.com/articles/ncomms5114; Buarque, B., Vogl, M., Kaye, A. (2025), "Growing and Pruning the Archive: An Agent-Based Model to Build Letter Correspondence Networks", in: Digital Scholarship in the Humanities 40, no. 4, 1-14. -- Here, rows = channels, columns = fandoms. "1" means membership in a specific year and membership basis. Memberships are randomized, but preserve the fandom sizes and the number of memberships of each channel (channels without memberships are left out). The null statistic is the number of channels shared by a pair of fandoms; the all-pairs version evaluates the same states for all pairs.
    """

    left, right = patterns[first], patterns[second]

    if isinstance(left, (set, frozenset)):
        shared = left & right
        pool = (left | right) - shared
        if not pool:
            return
        available = sorted(pool)
        keep = len(left - shared)
        taken = bit_choice(available, keep)
        patterns[first] = shared | set(taken)
        patterns[second] = shared | (pool - set(taken))
        return

    left, right = int(left), int(right)
    shared = left & right
    pool = (left | right) & ~shared
    if pool == 0:
        return

    available = [bit for bit in range(pool.bit_length()) if pool >> bit & 1]
    keep = bin(left & ~shared).count("1")

    taken = bit_choice(available, keep)
    to_left = 0
    for bit in taken:
        to_left |= 1 << bit

    patterns[first] = shared | to_left
    patterns[second] = shared | (pool & ~to_left)


def schedule_for(size, burn_in=None, thin=None):

    if burn_in is None:
        burn_in = int(TUBECONNECT_BURN_IN_MULTIPLE * size)
    if thin is None:
        thin = max(1, int(TUBECONNECT_THIN_MULTIPLE * size))
    return burn_in, thin


def _trade_stream(rng, size, steps, block=1 << 20):

    drawn = 0
    firsts = seconds = None
    position = 0
    while True:
        if firsts is None or position >= len(firsts):
            take = min(block, steps - drawn)
            if take <= 0:
                return
            firsts = rng.integers(0, size, size=take)
            seconds = rng.integers(0, size, size=take)
            drawn += take
            position = 0
        yield int(firsts[position]), int(seconds[position])
        position += 1


def fixed_margins_null(patterns, index_a, index_b, rng, permutations=OVERLAP_PERMUTATIONS,
                       burn_in=None, thin=None, report=True):

    pair = (index_a, index_b)
    return fixed_margins_null_all_pairs(patterns, [pair], rng, permutations,
                                        burn_in, thin, report)[pair]


def fixed_margins_null_all_pairs(patterns, pair_positions, rng, permutations=OVERLAP_PERMUTATIONS,
                                 burn_in=None, thin=None, report=True):

    empty = {pair: np.zeros(0, dtype=np.int64) for pair in pair_positions}
    if patterns.size == 0 or not pair_positions:
        return empty

    state = patterns.copy()
    size = state.size
    burn_in, thin = schedule_for(size, burn_in, thin)

    if report:
        print(f"  fixed-margins null: {size:,} channels, burn-in {burn_in:,} trades, "
              f"one sample per {thin:,}, {permutations:,} samples, "
              f"{len(pair_positions)} {platform.plural(len(pair_positions), 'pair')} "
              f"off one chain")

    trades = _trade_stream(rng, size, burn_in + permutations * thin)

    def bit_choice(available, keep):
        return rng.permutation(available)[:keep] if keep else []

    masks = {pair: (1 << pair[0]) | (1 << pair[1]) for pair in pair_positions}
    overlaps = {pair: np.empty(permutations, dtype=np.int64) for pair in pair_positions}

    for _ in range(burn_in):
        first, second = next(trades)
        if first != second:
            curveball_trade(state, first, second, bit_choice)

    for draw in range(permutations):
        for _ in range(thin):
            first, second = next(trades)
            if first != second:
                curveball_trade(state, first, second, bit_choice)

        for pair, mask in masks.items():
            overlaps[pair][draw] = int(((state & mask) == mask).sum())

    return overlaps


def permutation_overlap_null(set_a, set_b, activity_by_channel, rng, permutations=OVERLAP_PERMUTATIONS):
    """
    cf. Kool, W., van Hoof, H. & Welling, M. (2019). "Stochastic Beams and Where To Find Them: The Gumbel-Top-k Trick for Sampling Sequences Without Replacement", in: PMLR 97, 3499–3508, https://proceedings.mlr.press/v97/kool19a.html.
    """

    channels = list(activity_by_channel)
    population_size = len(channels)
    size_a, size_b = len(set_a), len(set_b)

    if (population_size == 0 or size_a == 0 or size_b == 0
            or size_a > population_size or size_b > population_size):
        return 0.0, 1.0, np.zeros(0)

    weights = np.array([activity_by_channel[channel] for channel in channels], dtype=float)
    log_weights = np.log(np.maximum(weights, 1.0))

    observed = len(set_a & set_b)
    membership = np.zeros(population_size, dtype=bool)
    overlaps = np.empty(permutations, dtype=np.int64)


    for run in range(permutations):
        keys_a = log_weights + rng.gumbel(size=population_size)
        keys_b = log_weights + rng.gumbel(size=population_size)

        drawn_a = np.argpartition(keys_a, -size_a)[-size_a:]
        drawn_b = np.argpartition(keys_b, -size_b)[-size_b:]

        membership[drawn_a] = True
        overlaps[run] = int(membership[drawn_b].sum())
        membership[drawn_a] = False

    expected = float(overlaps.mean())
    at_least_as_extreme = int((np.abs(overlaps - expected) >= abs(observed - expected)).sum())

    p_value = float((1 + at_least_as_extreme) / (permutations + 1))     # https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.permutation_test.html

    return expected, p_value, overlaps


def benjamini_hochberg(p_values):
    """
    Benjamini-Hochberg false discovery rate correction. Returns q-values in the input order.
    Cf. Benjamini, Y. & Hochberg, Y. (1995). "Controlling the False Discovery Rate: A Practical and Powerful Approach to Multiple Testing", in: JRSS B 57(1), 289–300, https://doi.org/10.1111/j.2517-6161.1995.tb02031.x; https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.false_discovery_control.html
    """

    count = len(p_values)
    if count == 0:
        return []

    order = sorted(range(count), key=lambda position: p_values[position])
    q_values = [0.0] * count
    previous = 1.0

    for rank, position in enumerate(reversed(order), start=1):
        index = count - rank + 1
        value = min(previous, p_values[position] * count / index)
        q_values[position] = value
        previous = value

    return q_values


def population_for(activity_by_year, year, frame=POPULATION_FRAME):
    """
    The channels treated as able to belong to either fandom in this year, with the activity
    weights the null model draws on. See POPULATION_FRAME for what the two choices cost.
    """

    if frame == "year":
        return dict(activity_by_year.get(year, {}))

    combined = {}
    for per_year in activity_by_year.values():
        for channel, count in per_year.items():
            combined[channel] = combined.get(channel, 0) + count

    for channel in combined:
        combined[channel] = max(1, combined[channel])

    return combined


def analyse_overlap_over_time(fandoms, permutations=OVERLAP_PERMUTATIONS, seed=OVERLAP_RANDOM_SEED,
                              frame=POPULATION_FRAME, bootstraps=OVERLAP_BOOTSTRAPS,
                              activity_null=False):
    """
    Compare every pair of fandoms in every year, on every membership basis.
    Reports the descriptive indices (Jaccard, overlap coefficient) together with what the overlap
    would have been by chance, so that neither can be read without the other. Returns a flat list
    of result rows, one per pair, year and basis.
    """

    members, activity = yearly_membership(fandoms)
    labels = [fandom["label"] for fandom in fandoms]
    rng = np.random.default_rng(seed)
    rows = []

    years_by_basis = {basis: sorted({year for label in labels for year in members[basis][label]})
                      for basis in MEMBERSHIP_BASES}
    pair_count = len(labels) * (len(labels) - 1) // 2
    cells = sum(len(years) * pair_count for years in years_by_basis.values())
    progress = tqdm(total=cells, desc="overlap", unit="cell")

    pair_positions = [(first, second)
                      for first in range(len(labels))
                      for second in range(first + 1, len(labels))]

    for basis in MEMBERSHIP_BASES:
        years = years_by_basis[basis]

        for year in years:
            patterns = membership_patterns(members[basis], labels, year)
            null_by_pair = fixed_margins_null_all_pairs(patterns, pair_positions, rng, permutations)
            population = population_for(activity[basis], year, frame)

            for first, second in pair_positions:
                    label_a, label_b = labels[first], labels[second]
                    progress.set_postfix_str(f"{basis} {label_a}-{label_b} {year}", refresh=False)
                    progress.update(1)
                    set_a = members[basis][label_a].get(year, set())
                    set_b = members[basis][label_b].get(year, set())

                    row = {
                        "basis"          : basis,
                        "fandom_a"       : label_a,
                        "fandom_b"       : label_b,
                        "year"           : year,
                        "population"     : len(population),
                        "population_frame": frame,
                        "interactions"   : int(sum(activity[basis].get(year, {}).values())),
                    }
                    row.update(compare_sets(set_a, set_b))

                    observed = row["overlap_count"]
                    size_a, size_b = row["A_count"], row["B_count"]

                    outside_pair = len(set(population) - (set_a | set_b))
                    row["channels_in_neither"] = outside_pair
                    row["baseline_available"] = bool(outside_pair and size_a and size_b)

                    if row["baseline_available"]:
                        row["expected_if_everyone_equally_likely"] = size_a * size_b / len(population)
                    else:
                        row["expected_if_everyone_equally_likely"] = None

                    if activity_null and row["baseline_available"]:
                        expected_activity, _, activity_draws = permutation_overlap_null(
                            set_a, set_b, population, rng, permutations)
                        row["expected_given_activity"] = (
                            expected_activity if activity_draws.size else None)
                    else:
                        row["expected_given_activity"] = None

                    null_overlaps = null_by_pair.get((first, second), np.zeros(0, dtype=np.int64))

                    if null_overlaps.size and size_a and size_b:
                        expected_fixed = float(null_overlaps.mean())
                        extreme = int((np.abs(null_overlaps - expected_fixed)
                                       >= abs(observed - expected_fixed)).sum())
                        row["expected_fixed_margins"] = expected_fixed
                        row["ratio_fixed_margins"] = (observed / expected_fixed) if expected_fixed else None
                        row["p_value"] = float((1 + extreme) / (permutations + 1))                      # cf. Phipson & Smyth (2010), "Permutation P-values Should Never Be Zero: Calculating Exact P-values When Permutations Are Randomly Drawn", https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.permutation_test.html
                    else:
                        row["expected_fixed_margins"] = None
                        row["ratio_fixed_margins"] = None
                        row["p_value"] = None

                    for name, expected in (("ratio_uniform", row["expected_if_everyone_equally_likely"]),
                                           ("ratio_given_activity", row["expected_given_activity"])):
                        if expected:
                            row[name] = observed / expected
                        else:
                            row[name] = None

                    if row["ratio_fixed_margins"] is None:
                        row["direction"] = None
                    elif row["ratio_fixed_margins"] > 1:
                        row["direction"] = "more than chance"
                    elif row["ratio_fixed_margins"] < 1:
                        row["direction"] = "less than chance"
                    else:
                        row["direction"] = "as expected"

                    low, high = bootstrap_jaccard_interval(
                        observed, size_a - observed, size_b - observed, len(population), rng,
                        draws=bootstraps)
                    row["jaccard_ci_low"], row["jaccard_ci_high"] = low, high

                    rows.append(row)

    progress.close()

    testable = [index for index, row in enumerate(rows) if row["p_value"] is not None]
    q_values = benjamini_hochberg([rows[index]["p_value"] for index in testable])
    for index, q_value in zip(testable, q_values):
        rows[index]["q_value"] = q_value
    for row in rows:
        row.setdefault("q_value", None)
        row["significant"] = bool(row["q_value"] is not None and row["q_value"] < SIGNIFICANCE_LEVEL)
        row["inference_status"] = ("fixed margins" if row["p_value"] is not None else "no baseline")
        row["activity_null"] = bool(activity_null)

    return rows


def save_overlap_table(rows, out_dir):

    os.makedirs(out_dir, exist_ok=True)

    json_path = os.path.join(out_dir, "overlap_over_time.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)

    csv_path = os.path.join(out_dir, "overlap_over_time.csv")
    if rows:
        columns = list(rows[0].keys())
        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(",".join(columns) + "\n")
            for row in rows:
                values = []
                for column in columns:
                    value = row[column]
                    values.append("" if value is None else str(value))
                handle.write(",".join(values) + "\n")

    return json_path, csv_path


def plot_overlap_over_time(rows, out_dir="output", basis="audience"):
    """
    Two panels per fandom pair
    """

    os.makedirs(out_dir, exist_ok=True)
    pairs = sorted({(row["fandom_a"], row["fandom_b"]) for row in rows if row["basis"] == basis})

    for label_a, label_b in pairs:
        selected = sorted([row for row in rows
                           if row["basis"] == basis
                           and row["fandom_a"] == label_a
                           and row["fandom_b"] == label_b],
                          key=lambda row: row["year"])
        if not selected:
            continue

        years = [row["year"] for row in selected]
        jaccard = [row["jaccard"] if row["jaccard"] is not None else float("nan") for row in selected]
        low = [row["jaccard_ci_low"] if row["jaccard_ci_low"] is not None else float("nan") for row in selected]
        high = [row["jaccard_ci_high"] if row["jaccard_ci_high"] is not None else float("nan") for row in selected]
        ratio = [row["ratio_fixed_margins"] if row["ratio_fixed_margins"] is not None else float("nan")
                 for row in selected]

        figure, (upper, lower) = plt.subplots(2, 1, sharex=True, figsize=(10, 7),
                                              gridspec_kw={"height_ratios": [1, 1]})

        upper.plot(years, jaccard, marker="o", color="steelblue", label="Jaccard index")
        upper.fill_between(years, low, high, color="steelblue", alpha=0.2, label="95% interval")
        upper.set_ylabel("Jaccard index")
        upper.set_title(f"Shared channels over time: {label_a} ↔ {label_b}  ({basis})")
        upper.legend(fontsize=8)

        for row in selected:
            if row["jaccard"] is not None:
                upper.annotate(f"n={row['A_count'] + row['B_count'] - row['overlap_count']}",
                               (row["year"], row["jaccard"]), textcoords="offset points",
                               xytext=(0, 7), ha="center", fontsize=7, color="dimgray")

        lower.axhline(1.0, color="gray", linestyle=":", linewidth=1)
        lower.plot(years, ratio, marker="o", color="darkorange",
                   label="observed / expected (both margins fixed)")

        marked = [(row["year"], row["ratio_fixed_margins"]) for row in selected
                  if row["significant"] and row["ratio_fixed_margins"] is not None]
        if marked:
            lower.scatter([year for year, _ in marked], [value for _, value in marked],
                          marker="*", s=140, color="black", zorder=3,
                          label=f"differs from chance (FDR < {SIGNIFICANCE_LEVEL})")

        lower.set_xlabel("Year")
        lower.set_ylabel("observed / expected\n(1 = chance, <1 = less than expected)")
        lower.legend(fontsize=8)
        lower.set_xticks(years)
        plt.setp(lower.get_xticklabels(), rotation=45, ha="right")

        plt.tight_layout()
        png_path = os.path.join(out_dir, platform.safe_filename(
            f"overlap_{label_a}__and__{label_b}_{basis}.png"))
        plt.savefig(png_path, dpi=150)
        plt.close()


def report_overlap_over_time(fandoms, out_dir="output", permutations=OVERLAP_PERMUTATIONS,
                             seed=OVERLAP_RANDOM_SEED, frame=POPULATION_FRAME,
                             bootstraps=OVERLAP_BOOTSTRAPS, activity_null=False):
    """
    Run the whole overlap analysis: compute, save the table, draw the figures.
    """

    print(f"Comparing fandom overlap per year ({permutations:,} permutations, "
          f"{bootstraps:,} bootstraps, seed {seed}, frame {frame}, "
          f"activity-weighted null {'on' if activity_null else 'off'}) ...")

    rows = analyse_overlap_over_time(fandoms, permutations=permutations, seed=seed,
                                     frame=frame, bootstraps=bootstraps,
                                     activity_null=activity_null)
    json_path, csv_path = save_overlap_table(rows, out_dir)
    print(f"Overlap table written: {json_path} and {csv_path}")

    print("  Results come from the fixed-margins null: every channel keeps its number of")
    print("  fandoms, each fandom keeps its size.")

    significant = [row for row in rows if row["significant"]]
    testable = sum(1 for row in rows if row["p_value"] is not None)
    untestable = sum(1 for row in rows if not row["baseline_available"] and row["A_count"] and row["B_count"])
    more = sum(1 for row in significant if row["direction"] == "more than chance")
    less = sum(1 for row in significant if row["direction"] == "less than chance")

    print(f"Years whose overlap differs from chance: {len(significant):,} of {testable:,} tested "
          f"(FDR < {SIGNIFICANCE_LEVEL}) - {more:,} with more overlap than expected, "
          f"{less:,} with less.")

    if untestable:
        print(f"No chance baseline for {untestable:,} {platform.plural(untestable, 'year')}: "
              f"the fandoms account for everyone active that year. Jaccard is still reported.")

    for basis in MEMBERSHIP_BASES:
        plot_overlap_over_time(rows, out_dir=out_dir, basis=basis)

    return rows


def plot_jaccard_over_time(fandoms, include_by_role=True, out_dir="output"):
    """
    Plot the development of the Jaccard index over time.
    """

    os.makedirs(out_dir, exist_ok=True)
    role_names = ["uploader", "commenter", "replier"]
    channel_sets = {}
    video_year = {}
    fandom_label_by_video = {}

    for fandom in fandoms:
        label = fandom["label"]
        channel_sets[label] = {}
        for vid, vmeta in fandom.get("videos", {}).items():
            fandom_label_by_video[vid] = label
            ts = vmeta.get("published_at")
            dt = parse_dt(ts) if ts else None
            if dt:
                video_year[vid] = dt.year

    def ensure_year(label, year):
        if year is None:
            return
        if year not in channel_sets[label]:
            channel_sets[label][year] = {
                "all"      : set(),
                "audience" : set(),
                "uploader" : set(),
                "commenter": set(),
                "replier"  : set(),
            }

    for fandom in fandoms:
        label = fandom["label"]
        for edge in fandom.get("edges", []):
            ch_id = edge.source_channel_id
            if edge.event_type == "uploader":
                yr = video_year.get(edge.target_video_id, None)
                ensure_year(label, yr)
                if yr is not None:
                    channel_sets[label][yr]["uploader"].add(ch_id)
                    channel_sets[label][yr]["all"].add(ch_id)
            elif edge.event_type == "commenter":
                dt = parse_dt(edge.comment_datetime) if edge.comment_datetime else None
                yr = dt.year if dt else None
                ensure_year(label, yr)
                if yr is not None:
                    channel_sets[label][yr]["commenter"].add(ch_id)
                    channel_sets[label][yr]["audience"].add(ch_id)
                    channel_sets[label][yr]["all"].add(ch_id)
            elif edge.event_type == "replier":
                dt = parse_dt(edge.reply_datetime) if edge.reply_datetime else None
                yr = dt.year if dt else None
                ensure_year(label, yr)
                if yr is not None:
                    channel_sets[label][yr]["replier"].add(ch_id)
                    channel_sets[label][yr]["audience"].add(ch_id)
                    channel_sets[label][yr]["all"].add(ch_id)

    labels = [group["label"] for group in fandoms]
    unique_pairs = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            unique_pairs.append((labels[i], labels[j]))

    all_years = sorted({yr for lab in channel_sets for yr in channel_sets[lab].keys()})
    details = {}

    series = {}

    for A, B in unique_pairs:
        if A not in channel_sets or B not in channel_sets:
            continue

        details[(A, B)] = {}
        series[(A, B)] = {"years": [], "all": [], "audience": [], "support": []}

        for yr in all_years:
            A_all = channel_sets[A].get(yr, {}).get("all", set())
            B_all = channel_sets[B].get(yr, {}).get("all", set())
            A_audience = channel_sets[A].get(yr, {}).get("audience", set())
            B_audience = channel_sets[B].get(yr, {}).get("audience", set())

            comparison_all = compare_sets(A_all, B_all)
            comparison_audience = compare_sets(A_audience, B_audience)

            series[(A, B)]["years"].append(yr)
            series[(A, B)]["all"].append(
                comparison_all["jaccard"] if comparison_all["jaccard"] is not None else float("nan"))
            series[(A, B)]["audience"].append(
                comparison_audience["jaccard"] if comparison_audience["jaccard"] is not None else float("nan"))
            series[(A, B)]["support"].append(len(A_all | B_all))

            entry = {
                "all"         : comparison_all,
                "audience"    : comparison_audience,
                "intersection": sorted(A_all & B_all),
                "A_only"      : sorted(A_all - B_all),
                "B_only"      : sorted(B_all - A_all),
            }

            if include_by_role:
                entry["by_role"] = {}
                for role in role_names:
                    A_r = channel_sets[A].get(yr, {}).get(role, set())
                    B_r = channel_sets[B].get(yr, {}).get(role, set())
                    role_entry = compare_sets(A_r, B_r)
                    role_entry.update({
                        "intersection": sorted(A_r & B_r),
                        "A_only": sorted(A_r - B_r),
                        "B_only": sorted(B_r - A_r),
                    })
                    entry["by_role"][role] = role_entry

            details[(A, B)][yr] = entry

    observed = [value for pair in series.values() for value in pair["all"] + pair["audience"]
                if value == value]
    shared_top = max(observed) if observed else 1.0
    shared_top = min(1.0, shared_top * 1.1) if shared_top > 0 else 1.0

    for (A, B), pair_series in series.items():
        if not pair_series["years"]:
            continue

        plt.figure()
        plt.plot(pair_series["years"], pair_series["all"], marker="o", label="all channels")
        plt.plot(pair_series["years"], pair_series["audience"], marker="s", linestyle="--",
                 label="audience only (commenters and repliers)")

        for year, value, support in zip(pair_series["years"], pair_series["all"], pair_series["support"]):
            if value == value and support:
                plt.annotate(f"n={support}", (year, value), textcoords="offset points",
                             xytext=(0, 6), ha="center", fontsize=7, color="dimgray")

        plt.xlabel("Year")
        plt.ylabel("Jaccard Index")
        plt.title(f"Jaccard over Time: {A} ↔ {B}")
        plt.xticks(pair_series["years"], rotation=45, ha="right")
        plt.ylim(0, shared_top)
        plt.legend(fontsize=8)
        plt.tight_layout()

        png_path = os.path.join(out_dir, platform.safe_filename(f"jaccard_{A}__and__{B}.png"))
        plt.savefig(png_path, dpi=150)
        plt.close()

        json_path = os.path.join(out_dir,
                                 platform.safe_filename(f"jaccard_{A}__and__{B}.details.json"))
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(details[(A, B)], handle, ensure_ascii=False, indent=2)

    return details


TUBECONNECT_MODES = ("overlaps", "interaction", "graph", "null", "plots", "all")

SUMMARY_PATH = "output/fandom_network.summary.json"
GEXF_PATH    = "output/fandom_network.gexf"


def load_and_cap(cap_basis=None):

    cap_basis = cap_basis or TUBECONNECT_CAP_BASIS
    fandoms = [load_fandom(spec, label=label, name=name)
               for spec, label, name in TUBECONNECTSETTINGS]

    start, end = get_overlap(fandoms, basis=cap_basis)
    if start and end:
        print(f"Applying {cap_basis}-based overlap window: {start.isoformat()} to {end.isoformat()}")
        cap_fandoms_to_overlap_time(fandoms, start, end, basis=cap_basis)
    elif cap_basis != "none":
        print("Could not compute a valid overlapping window; proceeding without capping.")

    return fandoms, (start, end, cap_basis)


def read_summary(path=SUMMARY_PATH):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def merge_summary(section, path=SUMMARY_PATH):

    merged = read_summary(path)
    merged.update(section)
    save_summary_json(merged, path)
    return merged


def window_section(overlap_time):
    start, end, basis = overlap_time
    if start and end:
        return {"overlap_window": {"basis": basis, "start_utc": start.isoformat(),
                                   "end_utc": end.isoformat(), "days": (end - start).days}}
    return {"overlap_window": {"basis": basis, "start_utc": None, "end_utc": None, "days": None}}


def tubeconnect(mode="all", cap_basis=None, permutations=OVERLAP_PERMUTATIONS,
                bootstraps=OVERLAP_BOOTSTRAPS, seed=OVERLAP_RANDOM_SEED,
                frame=POPULATION_FRAME, out_dir="output", confirm=True,
                activity_null=False):

    if mode not in TUBECONNECT_MODES:
        print(f"Unknown mode '{mode}'. Choose one of: {', '.join(TUBECONNECT_MODES)}.")
        return

    logo = """
┌───────────────────────────────────────────────────────────┐
│          ●───●───●                  ●───●───●             │
│         ╱    │    ╲   Welcome to   ╱    │    ╲            │
│      ●─●     ●     ●──────────────●     ●     ●─●         │
│         ╲    │    ╱  TubeConnect!  ╲    │    ╱            │
│          ●───●───●                  ●───●───●             │
└───────────────────────────────────────────────────────────┘
"""
    print(logo)
    print(f"Mode: {mode}")
    print("Settings:", TUBECONNECTSETTINGS)

    os.makedirs(out_dir, exist_ok=True)
    summary_path = os.path.join(out_dir, os.path.basename(SUMMARY_PATH))
    gexf_path    = os.path.join(out_dir, os.path.basename(GEXF_PATH))

    fandoms, overlap_time = load_and_cap(cap_basis)

    if mode == "all":
        summary = compute_summary(fandoms, overlap_time)
        graph = build_graph(fandoms)
        export_gexf(graph, gexf_path,
                    graph_label=" vs ".join(fandom["name"] for fandom in fandoms))
        print(f"GEXF exported: {gexf_path}.")
        save_summary_json(summary, summary_path)
        print(f"Summary written: {summary_path}")
        generate_plots(summary)
        plot_jaccard_over_time(fandoms, out_dir=out_dir)
        report_overlap_over_time(fandoms, out_dir=out_dir, permutations=permutations,
                                 seed=seed, frame=frame, bootstraps=bootstraps,
                                 activity_null=activity_null)
        return summary

    if mode == "overlaps":
        section = compute_pairwise_overlaps(fandoms)
        section.update(window_section(overlap_time))
        merge_summary(section, summary_path)
        print(f"Summary written: {summary_path}")
        print(f"  {'pair':<12}{'A':>9}{'B':>9}{'shared':>9}{'Jaccard':>10}{'overlap coef.':>15}")
        for entry in section.get("pairwise", []):
            first, second = entry["pair"]
            jaccard = entry["jaccard"]
            coefficient = entry["overlap_coefficient"]
            print(f"  {first + ' / ' + second:<12}{entry['A_count']:>9,}{entry['B_count']:>9,}"
                  f"{entry['overlap_count']:>9,}"
                  f"{('-' if jaccard is None else format(jaccard, '.5f')):>10}"
                  f"{('-' if coefficient is None else format(coefficient, '.5f')):>15}")
        return section

    if mode == "interaction":
        section = compute_cross_fandom_interactions(fandoms)
        section.update(window_section(overlap_time))
        merge_summary(section, summary_path)
        print(f"Summary written: {summary_path}")
        for label, values in section.get("cross_share", {}).items():
            print(f"  {label}: cross-share {values.get('cross_share', 0):.4f}")
        return section

    if mode == "graph":
        graph = build_graph(fandoms)
        export_gexf(graph, gexf_path,
                    graph_label=" vs ".join(fandom["name"] for fandom in fandoms))
        print(f"GEXF exported: {gexf_path}. "
              f"{graph.number_of_nodes():,} nodes, {graph.number_of_edges():,} edges.")
        return graph

    if mode == "plots":
        summary = read_summary(summary_path)
        if "interaction_matrix" not in summary:
            print(f"No interaction matrices in '{summary_path}'. Run 'tubeconnect "
                  f"interaction' first, or 'tubeconnect all'.")
            return None
        generate_plots(summary)
        plot_jaccard_over_time(fandoms, out_dir=out_dir)
        return summary

    if mode == "null":
        return report_overlap_over_time(fandoms, out_dir=out_dir, permutations=permutations,
                                        seed=seed, frame=frame, bootstraps=bootstraps,
                                        activity_null=activity_null)


if __name__ == "__main__":
    tubeconnect(sys.argv[1] if len(sys.argv) > 1 else "all")
    sys.exit()
