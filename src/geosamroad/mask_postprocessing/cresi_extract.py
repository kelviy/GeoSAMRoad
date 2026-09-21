import networkx as nx

from . import skeletonize, wkt_to_graph

EMPTY_WKT = "LINESTRING EMPTY"


## Configs
DEFAULTS = dict(
    hole_size=4,                    # fill gaps size in px
    min_spur_length_pix=10,         # 50 m at 5 m/px
    min_subgraph_length_pix=60,     # 300 m at 5 m/px
    cv2_kernel_close=7,
    cv2_kernel_open=7,
    skel_replicate=5,
    skel_clip=2,
    use_medial_axis=False,
    add_small=True,
)


def road_png_to_nx(road_png, thresh, verbose=False, **overrides):
    p = {**DEFAULTS, **overrides}

    params = (
        str(road_png),              # img path
        "",                         # out ske file
        "",                         # out gpickle
        thresh,
        False,                      # debug
        True,                       # fix borders
        (),
        p["skel_replicate"],
        p["skel_clip"],
        255,                        # img mult
        p["hole_size"],
        p["cv2_kernel_close"],
        p["cv2_kernel_open"],
        p["min_subgraph_length_pix"],
        p["min_spur_length_pix"],
        (200000, 200000),           # max output size
        p["use_medial_axis"],
        1,                          # num classes
        "all",                      # skeleton band
        27,                         # kernel blur
        0.2,                        # min background frac
        verbose,
    )
    res = skeletonize.img_to_ske_G(params)
    # Empty mask is [EMPTY, [], []] instead of (G, ske, img)
    if res[0] == EMPTY_WKT:
        return nx.MultiGraph()
    G_ske = res[0]

    wkt_list = skeletonize.G_to_wkt(G_ske, add_small=p["add_small"],
                                    connect_crossroads=True, verbose=verbose)
    if not wkt_list or wkt_list == [EMPTY_WKT]:
        return nx.MultiGraph()

    nodes_edges = wkt_to_graph.wkt_list_to_nodes_edges(wkt_list)
    if nodes_edges is None:
        print(f"  WARNING: wkt_list_to_nodes_edges bailed out on {road_png} "
              f"(duplicate edge) -- emitting an empty graph")
        return nx.MultiGraph()

    G = wkt_to_graph.nodes_edges_to_G(*nodes_edges)
    if len(G.nodes()) == 0:
        return G
    return wkt_to_graph.clean_sub_graphs(
        G, min_length=p["min_subgraph_length_pix"], weight="length_pix",
        verbose=False, super_verbose=False)


def nx_to_edges_xy(G):
    """networkx graph -> [((x0, y0), (x1, y1)), ...] in mask-pixel space."""
    edges = []
    for u, v in G.edges():
        du, dv = G.nodes[u], G.nodes[v]
        edges.append(((du["x_pix"], du["y_pix"]), (dv["x_pix"], dv["y_pix"])))
    return edges

def edges_xy_to_adj(edges_xy, upscale):
    adj = {}
    for (x0, y0), (x1, y1) in edges_xy:
        u = (x0 / upscale, y0 / upscale)
        v = (x1 / upscale, y1 / upscale)
        if u == v:                      # self-loop after the downscale
            continue
        adj.setdefault(u, []).append(v)
        adj.setdefault(v, [])           # never leave an endpoint dangling
    return adj
