""" Visualise prediction and ground truth side by side. After inferencer and graph metrics are calculated
Usage: 
    python -m geosamroad.triage --run-dir save/model_output \
        --dataset-dir <ROSA> --split test --metric apls --worst 20
"""
import cv2
import numpy as np


def visualize_image_and_graph(img, nodes, edges, viz_img_size=512, node_radius=4, edge_thickness=4):
    """Draw a predicted graph over the RGB tile"""
    # img is rgb
    # Node coordinates in [0, 1], representing the normalized (r, c)
    # (r, c) -> (x, y)
    nodes = nodes[:, ::-1]

    # Resize the image to the specified visualization size, RGB->BGR
    img = cv2.resize(img, (viz_img_size, viz_img_size))
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

    # Draw edges
    for edge in edges:
        start_node = nodes[edge[0]] * viz_img_size
        end_node = nodes[edge[1]] * viz_img_size
        cv2.line(
            img,
            (int(start_node[0]), int(start_node[1])),
            (int(end_node[0]), int(end_node[1])),
            (15, 160, 253),
            edge_thickness,
        )

    # Draw nodes -- after the edges, so they stay visible on top
    for node in nodes:
        x, y = node * viz_img_size
        cv2.circle(img, (int(x), int(y)), node_radius, (0, 255, 255), -1)

    return img


def rasterize_graph(nodes, edges, viz_img_size, dilation_radius):
    # Rasterize the graph.
    # Node coordinates in [0, 1], representing the normalized (r, c)

    # (r, c) -> (x, y)
    nodes = nodes[:, ::-1]

    # Creates the canvas
    img = np.zeros((viz_img_size, viz_img_size, 3), dtype=np.uint8)

    # Draw predicted nodes as white squares
    for node in nodes:
        x, y = node * viz_img_size
        cv2.rectangle(
            img,
            (int(x) - dilation_radius, int(y) - dilation_radius),
            (int(x) + dilation_radius, int(y) + dilation_radius),
            (255, 255, 255),
            -1,
        )

    # Draw predicted edges as white lines
    for edge in edges:
        start_node = nodes[edge[0]] * viz_img_size
        end_node = nodes[edge[1]] * viz_img_size
        cv2.line(
            img,
            (int(start_node[0]), int(start_node[1])),
            (int(end_node[0]), int(end_node[1])),
            (255, 255, 255),
            dilation_radius * 2,
        )

    return img


def _adj_to_nodes_edges(adj, upscale, viz_size):
    """adjacency dict to image space (row, column)"""
    idx = {k: i for i, k in enumerate(adj)}
    nodes = [k for k in adj]
    edges, seen = [], set()
    for n1, nbrs in adj.items():
        for n2 in nbrs:
            if n2 not in idx:
                idx[n2] = len(nodes)
                nodes.append(n2)
            if (n1, n2) in seen or (n2, n1) in seen:
                continue
            seen.add((n1, n2))
            edges.append((idx[n1], idx[n2]))
    if not nodes:
        return np.zeros((0, 2)), np.zeros((0, 2), dtype=np.int64)
    nodes_rc = np.array([(y, x) for x, y in nodes], dtype=np.float64)
    nodes_rc = nodes_rc * upscale / viz_size
    return nodes_rc, np.array(edges, dtype=np.int64)


def _label(img, text):
    cv2.rectangle(img, (0, 0), (img.shape[1], 34), (0, 0, 0), -1)
    cv2.putText(img, text, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (255, 255, 255), 2, cv2.LINE_AA)
    return img


def main():
    import pickle
    from argparse import ArgumentParser
    from pathlib import Path

    import pandas as pd
    import rasterio
    from rasterio.windows import Window

    from geosamroad.dataset.bands import ENHANCED_RGB
    from geosamroad.dataset.helper import read_split_csv, read_upsampled_window

    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True,
                        help="inferencer output dir (with graph/ and results/results.csv)")
    parser.add_argument("--dataset-dir", required=True, help="ROSA root")
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--metric", default="apls",
                        choices=["apls", "topo_precision", "topo_recall",
                                 "topo_overall_recall"],
                        help="rank tiles ascending by this results.csv column")
    parser.add_argument("--worst", type=int, default=20,
                        help="how many worst tiles to render")
    parser.add_argument("--upscale", type=int, default=4)
    parser.add_argument("--viz-size", type=int, default=2048,
                        help="output panel edge px (per side)")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    results_csv = run_dir / "results" / "results.csv"
    if not results_csv.exists():
        raise SystemExit(
            f"{results_csv} not found -- run geosamroad.eval_graphs first "
            f"(--pred-dir {run_dir / 'graph'})")
    scores = pd.read_csv(results_csv).sort_values(args.metric, na_position="last")
    worst = scores.head(args.worst)

    split_df = read_split_csv(args.dataset_dir, args.split)
    by_tile = {Path(r.image_path).stem: r for r in split_df.itertuples(index=False)}
    data_dir = Path(args.dataset_dir)
    up_size = 512 * args.upscale
    out_dir = run_dir / "triage"
    out_dir.mkdir(parents=True, exist_ok=True)

    for rank, row in enumerate(worst.itertuples(index=False)):
        tile = row.tile
        meta = by_tile.get(tile)
        if meta is None:
            print(f"{tile}: not in {args.split} split, skipping")
            continue
        with rasterio.open(data_dir / meta.image_path) as src:
            rgb = read_upsampled_window(
                src, list(ENHANCED_RGB), Window(0, 0, 512, 512), up_size)
        rgb = (np.clip(rgb, 0.0, 1.0).transpose(1, 2, 0) * 255).astype(np.uint8)

        with open(run_dir / "graph" / f"{tile}.p", "rb") as f:
            pred_adj = pickle.load(f)
        with open(data_dir / meta.mask_adj_graph_path, "rb") as f:
            gt_adj = pickle.load(f)

        panels = []
        for name, adj in (("pred", pred_adj), ("gt", gt_adj)):
            nodes, edges = _adj_to_nodes_edges(adj, args.upscale, up_size)
            panel = visualize_image_and_graph(rgb.copy(), nodes, edges, args.viz_size)
            score = getattr(row, args.metric)
            _label(panel, f"{name}  {tile}  {args.metric}={score:.4f}")
            panels.append(panel)
        out = np.concatenate(panels, axis=1)
        out_path = out_dir / f"{rank:03d}_{args.metric}_{getattr(row, args.metric):.4f}_{tile}.png"
        cv2.imwrite(str(out_path), out)
        print(f"{rank:03d} {tile} {args.metric}={getattr(row, args.metric):.4f} -> {out_path.name}")

    print(f"\n{min(len(worst), args.worst)} panels in {out_dir}")


if __name__ == "__main__":
    main()
