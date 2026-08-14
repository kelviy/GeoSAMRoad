"""Sliding window inference

Usage:
    python -m geosamroad.inferencer \
        --config src/geosamroad/configs/local/model.yaml \
        --checkpoint /kaggle/working/checkpoints/model/best.ckpt \
        --split test --output-dir save/model_output
"""
import math
import pickle
import time
from argparse import ArgumentParser
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import rtree
import scipy.spatial
import torch
import yaml
from rasterio.windows import Window
import rasterio

from geosamroad.sam_road import graph_extraction, graph_utils

from geosamroad import triage
from geosamroad.dataset.bands import ENHANCED_RGB
from geosamroad.dataset.helper import (
    read_split_csv,
    read_upsampled_window,
    read_window,
)
from geosamroad.models.model import GeoSAMRoad
from geosamroad.utils import (
    apply_overrides,
    build_dataset_config,
    finalize_config,
    load_config,
)

NATIVE_TILE_PX = 512


def parse_args():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True,
                        help="yaml config the checkpoint was trained with")
    parser.add_argument("--checkpoint", default="",
                        help="trained checkpoint (omit for random weights -- pipeline debug only)")
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--output-dir", default="",
                        help="output root (default save/<RUN_NAME>_<split>)")
    parser.add_argument("--dataset-dir", default="",
                        help="override DATASET_DIR (ROSA root)")
    parser.add_argument("--sam-ckpt-path", default="",
                        help="override SAM_CKPT_PATH (only needed without --checkpoint)")
    parser.add_argument("--set", dest="overrides", action="append", default=[],
                        metavar="KEY=VALUE", help="override any config key (repeatable)")
    parser.add_argument("--device", default="auto", help="auto|cuda|mps|cpu")
    parser.add_argument("--max-tiles", type=int, default=0,
                        help="stop after N tiles (0 = whole split); pipeline debug")
    return parser.parse_args()


def pick_device(name):
    if name != "auto":
        return torch.device(name)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def min_patches_per_edge(image_size, patch_size):
    """Fewest patches per edge that still tile ``image_size`` with no gap.

    The starts are ``linspace(0, image_size - patch_size, n)``, so consecutive
    ones are ``(image_size - patch_size) / (n - 1)`` apart; that has to be at
    most ``patch_size``, i.e. ``n >= image_size / patch_size``.
    """
    return max(1, math.ceil(image_size / patch_size))


def patch_offsets(image_size, patch_size, patches_per_edge):
    """Top-left corners of an evenly spaced overlapping patch grid."""
    minimum = min_patches_per_edge(image_size, patch_size)
    if patches_per_edge < minimum:
        raise ValueError(
            f"INFER_PATCHES_PER_EDGE={patches_per_edge} leaves gaps in a "
            f"{image_size}px tile at PATCH_SIZE={patch_size}: it needs at least "
            f"{minimum} per edge ({minimum ** 2} patches) to cover the tile."
        )
    if patches_per_edge < 2:
        starts = np.array([0])
    else:
        starts = np.round(
            np.linspace(0, image_size - patch_size, patches_per_edge)
        ).astype(int)
    return [(int(x), int(y)) for y in starts for x in starts]


def infer_one_tile(net, image, config, device):
    """image: [C, H, W] float tensor. Returns (pred_nodes_rc, pred_edges,
    keypoint_mask_u8, road_mask_u8) in upscaled-pixel coordinates."""
    _, H, W = image.shape
    patch = int(config.PATCH_SIZE)
    offsets = patch_offsets(H, patch, int(config.INFER_PATCHES_PER_EDGE))
    batch_size = int(config.INFER_BATCH_SIZE)

    fused = torch.zeros(2, H, W, device=device)
    counter = torch.zeros(H, W, device=device)
    batch_features, batch_offsets = [], []

    ## Pass 1: fused masks + cached per-patch encoder features.
    for start in range(0, len(offsets), batch_size):
        chunk = offsets[start:start + batch_size]
        patches = torch.stack(
            [image[:, y:y + patch, x:x + patch] for x, y in chunk]
        ).to(device)
        with torch.no_grad():
            # [B, H, W, 2], [B, D, h, w]
            mask_scores, features = net.infer_masks_and_img_features(patches)
        batch_features.append(features)
        batch_offsets.append(chunk)
        for i, (x, y) in enumerate(chunk):
            fused[0, y:y + patch, x:x + patch] += mask_scores[i, :, :, 0]
            fused[1, y:y + patch, x:x + patch] += mask_scores[i, :, :, 1]
            counter[y:y + patch, x:x + patch] += 1.0

    fused /= counter
    keypoint_mask = (fused[0] * 255).to(torch.uint8).cpu().numpy()
    road_mask = (fused[1] * 255).to(torch.uint8).cpu().numpy()

    # (x, y) points from the fused masks
    graph_points = graph_extraction.extract_graph_points(keypoint_mask, road_mask, config)
    if graph_points.shape[0] == 0:
        return graph_points, np.zeros((0, 2), dtype=np.int64), keypoint_mask, road_mask

    graph_rtree = rtree.index.Index()
    for i, (x, y) in enumerate(graph_points):
        graph_rtree.insert(i, (x, y, x, y))

    ## Pass 2: TopoNet edge scores from the cached features
    max_nbr = int(config.MAX_NEIGHBOR_QUERIES)
    edge_scores, edge_counts = defaultdict(float), defaultdict(float)
    for features, chunk in zip(batch_features, batch_offsets):
        topo_data = {"points": [], "pairs": [], "valid": []}
        idx_maps = []
        for x0, y0 in chunk:
            x1, y1 = x0 + patch, y0 + patch
            patch_point_indices = list(graph_rtree.intersection((x0, y0, x1, y1)))
            idx_maps.append(dict(enumerate(patch_point_indices)))
            n = len(patch_point_indices)
            patch_points = graph_points[patch_point_indices, :] - np.array(
                [[x0, y0]], dtype=graph_points.dtype
            )
            if n:
                kdtree = scipy.spatial.KDTree(patch_points)
                # k+1: the nearest neighbour is always self
                _, knn_idx = kdtree.query(
                    patch_points, k=max_nbr + 1,
                    distance_upper_bound=int(config.NEIGHBOR_RADIUS),
                )
                knn_idx = knn_idx[:, 1:]
            else:
                knn_idx = np.zeros((0, max_nbr), dtype=np.int64)
            src_idx = np.tile(np.arange(n)[:, np.newaxis], (1, max_nbr))
            valid = knn_idx < n
            tgt_idx = np.where(valid, knn_idx, src_idx)
            topo_data["points"].append(patch_points)
            topo_data["pairs"].append(np.stack([src_idx, tgt_idx], axis=-1))
            topo_data["valid"].append(valid)

        # pad each patch's arrays to the batch max point count and stack
        collated = {}
        for key, arrays in topo_data.items():
            length = max(a.shape[0] for a in arrays)
            collated[key] = np.stack([
                np.pad(a, [(0, length - a.shape[0])] + [(0, 0)] * (a.ndim - 1))
                for a in arrays
            ])
        if collated["points"].shape[1] == 0:
            continue

        points = torch.tensor(collated["points"], dtype=torch.float32, device=device)
        pairs = torch.tensor(collated["pairs"], dtype=torch.int64, device=device)
        valid = torch.tensor(collated["valid"], dtype=torch.bool, device=device)
        with torch.no_grad():
            # [B, N_samples, N_pairs, 1]
            topo_scores = net.infer_toponet(features, points, pairs, valid)
        # all-invalid (padded) queries return nan
        topo_scores = torch.where(
            torch.isnan(topo_scores), -100.0, topo_scores
        ).squeeze(-1).cpu().numpy()

        b, n_samples, n_pairs = topo_scores.shape
        for bi in range(b):
            for si in range(n_samples):
                for pi in range(n_pairs):
                    if not collated["valid"][bi, si, pi]:
                        continue
                    src_p, tgt_p = collated["pairs"][bi, si, pi, :]
                    src, tgt = idx_maps[bi][src_p], idx_maps[bi][tgt_p]
                    score = topo_scores[bi, si, pi]
                    assert 0.0 <= score <= 1.0
                    edge_scores[(src, tgt)] += score
                    edge_counts[(src, tgt)] += 1.0

    pred_edges = [
        e for e, s in edge_scores.items()
        if s / edge_counts[e] > float(config.TOPO_THRESHOLD)
    ]
    pred_edges = np.array(pred_edges, dtype=np.int64).reshape(-1, 2)
    pred_nodes = graph_points[:, ::-1]  # (x, y) -> (row, col)
    return pred_nodes, pred_edges, keypoint_mask, road_mask


def main():
    args = parse_args()
    config = load_config(args.config)
    if args.dataset_dir:
        config.DATASET_DIR = args.dataset_dir
    if args.sam_ckpt_path:
        config.SAM_CKPT_PATH = args.sam_ckpt_path
    apply_overrides(config, args.overrides)
    if args.checkpoint and not any(o.split("=")[0].strip() == "PRETRAINED"
                                   for o in args.overrides):
        config.PRETRAINED = False
    finalize_config(config)

    device = pick_device(args.device)
    ds_config = build_dataset_config(config)   # resolves the encoder's read bands
    upscale = int(config.UPSCALE)
    up_size = NATIVE_TILE_PX * upscale

    patch_offsets(up_size, int(config.PATCH_SIZE), int(config.INFER_PATCHES_PER_EDGE))

    net = GeoSAMRoad(config)
    if args.checkpoint:
        ckpt = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
        print(f"##### Loading trained ckpt {args.checkpoint} #####")
        net.load_state_dict(ckpt["state_dict"], strict=True)
    else:
        print("##### WARNING: no --checkpoint, running with untrained weights #####")
    net.eval().to(device)

    out_dir = Path(args.output_dir or f"save/{config.RUN_NAME}_{args.split}")
    for sub in ("mask", "viz", "graph"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)
    with open(out_dir / "config.yaml", "w") as f:
        yaml.dump(config.to_dict(), f)

    df = read_split_csv(config.DATASET_DIR, args.split)
    if args.max_tiles:
        df = df.head(args.max_tiles)
    data_dir = Path(config.DATASET_DIR)

    total_seconds = 0.0
    for row in df.itertuples(index=False):
        tile = Path(row.image_path).stem
        print(f"Processing {tile}")
        win = Window(0, 0, NATIVE_TILE_PX, NATIVE_TILE_PX)
        with rasterio.open(data_dir / row.image_path) as src:
            if upscale > 1:
                img = read_upsampled_window(src, list(ds_config.bands), win, up_size)
                rgb = read_upsampled_window(src, list(ENHANCED_RGB), win, up_size)
            else:
                img = read_window(src, list(ds_config.bands), win)
                rgb = read_window(src, list(ENHANCED_RGB), win)
        image = torch.from_numpy(np.ascontiguousarray(img))

        # Convert to rgb
        rgb = (np.clip(rgb, 0.0, 1.0).transpose(1, 2, 0) * 255).astype(np.uint8)

        start = time.time()
        pred_nodes, pred_edges, itsc_mask, road_mask = infer_one_tile(
            net, image, config, device
        )
        total_seconds += time.time() - start

        cv2.imwrite(str(out_dir / "mask" / f"{tile}_road.png"), road_mask)
        cv2.imwrite(str(out_dir / "mask" / f"{tile}_itsc.png"), itsc_mask)

        viz = triage.visualize_image_and_graph(
            rgb, pred_nodes / up_size, pred_edges, up_size,
            node_radius=1, edge_thickness=1,
        ) if pred_nodes.shape[0] else cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        cv2.imwrite(str(out_dir / "viz" / f"{tile}.png"), viz)

        graph = graph_utils.convert_to_sat2graph_format(
            pred_nodes[:, ::-1] / upscale, pred_edges
        ) if pred_nodes.shape[0] else {}
        with open(out_dir / "graph" / f"{tile}.p", "wb") as f:
            pickle.dump(graph, f)
        print(f"Done {tile}: {pred_nodes.shape[0]} nodes, {pred_edges.shape[0]} edges")

    summary = (f"Inference over {len(df)} {args.split} tiles with {args.config}: "
               f"{total_seconds:.1f}s")
    print(summary)
    with open(out_dir / "inference_time.txt", "w") as f:
        f.write(summary + "\n")


if __name__ == "__main__":
    main()
