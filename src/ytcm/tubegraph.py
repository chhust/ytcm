""" TubeGraph contains a set of network analysis functions for the YouTube Comment Miner.
Some functions adapt example code and usage patterns from the

-- NetworkX (parts are heavily inspired), https://github.com/PacktPublishing/Network-Science-with-Python-and-NetworkX-Quick-Start-Guide,
-- Seaborn,
-- Matplotlib, and
-- tqdm (to a lesser extent) documentations.

I found inspiration about possible network analysis metrics in both the Gephi app and in a monograph about it:
Ken Cherven, "Mastering Gephi Network Visualization: Produce Advanced Network Graphs in Gephi and Gain Valuable Insights
Into Your Network Datasets", Birmingham and Mumbai: Packt open source, 2015.

Then, the following article provided a useful overview about key concepts with regards to the Digital Humanities:
Fotos Jannidis, "Netzwerke", in: "Digital Humanities. Eine Einführung", ed. by ibid., Hubertus Kohle, and Malte Rehbein,
Stuttgart: Metzler, 2017, pp. 147-161.

Finally, a lot of the code snippets are taken from or heavily influenced by Edward L. Platt, "Network Science with Python and NetworkX Quick Start Guide: Explore and Visualize Network Data"
"""

import gc
import heapq
import logging
import random
import time
import warnings
import matplotlib.lines   as mlines
import matplotlib.patches as mpatches
import matplotlib.pyplot  as plt
import networkx           as nx
import numpy              as np
import pandas             as pd
import seaborn            as sns

from collections                   import defaultdict, Counter
from matplotlib                    import colormaps
from networkx.algorithms.community import greedy_modularity_communities, louvain_communities
from scipy.sparse                  import csr_matrix
from tqdm                          import tqdm

from ytcm import repo as repo
from ytcm import tubeconnect as tc
from ytcm.config import GRAPH_BAND_CELLS
from ytcm.font_utils import configure_cjk_fonts
from ytcm.results import write_table

logger = logging.getLogger(__name__)

configure_cjk_fonts()


def channel_occurrence_stats():
    """Count how often a channel appears in various roles:
    as uploader, as commenter, as replier, in how many unique videos
    """

    stats = repo.channel_role_counts()

    data_list = []

    for channel_id, counts in stats.items():
        data_list.append({
            "channel_id"      : channel_id,
            "as_uploader"     : counts["as_uploader"],
            "as_commenter"    : counts["as_commenter"],
            "as_replier"      : counts["as_replier"],
            "total_activity"  : counts["as_uploader"] + counts["as_commenter"] + counts["as_replier"],
            "videos_active_in": len(counts["unique_videos"])
        })

    df = pd.DataFrame(data_list, columns=["channel_id", "as_uploader", "as_commenter",
                                          "as_replier", "total_activity", "videos_active_in"])

    return df.sort_values(by="total_activity", ascending=False).reset_index(drop=True)


def plot_channel_role_proportions(df, roles=("as_uploader", "as_commenter", "as_replier"), top_n=None, normalize=False,
                                  figsize=(12,6), palette=None):
    """Bar chart of role counts per channel with optional filtering/normalization."""

    cols = [c for c in roles if c in df.columns]
    use = df.copy()

    if normalize:
        use[cols] = use[cols].div(use[cols].sum(axis=1).replace(0, 1), axis=0)

    if top_n:
        use = use.sort_values(by=cols, ascending=False).head(top_n)

    df_long = use.melt(id_vars="channel_id", value_vars=cols, var_name="role", value_name="count")
    plt.figure(figsize=figsize)
    sns.barplot(data=df_long, x="channel_id", y="count", hue="role", palette=palette)
    plt.title("Channel Roles")
    plt.xticks(rotation=45, ha="right")

    plt.tight_layout()
    plt.show()


def build_reply_network(include_self=False, min_weight=1):
    """Construct a directed reply graph with options to include self-replies and drop light edges."""

    graph = nx.DiGraph()

    for parent, child in tqdm(repo.reply_author_pairs(include_self),
                              desc="Reading reply pairs"):
        if graph.has_edge(child, parent):
            graph[child][parent]["weight"] += 1
        else:
            graph.add_edge(child, parent, weight=1)

    to_remove = [(u, v) for u, v, d in graph.edges(data=True) if d.get("weight", 0) < min_weight]
    graph.remove_edges_from(to_remove)

    graph.graph["filters"] = {"self_replies": "included" if include_self else "excluded",
                          "min_weight": min_weight}
    graph.graph["construction"] = "observed reply edges"

    try:
        nx.write_gexf(graph, "directed_reply_graph.gexf")
        print("    wrote directed_reply_graph.gexf")
    except OSError as e:
        logger.error(f"Could not write directed_reply_graph.gexf: {e}.")

    return graph


def plot_reply_network(graph, top_n=30):
    """Visualize top reply interactions as a directed graph."""

    degrees = dict(graph.degree())                                      # Choose the most important nodes
    top_nodes = sorted(degrees, key=degrees.get, reverse=True)[:top_n]
    subG = graph.subgraph(top_nodes).copy()

    subG.remove_nodes_from(list(nx.isolates(subG)))                 # Remove isolated nodes

    pos = nx.spring_layout(subG, k=0.25, seed=42)
    node_sizes = [300 + 20 * subG.degree(n) for n in subG.nodes()]  # Node sizes should be proportional with degrees
    edge_weights = [subG[u][v]['weight'] for u, v in subG.edges()]

    plt.figure(figsize=(14, 14))
    nx.draw_networkx_edges(subG, pos, edge_color="gray", arrows=True, width=edge_weights, alpha=0.6)
    nx.draw_networkx_nodes(subG, pos, node_size=node_sizes, node_color="skyblue", alpha=0.9)
    nx.draw_networkx_labels(subG, pos, font_size=8)

    label = f"Directed Reply Network of YouTube Channels\n(top {top_n} by interaction degree)"
    plt.title(label, fontsize=16)
    plt.axis("off")

    legend_elements = [
        mpatches.Patch(color="skyblue", label="YouTube Channel (Node)"),
        mpatches.Patch(color="gray", label="Reply from one channel to another (Edge)"),
    ]
    plt.legend(handles=legend_elements, loc="upper left", fontsize=10)

    plt.tight_layout()
    plt.show()


def _share_of(part, whole):

    return f"{part / whole * 100:.1f}%" if whole else "0.0%"


def _select_participation(roles=("uploader","commenter","replier"), exclude_uploader=True,
                         min_videos_per_channel=2, max_participants_per_video=None,
                         top_channels=None, report=True, label="participation"):

    uploaders, by_video = repo.participation_by_video(roles)
    participation = defaultdict(set)
    videos_seen = videos_kept = 0
    parts_seen = parts_kept = 0

    for video_id, members in tqdm(by_video.items(), desc=f"Indexing {label}"):
        members = set(members)
        if exclude_uploader and "uploader" in roles:
            members.discard(uploaders.get(video_id))
        videos_seen += 1
        parts_seen += len(members)
        if max_participants_per_video and len(members) > max_participants_per_video:
            continue
        videos_kept += 1
        parts_kept += len(members)
        for channel in members:
            participation[channel].add(video_id)

    counts = {channel: len(videos) for channel, videos in participation.items()}
    eligible = [channel for channel, count
                in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
                if count >= min_videos_per_channel]
    keep = eligible[:top_channels] if top_channels else eligible

    if report:
        if max_participants_per_video:
            print(f"    videos {videos_seen:,} -> {videos_kept:,} at most "
                  f"{max_participants_per_video:,} participants: "
                  f"{videos_seen - videos_kept:,} "
                  f"{'video' if videos_seen - videos_kept == 1 else 'videos'} dropped, "
                  f"and with them "
                  f"{_share_of(parts_seen - parts_kept, parts_seen)} of all "
                  f"{parts_seen:,} participations")
        else:
            print(f"    videos {videos_seen:,}, participations {parts_seen:,}, "
                  f"no cap on participants per video")
        print(f"    channels {len(counts):,} -> {len(eligible):,} on at least "
              f"{min_videos_per_channel} videos: "
              f"{_share_of(len(counts) - len(eligible), len(counts))} dropped")
        if top_channels:
            print(f"    channels {len(eligible):,} -> {len(keep):,} at top_channels "
                  f"{top_channels:,}: "
                  f"{_share_of(len(eligible) - len(keep), len(eligible))} dropped")

    return keep, participation


def _participation_matrix(keep, participation):

    channel_index = {channel: i for i, channel in enumerate(keep)}
    videos = sorted({video for channel in keep for video in participation[channel]})
    video_index = {video: i for i, video in enumerate(videos)}

    rows, cols = [], []
    for channel in keep:
        row = channel_index[channel]
        for video in participation[channel]:
            rows.append(row)
            cols.append(video_index[video])

    return csr_matrix((np.ones(len(rows), dtype=np.int32), (rows, cols)),
                      shape=(len(keep), len(videos)))


def _band_edges(incidence, block_rows=None, cell_budget=None):

    rows = incidence.shape[0]
    if block_rows:
        return list(range(0, rows, block_rows)) + [rows]

    per_video = np.asarray(incidence.sum(axis=0)).ravel()
    reach = incidence @ per_video

    budget = cell_budget or GRAPH_BAND_CELLS
    cuts, running = [0], 0.0
    for i in range(rows):
        if running and running + reach[i] > budget:
            cuts.append(i)
            running = 0.0
        running += reach[i]
    cuts.append(rows)
    return cuts


def _cooccurrence_bands(incidence, block_rows=None, cell_budget=None):

    transposed = incidence.T.tocsc()
    cuts = _band_edges(incidence, block_rows, cell_budget)
    for start, stop in zip(cuts, cuts[1:]):
        if stop > start:
            yield start, (incidence[start:stop] @ transposed).tocsr()


def _projected_edges(keep, participation, min_weight=2, top_k_per_node=50,
                     max_edges=None, block_rows=None):

    incidence = _participation_matrix(keep, participation)

    edges = []
    pairs_seen = 0
    pairs_at_weight = 0
    for start, band in _cooccurrence_bands(incidence, block_rows):
        indptr, indices, counts = band.indptr, band.indices, band.data
        for local in range(band.shape[0]):
            i = start + local
            neighbors = []
            for position in range(indptr[local], indptr[local + 1]):
                j = int(indices[position])
                if j == i:
                    continue
                if j > i:
                    pairs_seen += 1
                if counts[position] >= min_weight:
                    neighbors.append((int(counts[position]), j))
                    if j > i:
                        pairs_at_weight += 1
            if not neighbors:
                continue
            k = min(top_k_per_node, len(neighbors))
            for weight, j in heapq.nlargest(k, neighbors, key=lambda pair: pair[0]):
                if i < j:
                    edges.append((i, j, weight))

    after_top_k = len(edges)
    if max_edges and len(edges) > max_edges:
        edges.sort(key=lambda edge: edge[2], reverse=True)
        edges = edges[:max_edges]

    return edges, pairs_seen, pairs_at_weight, after_top_k


def build_interaction_graph(roles=("uploader","commenter","replier"), exclude_uploader=True,
                            top_channels=None, min_videos_per_channel=2, max_participants_per_video=None,
                            min_weight=2, top_k_per_node=50, max_edges=None, block_rows=None, report=True):

    if report:
        print(f"  interaction graph: roles {'+'.join(roles)}, "
              f"uploader {'excluded' if exclude_uploader else 'included'}")

    keep, participation = _select_participation(
        roles, exclude_uploader, min_videos_per_channel, max_participants_per_video,
        top_channels, report, label="participation (interaction)")

    if not keep:
        if report:
            print("    nothing left to build a graph from")
        return nx.Graph()

    edges, pairs_seen, pairs_at_weight, after_top_k = _projected_edges(
        keep, participation, min_weight, top_k_per_node, max_edges, block_rows)

    if report:
        print(f"    co-occurring pairs {pairs_seen:,} -> {pairs_at_weight:,} at weight "
              f">= {min_weight}: {_share_of(pairs_seen - pairs_at_weight, pairs_seen)} dropped")
        print(f"    pairs {pairs_at_weight:,} -> {after_top_k:,} after top {top_k_per_node} "
              f"per node: {_share_of(pairs_at_weight - after_top_k, pairs_at_weight)} dropped")
        if max_edges:
            print(f"    edges {after_top_k:,} -> {len(edges):,} at max_edges {max_edges:,}: "
                  f"{_share_of(after_top_k - len(edges), after_top_k)} dropped")

    graph = nx.Graph()
    graph.graph["filters"] = {
        "roles": "+".join(roles),
        "uploader": "excluded" if exclude_uploader else "included",
        "min_videos_per_channel": min_videos_per_channel,
        "min_weight": min_weight,
        "top_k_per_node": top_k_per_node,
        "top_channels": top_channels,
        "max_participants_per_video": max_participants_per_video,
        "max_edges": max_edges,
    }
    graph.graph["construction"] = "co-occurrence projection"
    channel_of = np.array(keep)
    for i, j, weight in edges:
        graph.add_edge(channel_of[i], channel_of[j], weight=weight)

    del edges
    gc.collect()
    return graph


def channel_video_participation_matrix(roles=("uploader", "commenter", "replier"), dtype=bool,
                                       top_channels=None, top_videos=None):

    participation = defaultdict(set)

    uploaders, by_video = repo.participation_by_video(roles)
    for video_id, parts in tqdm(by_video.items(),
                                desc="Scanning videos for channel video participation stats:"):
        for ch in parts:
            participation[ch].add(video_id)

    all_channels = list(participation.keys())
    all_videos = list(uploaders.keys())

    if top_channels:
        counts = {ch: len(vids) for ch, vids in participation.items()}
        all_channels = [ch for ch, _ in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:top_channels]]

    if top_videos:                                                  # Rank videos by number of participating channels
        vcount = Counter(v for vids in participation.values() for v in vids)
        all_videos = [v for v, _ in vcount.most_common(top_videos)]

    matrix = pd.DataFrame(False if dtype is bool else 0, index=all_channels, columns=all_videos)

    for ch in all_channels:
        vids = list(participation.get(ch, set()) & set(all_videos))
        if vids:
            matrix.loc[ch, vids] = True if dtype is bool else 1

    if dtype is bool and matrix.dtypes.nunique() == 1:
        matrix = matrix.astype(bool)

    return matrix


def plot_channel_clustering_heatmap(matrix, max_channels=400, cluster_max=None, fast=True, both=True, cmap="viridis",
                                    heatmap_figsize=(10, 8), clustermap_figsize=(12, 10), *,
                                    auto=True,                 # if True, choose sensible defaults based on data stats
                                    similarity="dot",          # "dot" | "cosine" | "corr"
                                    transform="none",          # "none" | "log1p"
                                    clip_vmax=None             # None | "p99" | "p995"
):
    """
    Plot a channel co-occurrence heatmap and a clustermap with hierarchical clustering,
    applying an upper limit to the number of channels for plotting to manage performance.
    This code is inspired from lots of examples on the web, mostly the seaborn documentation.
    I hope it all fits together!!
    """

    def auto_settings(profiles):
        """
        Derive simple heuristics from sparsity and skewness.
        """

        n_rows, n_cols = profiles.shape
        nnz = (profiles.values != 0).sum()                             # sparsity of the raw matrix (fraction of non-zeros)
        sparsity = nnz / (n_rows * n_cols) if n_rows * n_cols > 0 else 0.0
        row_sum = pd.Series(profiles.sum(axis=1))                      # skewness of row activity (heavy tails suggest log1p)
        skew = float(row_sum.skew()) if len(row_sum) else 0.0

        s = {}
        varying = float((np.std(profiles.values.astype(float), axis=1) > 0).mean()) if n_rows else 0.0

        s["transform"] = "log1p" if skew > 1.0 else "none"
        s["similarity"] = "corr" if (sparsity >= 0.10 and varying >= 0.5) else "cosine"
        s["clip_vmax"] = "p99" if sparsity < 0.10 else "p995"

        return s, {"sparsity": sparsity, "skew": skew}

    def apply_transform(profiles, how):
        """
        Apply a monotone transform to stabilize heavy tails.
        """

        if how == "log1p":
            return np.log1p(profiles)
        return profiles

    def build_similarity(profiles, how):
        """
        Compute channel-by-channel similarity.
        """

        if how == "dot":
            counted = profiles.astype(float)
            scores = counted.dot(counted.T)
        elif how == "cosine":
            profile_values = profiles.values.astype(float)
            denom = np.linalg.norm(profile_values, axis=1, keepdims=True)            # L2-normalize rows (channels)
            denom[denom == 0] = 1.0
            profile_values = profile_values / denom
            scores = pd.DataFrame(profile_values @ profile_values.T, index=profiles.index, columns=profiles.index)
        elif how == "corr":
            profile_values = profiles.values.astype(float)                                  # Pearson correlation between channel profiles
            if profile_values.shape[0] == 0:
                scores = pd.DataFrame(profile_values, index=profiles.index, columns=profiles.index)
            else:
                flat = np.std(profile_values, axis=1) == 0
                with np.errstate(invalid="ignore", divide="ignore"):
                    correlations = np.corrcoef(profile_values)
                undefined = int(np.isnan(correlations).sum())
                correlations = np.nan_to_num(correlations, nan=0.0, posinf=0.0, neginf=0.0)
                if undefined:
                    print(f"    correlation undefined for {undefined:,} of {correlations.size:,} cells "
                          f"({undefined / correlations.size * 100:.0f}%): {int(flat.sum()):,} of "
                          f"{len(flat):,} channels appear on the same videos every time. "
                          f"Shown as 0.")
                scores = pd.DataFrame(correlations, index=profiles.index, columns=profiles.index)
        else:
            raise ValueError(f"Unknown similarity '{how}'")
        if len(scores) > 0:
            scores = scores.copy()
            values = scores.to_numpy(copy=True)
            np.fill_diagonal(values, 0.0)
            scores = pd.DataFrame(values, index=scores.index, columns=scores.columns)
        return scores

    def clip_upper(scores, mode):
        """
        Return a vmax value from upper percentile to tame outliers (None = no clipping).
        """

        if mode is None or scores.size == 0:
            return None
        vals = scores.values
        vals = vals[np.isfinite(vals)]
        vals = vals[vals > 0]                                           # ignore zeros for percentile of positive mass
        if vals.size == 0:
            return None
        p = 99.0 if mode == "p99" else 99.5
        return float(np.percentile(vals, p))

    if matrix.shape[0] == 0 or matrix.shape[1] == 0:
        return

    if auto:
        auto_s, stats = auto_settings(matrix)
        if transform == "none":                                         # only override when user left defaults
            transform = auto_s["transform"]
        if similarity == "dot":
            similarity = auto_s["similarity"]
        if clip_vmax is None:
            clip_vmax = auto_s["clip_vmax"]
        skew_text = ("undefined" if np.isnan(stats["skew"])
                     else f"≈ {stats['skew']:.2f}")
        print(f"[auto] sparsity ≈ {stats['sparsity']:.3f}, skew {skew_text} → "
              f"transform={transform}, similarity={similarity}, clip_vmax={clip_vmax}")

    if cluster_max is None:
        cluster_max = max_channels

    print("Calculating co-occurrence matrix. This may take a while.")
    num_channels = matrix.shape[0]
    print(f"Co-occurrence matrix has {num_channels:,} channels.")

    if num_channels > max_channels and fast:            # fast mode: reduce to top N channels for heatmap
        top_indices = matrix.sum(axis=1).nlargest(max_channels).index
        matrix = matrix.loc[top_indices]
        num_channels = matrix.shape[0]
        print(f"Too large to plot whole; the heatmap uses the {num_channels:,} most "
              f"active channels.")

    matrix_t = apply_transform(matrix, transform)
    similarity_mat = build_similarity(matrix_t, similarity)

    vmax = clip_upper(similarity_mat, clip_vmax)
    vmin = 0

    print("Plotting co-occurrence heatmap ...")
    plt.figure(figsize=heatmap_figsize)
    sns.heatmap(similarity_mat, cmap=cmap, vmin=vmin, vmax=vmax)
    plt.title("Channel Co-Occurrence Heatmap")
    plt.tight_layout()
    plt.show()

    if both:                                                        # clustermap/dendrogram
        print("Plotting clustermap (hierarchical clustering of channels) ...")
        try:
            if similarity_mat.shape[0] > cluster_max:               # too large? reduce data
                subset_size = cluster_max
                print(f"{similarity_mat.shape[0]:,} channels is a lot for a dendrogram; "
                      f"using the top {subset_size:,} by co-occurrence.")
                # Select top channels by total similarity to others (most connected channels)
                top_cluster_indices = similarity_mat.sum(axis=1).nlargest(subset_size).index
                similarity_subset = similarity_mat.loc[top_cluster_indices, top_cluster_indices]
            else:
                similarity_subset = similarity_mat  # use full similarity if it's within the limit

            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*looks suspiciously like an "
                                                          "uncondensed distance matrix")
                warnings.filterwarnings("ignore", message="Clustering large matrix with scipy")
                cg = sns.clustermap(
                    similarity_subset, cmap=cmap, figsize=clustermap_figsize,
                    vmin=vmin, vmax=vmax
                )       # build map
            cg.fig.suptitle("Channel Co-Occurrence Heatmap (Hierarchical Clustering)")
            plt.show()
        except Exception as e:
            print(f"Clustermap generation failed: {e}")
            print("Skipping clustermap. Try a smaller 'cluster_max' or increase the recursion limit.")


def top_connected_channels(graph, top_n=10):
    """
    Return the top_n channels with the highest degree in the graph.
    """

    degrees = dict(graph.degree())
    top = sorted(degrees.items(), key=lambda x: x[1], reverse=True)[:top_n]

    return pd.DataFrame(top, columns=["channel_id", "degree"])


def plot_channel_degree_distribution(graph, cap_height=100, bin_count=30, x_min=None, x_max=None, figsize=(12,6),
                                     capped_color="tomato", normal_color="steelblue"):
    """Plot degree distribution with clipping, bin control and some useless eye candy."""

    degrees = [degree for _, degree in graph.degree()]
    total_nodes = len(degrees)
    zero_degree_count = sum(1 for degree in degrees if degree == 0)
    non_zero_degrees = [degree for degree in degrees if degree > 0]
    non_zero_count = len(non_zero_degrees)

    if non_zero_count == 0:
        print("No nodes with degree > 0 in the graph.")
        return

    zero_percent = zero_degree_count / total_nodes * 100
    non_zero_percent = non_zero_count / total_nodes * 100

    min_degree = max(1, min(non_zero_degrees))
    max_degree = max(non_zero_degrees)

    x_min = x_min if x_min is not None else min_degree
    x_max = x_max if x_max is not None else max_degree
    if x_min <= 0:
        x_min = min_degree
    if x_max <= x_min:
        x_max = max_degree

    filtered_degrees = [degree for degree in non_zero_degrees if x_min <= degree <= x_max]
    if not filtered_degrees:
        print(f"No nodes with degree >0 in range {x_min}–{x_max}.")
        return

    span = x_max - x_min
    if span < 1:
        bin_edges = np.array([x_min - 0.5, x_max + 0.5])
    else:
        bin_edges = np.unique(np.linspace(x_min, x_max, min(bin_count, int(span)) + 1))
        if len(bin_edges) < 2:
            bin_edges = np.array([x_min - 0.5, x_max + 0.5])
    bins = pd.cut(filtered_degrees, bins=bin_edges, right=True, include_lowest=True)
    bucket_counts = bins.value_counts().sort_index()

    fig, ax = plt.subplots(figsize=figsize)
    outliers = {}
    for i, (bucket, count) in enumerate(bucket_counts.items()):
        label = f"{int(bucket.left)}–{int(bucket.right)}"
        if count > cap_height:
            ax.bar(label, cap_height, color=capped_color)
            ax.plot(i, cap_height, marker="^")
            ax.text(i, cap_height + 1, str(count), ha="center", va="bottom", fontsize=8)
            outliers[label] = count
        else:
            ax.bar(label, count, color=normal_color)
            ax.text(i, count + 0.5, str(count), ha="center", va="bottom", fontsize=8)

    ax.set_title(f"Degree Distribution of Channel Network\n(Max. Bins: {bin_count}, Range: {x_min}–{x_max}, Cap: {cap_height})")
    ax.set_xlabel("Node Degree")
    ax.set_ylabel("Number of Nodes")
    plt.xticks(rotation=45, ha="right")

    legend_lines = [
        f"{zero_degree_count:,} nodes with degree 0 ({zero_percent:.2f} %)",
        f"{non_zero_count:,} nodes with degree ≥1 ({non_zero_percent:.2f} %)",
        ""
    ]

    if outliers:
        legend_lines.append("Absolute Counts for Clipped Bins:")
        for label, count in outliers.items():
            legend_lines.append(f"  - {label}: {count:,} nodes")

    text_legend = mlines.Line2D([], [], color="white", label="\n".join(legend_lines))
    capped_patch = mpatches.Patch(color=capped_color, label="Capped Bins")
    normal_patch = mpatches.Patch(color=normal_color, label="Uncapped Bins")
    ax.legend(handles=[normal_patch, capped_patch, text_legend], loc="upper right", fontsize=9)

    plt.tight_layout()
    plt.show()


COMMUNITY_METHODS = ("louvain", "greedy")


def _detect_communities(graph, method="louvain", seed=42):
    """
    Cf. https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.community.louvain.louvain_communities.html, and Blondel, V. D., Guillaume, J.-L.,
      Lambiotte, R. & Lefebvre, E. (2008), "Fast unfolding of communities in large networks", 
      in: Journal of Statistical Mechanics, P10008.
      The alternative is Clauset, A., Newman, M. E. J. & Moore, C. (2004), "Finding community structure in
      very large networks", in: Physical Review E 70, 066111.
    """

    if method not in COMMUNITY_METHODS:
        raise ValueError(f"Unknown method '{method}'. Choose one of: "
                         f"{', '.join(COMMUNITY_METHODS)}.")
    if graph.number_of_edges() == 0:
        return [{node} for node in graph]
    if method == "greedy":
        return list(greedy_modularity_communities(graph, weight="weight"))
    return louvain_communities(graph, weight="weight", seed=seed)


def community_structure(roles=("uploader", "commenter", "replier"), exclude_uploader=True,
                        min_videos_per_channel=2, max_participants_per_video=None,
                        top_channels=None, min_weight=2, top_k_per_node=50, max_edges=None,
                        block_rows=None, method="louvain", seed=42, draws=20,
                        budget_seconds=600.0, report=True):

    keep, participation = _select_participation(
        roles, exclude_uploader, min_videos_per_channel, max_participants_per_video,
        top_channels, report, label="participation (communities)")

    empty = pd.DataFrame(columns=["measure", "value", "note"])
    if not keep:
        if report:
            print("    no channels left to find communities in")
        return empty, pd.DataFrame(columns=["channel_id", "community"])

    edges, _, _, _ = _projected_edges(keep, participation, min_weight, top_k_per_node,
                                      max_edges, block_rows)
    graph = nx.Graph()
    graph.graph["filters"] = {
        "roles": "+".join(roles),
        "uploader": "excluded" if exclude_uploader else "included",
        "min_videos_per_channel": min_videos_per_channel,
        "min_weight": min_weight,
        "top_k_per_node": top_k_per_node,
    }
    graph.graph["construction"] = "co-occurrence projection"
    channel_of = np.array(keep)
    for i, j, weight in edges:
        graph.add_edge(channel_of[i], channel_of[j], weight=weight)

    started = time.perf_counter()
    communities = _detect_communities(graph, method, seed)
    detection_seconds = time.perf_counter() - started
    observed = (nx.community.modularity(graph, communities, weight="weight")
                if graph.number_of_edges() else 0.0)
    sizes = sorted((len(part) for part in communities), reverse=True)

    membership = pd.DataFrame(
        [{"channel_id": channel, "community": index}
         for index, part in enumerate(communities) for channel in sorted(part)])

    rows = []
    _measure(rows, "nodes", graph.number_of_nodes(), _filters_note(graph))
    _measure(rows, "edges", graph.number_of_edges())
    _measure(rows, "method", method, f"seed {seed}, {detection_seconds:.1f}s")
    _measure(rows, "communities", len(communities))
    _measure(rows, "largest community", sizes[0] if sizes else 0,
             f"{sizes[0] / len(keep) * 100:.1f}% of channels" if sizes else "")
    _measure(rows, "median community size", int(np.median(sizes)) if sizes else 0)
    _measure(rows, "singleton communities", sum(1 for size in sizes if size == 1))

    burn_in, thin = tc.schedule_for(len(keep))
    if draws:
        per_draw = detection_seconds + max(0.5, detection_seconds)
        projected = (burn_in + draws * thin) * 3e-6 + draws * per_draw
        if projected > budget_seconds:
            _measure(rows, "modularity", observed,
                     f"REPORTED WITHOUT A NULL: the null projects {projected / 60:,.0f} min "
                     f"against a budget of {budget_seconds / 60:,.0f} min. This number is no "
                     f"evidence of anything on its own: raise budget_seconds or cut the graph "
                     f"down with min_weight.")
            draws = 0
        else:
            null = _modularity_null(keep, participation, min_weight, top_k_per_node,
                                    max_edges, block_rows, method, seed, draws,
                                    burn_in, thin, report)
            usable = len(null)
            spread = float(np.std(null)) if usable > 1 else 0.0
            middle = float(np.mean(null)) if usable else float("nan")
            null_note = (f"null {middle:.4f} +/- {spread:.4f} over {usable} draws" if usable
                   else f"no null: none of the {draws} shuffles produced a single edge at "
                        f"min_weight {min_weight}, so there is nothing to compare against")
            _measure(rows, "modularity", observed,
                     null_note
                     + (f"; top_k_per_node={top_k_per_node} raises both, see the note below"
                        if top_k_per_node and top_k_per_node < graph.number_of_nodes() else ""))
            _measure(rows, "null mean modularity", middle,
                     "curveball, both bipartite margins fixed: every channel keeps its "
                     "number of videos, every video keeps its number of participants")
            _measure(rows, "null spread", spread,
                     "one chain, so the samples are correlated and this understates")
            _measure(rows, "ratio to chance", observed / middle if middle else float("nan"),
                     "top_k_per_node raises the measured value and the null together; "
                     "compare the pair, not the value"
                     if top_k_per_node and top_k_per_node < graph.number_of_nodes() else "")
            _measure(rows, "draws at or above observed",
                     int(sum(1 for value in null if value >= observed)),
                     f"of {usable:,} usable draws of {draws:,} asked for; burn-in "
                     f"{burn_in:,} trades, one draw every {thin:,}")
    if not draws:
        if not any(row["measure"] == "modularity" for row in rows):
            _measure(rows, "modularity", observed,
                     "REPORTED WITHOUT A NULL: draws=0 was asked for. A modularity on a "
                     "clique projection is high (whatever the data does), this does not mean much.")

    table = pd.DataFrame(rows)
    if report:
        _print_structure(rows, graph)
    return table, membership


def _modularity_null(keep, participation, min_weight, top_k_per_node, max_edges,
                     block_rows, method, seed, draws, burn_in, thin, report):
    """
    Cf. Strona et al. (2014), see the curveball implementation in TubeConnect.py. Curveball here randomizes the channel–video matrix. Each channel keeps its number of videos, each video its number of participating channels. Projection, edge filtering, and community detection are rerun for each sample. Samples that do not yield any projected edges are left out.
    """

    rng = np.random.default_rng(seed)
    rows = [set(participation[channel]) for channel in keep]
    size = len(rows)
    picker = random.Random(seed)

    def bit_choice(available, count):
        return picker.sample(available, count)

    def run(steps):
        for first, second in tc._trade_stream(rng, size, steps):
            if first != second:
                tc.curveball_trade(rows, int(first), int(second), bit_choice)

    if report:
        print(f"    modularity null: {size:,} channels, burn-in {burn_in:,} trades, "
              f"one draw every {thin:,}, {draws} draws, both bipartite margins fixed")
    run(burn_in)

    values = []
    for _ in tqdm(range(draws), desc="Modularity null", unit="draw"):
        run(thin)
        shuffled = {channel: rows[index] for index, channel in enumerate(keep)}
        edges, _, _, _ = _projected_edges(keep, shuffled, min_weight, top_k_per_node,
                                          max_edges, block_rows)
        graph = nx.Graph()
        channel_of = np.array(keep)
        for i, j, weight in edges:
            graph.add_edge(channel_of[i], channel_of[j], weight=weight)
        if graph.number_of_edges() == 0:
            continue
        values.append(nx.community.modularity(
            graph, _detect_communities(graph, method, seed), weight="weight"))

    return values


def _measure(rows, name, value, note=""):
    if isinstance(value, float) and value != value:
        value = "undefined"
    rows.append({"measure": name, "value": value, "note": note})


def _filters_note(graph):
    filters = graph.graph.get("filters") or {}
    if not filters:
        return "no filters recorded"
    return ", ".join(f"{name}={value}" for name, value in filters.items()
                     if value is not None)


def _affordable_path_measures(graph, budget_seconds=30.0, sample=8):

    nodes = list(graph)
    if len(nodes) < 2:
        return 0.0, True

    picked = nodes[:min(sample, len(nodes))]
    started = time.perf_counter()
    for node in picked:
        dict(nx.single_source_shortest_path_length(graph, node))
    elapsed = time.perf_counter() - started

    projected = elapsed / len(picked) * len(nodes)
    return projected, projected <= budget_seconds


def graph_structure(graph, expensive=False, budget_seconds=30.0, report=True):

    rows = []
    node_count = graph.number_of_nodes()
    edge_count = graph.number_of_edges()
    projection = graph.graph.get("construction") == "co-occurrence projection"
    clique = ("high by construction") if projection else ""
    mixing = ("shaped by construction") if projection else ""

    _measure(rows, "nodes", node_count, _filters_note(graph))
    _measure(rows, "edges", edge_count)

    if node_count == 0:
        _measure(rows, "components", 0, "empty graph")
        if report:
            _print_structure(rows, graph)
        return pd.DataFrame(rows)

    _measure(rows, "density", nx.density(graph))

    components = sorted((len(part) for part in nx.connected_components(graph)), reverse=True)
    _measure(rows, "components", len(components))
    _measure(rows, "largest component", components[0],
             f"{components[0] / node_count * 100:.1f}% of nodes")
    _measure(rows, "isolated nodes", sum(1 for size in components if size == 1))
    _measure(rows, "median component size", int(np.median(components)))

    _measure(rows, "average clustering", nx.average_clustering(graph), clique)
    _measure(rows, "transitivity", nx.transitivity(graph), clique)

    try:
        assortativity = nx.degree_assortativity_coefficient(graph)
    except Exception:
        assortativity = float("nan")
    _measure(rows, "degree assortativity", assortativity,
             "undefined: every node has the same degree"
             if assortativity != assortativity else mixing)

    bare = nx.Graph(graph)
    bare.remove_edges_from(nx.selfloop_edges(bare))
    cores = nx.core_number(bare)
    highest = max(cores.values()) if cores else 0
    _measure(rows, "max k-core", highest,
             f"{sum(1 for value in cores.values() if value == highest):,} nodes in it")

    rank = nx.pagerank(graph, weight="weight") if edge_count else {}
    if rank:
        best = max(rank, key=rank.get)
        _measure(rows, "highest PageRank", rank[best], f"channel {best}")

    if expensive:
        largest = graph.subgraph(max(nx.connected_components(graph), key=len)).copy()
        projected, affordable = _affordable_path_measures(largest, budget_seconds)
        if affordable:
            _measure(rows, "diameter", nx.diameter(largest),
                     "largest component only; the diameter of a split graph is infinite")
            _measure(rows, "average shortest path", nx.average_shortest_path_length(largest),
                     "largest component only")
        else:
            _measure(rows, "diameter", None,
                     f"not computed: projected {projected:,.0f} s against a budget of "
                     f"{budget_seconds:,.0f} s, measured on this graph")
            _measure(rows, "average shortest path", None, "not computed, same projection")

    table = pd.DataFrame(rows)
    if report:
        _print_structure(rows, graph)
    return table


def directed_structure(graph, report=True):

    rows = []
    node_count = graph.number_of_nodes()
    edge_count = graph.number_of_edges()

    _measure(rows, "nodes", node_count, _filters_note(graph))
    _measure(rows, "edges", edge_count)

    if node_count == 0:
        _measure(rows, "weakly connected components", 0, "empty graph")
        if report:
            _print_structure(rows, graph)
        return pd.DataFrame(rows)

    _measure(rows, "density", nx.density(graph))

    weak = sorted((len(part) for part in nx.weakly_connected_components(graph)), reverse=True)
    strong = sorted((len(part) for part in nx.strongly_connected_components(graph)), reverse=True)
    _measure(rows, "weakly connected components", len(weak))
    _measure(rows, "largest weak component", weak[0],
             f"{weak[0] / node_count * 100:.1f}% of nodes")
    _measure(rows, "strongly connected components", len(strong))
    _measure(rows, "largest strong component", strong[0],
             "everyone here can reach everyone else by replies")

    if edge_count:
        _measure(rows, "reciprocity", nx.reciprocity(graph),
                 "share of reply relationships that go both ways")
        _measure(rows, "self-replies", nx.number_of_selfloops(graph))

    _measure(rows, "mean out-degree", edge_count / node_count)

    table = pd.DataFrame(rows)
    if report:
        _print_structure(rows, graph)
    return table


def _print_structure(rows, graph):
    construction = graph.graph.get("construction")
    print(f"  graph structure{f' ({construction})' if construction else ''}")
    for row in rows:
        value = row["value"]
        if value is None:
            shown = "-"
        elif isinstance(value, str):
            shown = value
        elif isinstance(value, float):
            shown = f"{value:,.5f}" if abs(value) < 1000 else f"{value:,.1f}"
        else:
            shown = f"{value:,}"
        note = f"   {row['note']}" if row["note"] else ""
        print(f"    {row['measure']:<28}{shown:>14}{note}")


def compute_centrality_measures(graph, skip_slow=False, speed_up=True):
    """
    Compute centrality scores for nodes.
    """

    def choose_approx_k(n_nodes):
        if n_nodes < 1000:
            return n_nodes
        elif n_nodes < 5000:
            return 100
        elif n_nodes < 10000:
            return 50
        elif n_nodes < 30000:
            return 25
        else:
            return 10

    print("Calculating degree centrality ...")
    print(f"Graph stats: {graph.number_of_nodes():,} nodes, {graph.number_of_edges():,} edges")
    print(f"Graph density: {nx.density(graph):.5f}")

    degree = nx.degree_centrality(graph)
    results = {
        "channel_id": list(degree.keys()),
        "degree_centrality": list(degree.values()),
    }

    context = {"nodes": graph.number_of_nodes(), "edges": graph.number_of_edges(),
               "density": nx.density(graph), "filters": graph.graph.get("filters") or {}}

    if skip_slow:
        print("Skipping slow metrics (betweenness, eigenvector).")
        frame = pd.DataFrame(results).sort_values("degree_centrality", ascending=False)
        frame.attrs.update(context)
        return frame

    if not graph.number_of_edges():
        results["betweenness"] = [0.0 for _ in degree.keys()]
        results["eigenvector"] = [0.0 for _ in degree.keys()]
        frame = pd.DataFrame(results, columns=["channel_id", "degree_centrality",
                                               "betweenness", "eigenvector"]).sort_values(
            "degree_centrality", ascending=False)
        frame.attrs.update(context)
        return frame

    node_count = graph.number_of_nodes()
    k_value = choose_approx_k(node_count)

    if speed_up and node_count > 500:
        print(f"Graph has {node_count:,} nodes; using k-approximate betweenness with k = {k_value} ...")
        try:
            betweenness = nx.betweenness_centrality(graph, k=k_value, seed=42)
        except Exception as e:
            logger.error(f"Approximate betweenness calculation failed. Error: {e}.")
            print("Could not calculate the betweenness centrality.")
            betweenness = {}
    else:
        print("Calculating exact betweenness centrality. This may take a while.")
        try:
            betweenness = nx.betweenness_centrality(graph)
        except Exception as e:
            logger.error(f"Betweenness calculation failed. Error: {e}.")
            print("Could not calculate the betweenness centrality.")
            betweenness = {}

    results["betweenness"] = [betweenness.get(n, 0.0) for n in degree.keys()]

    try:
        print("Calculating eigenvector centrality ...")
        eigenvector = nx.eigenvector_centrality(graph, max_iter=1000, tol=1e-06)
        results["eigenvector"] = [eigenvector.get(n, 0.0) for n in degree.keys()]
    except Exception as e:
        logger.error(f"Eigenvector calculation failed. Error: {e}.")
        print("Could not calculate the eigenvector centrality.")
        results["eigenvector"] = [0.0 for _ in degree.keys()]

    frame = pd.DataFrame(results).sort_values("degree_centrality", ascending=False)
    frame.attrs.update(context)
    return frame


def plot_centrality_results(centrality_df, top_n=20, both=True, bar_figsize=(16,6), scatter_figsize=(8,6)):
    """Plot centrality analysis results, configurable sizes; 'both' toggles scatter."""

    titles = {"degree_centrality": "Degree Centrality", "betweenness": "Betweenness",
              "eigenvector": "Eigenvector"}
    df = centrality_df.copy()
    cols = [column for column in titles if column in df.columns]
    df = df[["channel_id"] + cols]
    top = df.sort_values("degree_centrality", ascending=False).head(top_n)

    fig, axes = plt.subplots(1, len(cols), figsize=bar_figsize, sharey=False, squeeze=False)

    for ax, metric, title in zip(axes.ravel(), cols, [titles[column] for column in cols]):
        tmp = top.sort_values(metric, ascending=True)
        ax.barh(tmp["channel_id"], tmp[metric])
        ax.set_title(title)
        ax.set_xlabel(metric)
        ax.set_ylabel("channel_id")

    plt.tight_layout()
    plt.show()

    if both and {"betweenness", "eigenvector"} <= set(cols):
        plt.figure(figsize=scatter_figsize)
        max_ev = top["eigenvector"].max() or 1
        sizes = 200 * (top["eigenvector"] / max_ev + 0.1)
        plt.scatter(top["degree_centrality"], top["betweenness"], s=sizes, alpha=0.7)

        for _, row in top.iterrows():
            plt.text(row["degree_centrality"], row["betweenness"], row["channel_id"], fontsize=8, ha="left", va="center")

        plt.xlabel("degree_centrality")
        plt.ylabel("betweenness")
        plt.title("Degree vs. Betweenness (marker size ~ eigenvector)")

        plt.tight_layout()
        plt.show()


def plot_top_channels(df, role="commenter", top_n=10):
    """Plot the top channels by role."""

    column_map = {
        "uploader" : "as_uploader",
        "commenter": "as_commenter",
        "replier"  : "as_replier",
        "total"    : "total_activity"
    }

    col = column_map.get(role, "as_commenter")
    top_df = df.sort_values(by=col, ascending=False).head(top_n)

    plt.figure(figsize=(10,6))
    plt.barh(top_df["channel_id"], top_df[col], color="steelblue")
    plt.gca().invert_yaxis()
    plt.title(f"Top {top_n} Channels by {role.title()}")
    plt.xlabel("Activity Count")

    plt.tight_layout()
    plt.show()


def plot_network_graph(graph, top_n=None, layout="spring", seed=42, figsize=(12,12), node_size=200, with_labels=True,
                       edge_cmap=plt.cm.Blues,):
    """
    Plot a (sub)graph with configurable layout and styling.
    layout might be 'spring', 'kamada_kawai', 'circular', 'random'. Some take ages to complete!
    """

    drawn = graph

    if top_n:
        degrees = dict(graph.degree())
        top_nodes = sorted(degrees, key=degrees.get, reverse=True)[:top_n]
        drawn = graph.subgraph(top_nodes).copy()

    if layout == "spring":
        positions = nx.spring_layout(drawn, k=0.15, iterations=20, seed=seed)
    elif layout == "kamada_kawai":
        positions = nx.kamada_kawai_layout(drawn)
    elif layout == "circular":
        positions = nx.circular_layout(drawn)
    else:
        positions = nx.random_layout(drawn, seed=seed)

    weights = [drawn[source][target]["weight"] for source, target in drawn.edges()]

    fig, ax = plt.subplots(figsize=figsize)

    nx.draw(drawn, positions, with_labels=with_labels, node_size=node_size, edge_color=weights, edge_cmap=edge_cmap, ax=ax)

    ax.set_title("Co-Occurrence Network of YouTube Channels in Comment Sections:\n" +
                 "YouTube Channels Connected by Shared Comment Activity", fontsize=16, pad=20)
    ax.axis("off")

    plt.tight_layout()
    plt.show()


def frequent_channel_pairs(threshold=3, roles=("uploader","commenter","replier"), exclude_uploader=True,
                           top_channels=None, min_videos_per_channel=2, max_participants_per_video=None, top_n=None,
                           block_rows=None, report=True):

    if report:
        print(f"  channel pairs: roles {'+'.join(roles)}, "
              f"uploader {'excluded' if exclude_uploader else 'included'}, "
              f"co-occurrence >= {threshold}")

    keep, participation = _select_participation(
        roles, exclude_uploader, min_videos_per_channel, max_participants_per_video,
        top_channels, report, label="participation (pairs)")

    empty = pd.DataFrame(columns=["channel_1","channel_2","co_occurrence"])
    if not keep:
        if report:
            print("    no channels left to pair")
        return empty

    incidence = _participation_matrix(keep, participation)
    channel_of = np.array(keep)

    pairs_seen = 0
    left, right, weights = [], [], []
    for start, band in _cooccurrence_bands(incidence, block_rows):
        indptr, indices, counts = band.indptr, band.indices, band.data
        rows = np.repeat(np.arange(start, start + band.shape[0]), np.diff(indptr))
        upper = indices > rows
        pairs_seen += int(upper.sum())
        selected = upper & (counts >= threshold)
        if not np.any(selected):
            continue
        left.append(rows[selected])
        right.append(indices[selected])
        weights.append(counts[selected])

    if not left:
        if report:
            print(f"    co-occurring pairs {pairs_seen:,} -> 0 at co-occurrence >= {threshold}")
        return empty

    pairs = pd.DataFrame({
        "channel_1": channel_of[np.concatenate(left)],
        "channel_2": channel_of[np.concatenate(right)],
        "co_occurrence": np.concatenate(weights).astype(np.int32),
    }).sort_values("co_occurrence", ascending=False)

    at_threshold = len(pairs)
    if top_n:
        pairs = pairs.head(top_n)

    if report:
        print(f"    co-occurring pairs {pairs_seen:,} -> {at_threshold:,} at co-occurrence "
              f">= {threshold}: {_share_of(pairs_seen - at_threshold, pairs_seen)} dropped")
        if top_n:
            print(f"    pairs {at_threshold:,} -> {len(pairs):,} at top_n {top_n:,}: "
                  f"{_share_of(at_threshold - len(pairs), at_threshold)} not shown")

    del incidence
    gc.collect()

    return pairs


def plot_channel_pair_network(pairs_df, top_n=50):
    """
    Visualize the most frequent co-occurring channel pairs as a network,
    including community detection and color-coded clusters, all done by networkx.
    """

    top_pairs = pairs_df.head(top_n)
    graph = nx.Graph()

    for _, row in top_pairs.iterrows():
        a, b, weight = row["channel_1"], row["channel_2"], row["co_occurrence"]
        graph.add_edge(a, b, weight=weight)

    if graph.number_of_edges() == 0:
        print("No edges to plot.")
        return

    communities = list(_detect_communities(graph, method="greedy"))
    node_community_map = {}
    for i, comm in enumerate(communities):
        for node in comm:
            node_community_map[node] = i

    pos = nx.spring_layout(graph, k=0.4, seed=42)                       # Layout, weights, colors
    weights = [graph[u][v]["weight"] for u, v in graph.edges()]
    cmap = colormaps.get_cmap("tab10")
    palette = getattr(cmap, "colors", [cmap(i / 9) for i in range(10)])
    node_colors = [palette[node_community_map.get(n, 0) % len(palette)] for n in graph.nodes()]

    plt.figure(figsize=(14, 14))
    max_weight = max(weights) if weights else 1
    scaled_widths = [1 + 2 * (weight / max_weight) for weight in weights]
    nx.draw_networkx_edges(graph, pos, width=scaled_widths, alpha=0.6, edge_color="gray")
    nx.draw_networkx_nodes(graph, pos, node_color=node_colors, node_size=500, alpha=0.9)
    nx.draw_networkx_labels(graph, pos, font_size=8)

    legend_colors = [palette[i % len(palette)] for i in range(len(communities))]
    for i, color in enumerate(legend_colors):
        plt.scatter([], [], color=color, label=f"Cluster {i+1}")
    plt.legend(title="Detected Communities", loc="upper left", fontsize=8)

    plt.title("Frequent Channel Pairs Network (Clustered)")
    plt.axis("off")
    plt.tight_layout()
    plt.show()

    try:
        nx.write_gexf(graph, "frequent_channel_pairs_network.gexf")
        print("    wrote frequent_channel_pairs_network.gexf")
    except OSError as e:
        logger.error(f"Could not write frequent_channel_pairs_network.gexf: {e}.")

"""
    # 4) Undirected interaction network, capped as well

    # 5) Centrality: bei riesigen Graphen approximieren/abbrechen

    # 6) Directed reply graph
"""
def tubegraph():

    logo = """
┌───────────────────────────────────────────────────────────┐
│      Welcome to ●     ●───●   ●   ●                       │
│                ╱ ╲     ╲ ╱ ╲ ╱ ╲ ╱ ╲                      │
│               ●───●     ●   ●   ●   ●────●                │
│                  ╱ ╲   ╱     ╲ ╱     ╲  ╱                 │
│                 ●   ●─●       ●      ●──● TubeGraph!      │
└───────────────────────────────────────────────────────────┘
"""

    print(logo)
    print("These functions take a long time (minutes to hours).")

    # 1. Basic stats
    df_stats = channel_occurrence_stats()
    write_table("tubegraph_channel_stats", df_stats)
    for role in ["uploader", "commenter", "replier", "total"]:
        plot_top_channels(df_stats, role=role)

    # 2. Participation matrix
    print("\nGenerating channel co-occurrence matrix. This may take a while.")
    matrix = channel_video_participation_matrix(roles=("uploader", "commenter", "replier"), top_channels=400,
                                                top_videos=None)
    plot_channel_clustering_heatmap(matrix, max_channels=400, cluster_max=300, fast=True, both=True)
    del matrix
    plt.close("all")
    gc.collect()


    # 3. Channel pairs
    print("\nDetecting frequent channel pairs.")
    pairs = frequent_channel_pairs(threshold=3, roles=("uploader","commenter","replier"), exclude_uploader=True,
                                   min_videos_per_channel=2, top_n=5000)
    write_table("tubegraph_channel_pairs", pairs)
    plot_channel_pair_network(pairs, top_n=200)

    # 4. Undirected interaction network
    print("Building undirected interaction graph.")
    graph = build_interaction_graph(roles=("uploader", "commenter", "replier"), exclude_uploader=True,
                                min_videos_per_channel=2, min_weight=2, top_k_per_node=50)
    print("Plotting network graph ...")
    plot_network_graph(graph, top_n=50)

    print("Plotting channel degree distribution.")
    plot_channel_degree_distribution(graph)
    print("\nChannels with the highest degree:")
    top = top_connected_channels(graph, top_n=20)
    print("No channels in the graph." if top.empty else top.to_string(index=False))

    print("\nDescribing the graph.")
    write_table("tubegraph_graph_structure", graph_structure(graph))

    print("\nFinding communities.")
    communities, membership = community_structure(min_videos_per_channel=2, min_weight=2, draws=20)
    write_table("tubegraph_communities", communities)
    write_table("tubegraph_community_membership", membership)

    print("Calculating centrality measures.")
    centrality_df = compute_centrality_measures(graph, skip_slow=False, speed_up=True)
    write_table("tubegraph_centrality", centrality_df)
    plot_centrality_results(centrality_df, top_n=10)

    print("Plotting role proportions.")
    plot_channel_role_proportions(df_stats, top_n=30)

    print("Constructing reply network ...")
    reply_graph = build_reply_network(include_self=False, min_weight=2)
    directed_structure(reply_graph)
    plot_reply_network(reply_graph, top_n=50)


def _shape_of(graph, label):
    undirected = graph.to_undirected() if graph.is_directed() else graph
    components = sorted(nx.connected_components(undirected), key=len, reverse=True)
    largest = components[0] if components else set()
    nodes = undirected.number_of_nodes()
    edges = undirected.number_of_edges()
    return {
        "label": label,
        "nodes": nodes,
        "edges": edges,
        "density": nx.density(undirected),
        "components": len(components),
        "largest_component": len(largest),
        "largest_component_share": len(largest) / max(1, nodes),
        "isolated_nodes": sum(1 for _, degree in undirected.degree() if degree == 0),
        "mean_degree": (2 * edges) / max(1, nodes),
        "transitivity": nx.transitivity(undirected),
    }


def tubegraphfull(matrix_channels=400, cluster_max=300, pair_threshold=3, pair_channels=None,
                  graph_channels=None, min_weight=2, community_method="louvain",
                  community_draws=20, community_budget=600.0, path_measures=False,
                  path_budget=30.0, max_participants=None, max_edges=None, block_rows=None,
                  skip_slow=False):

    roles = ("uploader", "commenter", "replier")

    stats = channel_occurrence_stats()
    write_table("tubegraph_channel_stats", stats)
    for role in ("uploader", "commenter", "replier", "total"):
        plot_top_channels(stats, role=role)
    plot_channel_role_proportions(stats, top_n=30, normalize=True)

    reply_graph = build_reply_network(include_self=False, min_weight=2)
    plot_reply_network(reply_graph, top_n=50)
    plot_channel_degree_distribution(reply_graph, cap_height=100, bin_count=40)
    write_table("tubegraph_top_connected", top_connected_channels(reply_graph, top_n=25))
    del reply_graph
    gc.collect()

    matrix = channel_video_participation_matrix(roles=roles, top_channels=matrix_channels,
                                                top_videos=None)
    plot_channel_clustering_heatmap(matrix, max_channels=matrix_channels,
                                    cluster_max=cluster_max, fast=True, both=True)
    del matrix
    gc.collect()

    pairs = frequent_channel_pairs(
        threshold=pair_threshold, roles=roles, exclude_uploader=True,
        top_channels=pair_channels, min_videos_per_channel=2,
        max_participants_per_video=max_participants, top_n=5000, block_rows=block_rows)
    write_table("tubegraph_channel_pairs", pairs)
    if len(pairs):
        plot_channel_pair_network(pairs, top_n=200)

    graph = build_interaction_graph(
        roles=roles, exclude_uploader=True, top_channels=graph_channels,
        min_videos_per_channel=2, max_participants_per_video=max_participants,
        min_weight=min_weight, top_k_per_node=50, max_edges=max_edges, block_rows=block_rows)
    write_table("tubegraph_graph_structure",
                graph_structure(graph, expensive=path_measures, budget_seconds=path_budget))

    communities, membership = community_structure(
        roles=roles, exclude_uploader=True, min_videos_per_channel=2,
        max_participants_per_video=max_participants, top_channels=graph_channels,
        min_weight=min_weight, max_edges=max_edges, block_rows=block_rows,
        method=community_method, draws=community_draws, budget_seconds=community_budget)
    write_table("tubegraph_communities", communities)
    write_table("tubegraph_community_membership", membership)

    plot_network_graph(graph, top_n=50)
    plot_channel_degree_distribution(graph, cap_height=100, bin_count=40)
    centrality = compute_centrality_measures(graph, skip_slow=skip_slow, speed_up=True)
    write_table("tubegraph_centrality", centrality)
    plot_centrality_results(centrality, top_n=20)
    del graph
    gc.collect()

    shapes = []
    for weight in (1, 2, 3, 5):
        graph = build_reply_network(include_self=False, min_weight=weight)
        shapes.append(_shape_of(graph, f"reply min_weight={weight}"))
        if weight == 1:
            plot_reply_network(graph, top_n=60)
            plot_channel_degree_distribution(graph, cap_height=200, bin_count=50)
        del graph
        gc.collect()

    for threshold in (2, 3, 5, 10):
        swept = frequent_channel_pairs(
            threshold=threshold, roles=roles, exclude_uploader=True,
            top_channels=pair_channels, min_videos_per_channel=2,
            max_participants_per_video=max_participants, top_n=100000, block_rows=block_rows)
        if threshold == 2:
            write_table("tubegraph_channel_pairs_threshold2", swept)
        del swept
        gc.collect()

    for channels in (8000, 30000):
        graph = build_interaction_graph(
            roles=roles, exclude_uploader=True, top_channels=channels,
            min_videos_per_channel=2, max_participants_per_video=max_participants,
            min_weight=1, top_k_per_node=50, max_edges=max_edges, block_rows=block_rows)
        shapes.append(_shape_of(graph, f"interaction top_channels={channels} min_weight=1"))
        if channels == 30000:
            plot_channel_degree_distribution(graph, cap_height=200, bin_count=50)
        del graph
        gc.collect()

    matrix = channel_video_participation_matrix(roles=roles, top_channels=1000, top_videos=None)
    plot_channel_clustering_heatmap(matrix, max_channels=1000, cluster_max=600,
                                    fast=True, both=True)
    del matrix
    gc.collect()

    write_table("tubegraph_network_shape", pd.DataFrame(shapes))


if __name__ == "__main__":
    tubegraph()
  