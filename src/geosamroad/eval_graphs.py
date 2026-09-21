"""TOPO + APLS metrics

Usage:
    python -m geosamroad.eval_graphs \
        --pred-dir save/model_output/graph \
        --dataset-dir /path/to/ROSADataset --split test
"""
import csv
import json
import math
import pickle
import shutil
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

import numpy as np

# import topo and apls
_PKG_DIR = Path(__file__).resolve().parent
_TOPO_DIR = _PKG_DIR / "metrics" / "topo"
_APLS_DIR = _PKG_DIR / "metrics" / "apls"
sys.path.insert(0, str(_TOPO_DIR))

import graph as splfy
import topo as topo_mod

from geosamroad.dataset.helper import read_split_csv

NATIVE_M_PER_PX = 10.0
LAT_TOP_LEFT = 41.0
LON_TOP_LEFT = -71.0
DEG_PER_UNIT = 1.0 / 111111.0


def xy2latlon(x, y):
    lat = LAT_TOP_LEFT - x * DEG_PER_UNIT
    lon = LON_TOP_LEFT + (y * DEG_PER_UNIT) / math.cos(math.radians(LAT_TOP_LEFT))
    return lat, lon


def parse_args():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--pred-dir", required=True,
                        help="inferencer graph/ dir with <tile>.p pickles")
    parser.add_argument("--dataset-dir", required=True, help="ROSA root")
    parser.add_argument("--split", default="test", choices=["train", "val", "test"])
    parser.add_argument("--out-dir", default="",
                        help="results root (default <pred-dir>/../results)")
    parser.add_argument("--metric-scale", type=int, default=4,
                        help="scoring-space px scale applied to BOTH graphs")
    parser.add_argument("--no-topo", action="store_true")
    parser.add_argument("--no-apls", action="store_true")
    parser.add_argument("--topo-matching-m", type=float, default=30.0,
                        help="TOPO marble-hole matching distance, ground metres")
    parser.add_argument("--topo-interval-m", type=float, default=15.0,
                        help="TOPO marble spacing along roads, ground metres")
    parser.add_argument("--topo-r-m", type=float, default=400.0,
                        help="TOPO propagation radius from each start, ground metres")
    parser.add_argument("--max-tiles", type=int, default=0)
    return parser.parse_args()


def load_adj(path):
    with open(path, "rb") as f:
        return pickle.load(f)


def scale_adj(adj, s):
    return {(k[0] * s, k[1] * s): [(n[0] * s, n[1] * s) for n in v]
            for k, v in adj.items()}


def build_road_graph(adj, bounds):
    """sat2graph dict (x, y) -> metrics/topo RoadGraph; tracks (min_lat, max_lon)."""
    g = splfy.RoadGraph()
    idmap, nid = {}, 0
    for n1, nbrs in adj.items():
        lat1, lon1 = xy2latlon(n1[0], n1[1])
        bounds[0] = min(bounds[0], lat1)
        bounds[1] = max(bounds[1], lon1)
        for n2 in nbrs:
            lat2, lon2 = xy2latlon(n2[0], n2[1])
            if n1 not in idmap:
                idmap[n1] = nid
                nid += 1
            if n2 not in idmap:
                idmap[n2] = nid
                nid += 1
            g.addEdge(idmap[n1], lat1, lon1, idmap[n2], lat2, lon2)
    g.ReverseDirectionLink()
    for node in g.nodes.keys():
        g.nodeScore[node] = 100
    for edge in g.edges.keys():
        g.edgeScore[edge] = 100
    return g


def run_topo(gt_adj, pred_adj, out_txt, match_deg, interval_deg, r_deg):
    """Returns (precision, recall, coverage, overall_recall)."""
    bounds = [LAT_TOP_LEFT, LON_TOP_LEFT]  # [min_lat, max_lon], updated in place
    g_gt = build_road_graph(gt_adj, bounds)
    g_prop = build_road_graph(pred_adj, bounds)
    region = [bounds[0] - 300 * DEG_PER_UNIT, LON_TOP_LEFT - 500 * DEG_PER_UNIT,
              LAT_TOP_LEFT + 300 * DEG_PER_UNIT, bounds[1] + 500 * DEG_PER_UNIT]
    g_gt.region = region
    g_prop.region = region

    out_txt.unlink(missing_ok=True)  # TOPOWithPairs appends
    losm = topo_mod.TOPOGenerateStartingPoints(
        g_gt, region=region, image="NULL", check=False, direction=False, metaData=None)
    lmap = topo_mod.TOPOGeneratePairs(
        g_prop, g_gt, losm, threshold=match_deg, region=region)
    topo_mod.TOPOWithPairs(
        g_prop, g_gt, lmap, losm, r=r_deg, step=interval_deg,
        threshold=match_deg, outputfile=str(out_txt),
        one2oneMatching=True, metaData=None)

    # last numeric line: "p r coverage overall_recall"
    vals = None
    for line in out_txt.read_text().splitlines():
        parts = line.split()
        if len(parts) == 4:
            try:
                vals = [float(p) for p in parts]
            except ValueError:
                continue
    if vals is None:
        raise RuntimeError(f"no TOPO result line in {out_txt}")
    return tuple(vals)


def adj_to_apls_json(adj, path):
    nodes, edges, nodemap, edge_map = [], [], {}, set()
    for k in adj:
        nodemap[k] = len(nodes)
        nodes.append(list(xy2latlon(k[0], k[1])))
    for n1, nbrs in adj.items():
        for n2 in nbrs:
            if n2 not in nodemap:      # neighbour not a key (dangling); add it
                nodemap[n2] = len(nodes)
                nodes.append(list(xy2latlon(n2[0], n2[1])))
            if (n1, n2) in edge_map or (n2, n1) in edge_map:
                continue
            edge_map.add((n1, n2))
            edges.append([nodemap[n1], nodemap[n2]])
    with open(path, "w") as f:
        json.dump([nodes, edges], f)


def build_apls_binary(out_dir):
    """Compile metrics/apls/main.go once; returns the binary path or None."""
    if shutil.which("go") is None:
        print("WARNING: `go` not found -- skipping APLS")
        return None
    try:
        if not (_APLS_DIR / "go.mod").exists():
            subprocess.run(["go", "mod", "init", "apls"], cwd=_APLS_DIR, check=True,
                           capture_output=True)
            subprocess.run(["go", "mod", "tidy"], cwd=_APLS_DIR, check=True,
                           capture_output=True)
        binary = Path(out_dir).resolve() / "apls_bin"
        subprocess.run(["go", "build", "-o", str(binary), "main.go"],
                       cwd=_APLS_DIR, check=True, capture_output=True)
        return binary
    except subprocess.CalledProcessError as e:
        print("WARNING: APLS build failed -- skipping APLS\n",
              e.stderr.decode(errors="replace"))
        return None


def run_apls(binary, gt_adj, pred_adj, work_dir, tile):
    gt_json = work_dir / f"{tile}_gt.json"
    prop_json = work_dir / f"{tile}_prop.json"
    out_txt = work_dir / f"{tile}.txt"
    adj_to_apls_json(gt_adj, gt_json)
    adj_to_apls_json(pred_adj, prop_json)
    subprocess.run([str(binary), str(gt_json), str(prop_json), str(out_txt)],
                   check=True, capture_output=True)
    # "apls_gt apls_prop apls"
    return float(out_txt.read_text().split()[2])


def main():
    args = parse_args()
    pred_dir = Path(args.pred_dir)
    out_dir = Path(args.out_dir or pred_dir.parent / "results")
    topo_dir, apls_dir = out_dir / "topo", out_dir / "apls"
    for d in (topo_dir, apls_dir):
        d.mkdir(parents=True, exist_ok=True)

    s = args.metric_scale                  # native px -> scoring units (2.5 m/unit at 4)
    m_per_unit = NATIVE_M_PER_PX / s       # ground metres per coordinate unit
    to_deg = lambda ground_m: (ground_m / m_per_unit) * DEG_PER_UNIT
    match_deg = to_deg(args.topo_matching_m)
    interval_deg = to_deg(args.topo_interval_m)
    r_deg = to_deg(args.topo_r_m)

    apls_binary = None if args.no_apls else build_apls_binary(out_dir)

    df = read_split_csv(args.dataset_dir, args.split)
    if args.max_tiles:
        df = df.head(args.max_tiles)

    rows, skipped = [], []
    for row in df.itertuples(index=False):
        tile = Path(row.image_path).stem
        gt_path = Path(args.dataset_dir) / row.mask_adj_graph_path
        pred_path = pred_dir / f"{tile}.p"
        if not pred_path.exists():
            print(f"{tile}: no prediction pickle, skipping")
            skipped.append(tile)
            continue
        gt_adj = scale_adj(load_adj(gt_path), s)
        pred_adj = scale_adj(load_adj(pred_path), s)
        if not gt_adj:
            skipped.append(tile)           # empty GT: nothing to score
            continue

        result = {"tile": tile, "topo_precision": np.nan, "topo_recall": np.nan,
                  "topo_overall_recall": np.nan, "apls": np.nan}
        if not pred_adj:                   # predicted nothing on a road tile
            result.update(topo_precision=0.0, topo_recall=0.0,
                          topo_overall_recall=0.0, apls=0.0)
        else:
            if not args.no_topo:
                try:
                    p, r, cov, overall_r = run_topo(
                        gt_adj, pred_adj, topo_dir / f"{tile}.txt",
                        match_deg, interval_deg, r_deg)
                    result.update(topo_precision=p, topo_recall=r,
                                  topo_overall_recall=overall_r)
                except Exception as e:
                    print(f"{tile}: TOPO failed: {e}")
            if apls_binary is not None:
                try:
                    result["apls"] = run_apls(apls_binary, gt_adj, pred_adj,
                                              apls_dir, tile)
                except Exception as e:
                    print(f"{tile}: APLS failed: {e}")
        rows.append(result)
        print(f"{tile}: topo P={result['topo_precision']:.4f} "
              f"R={result['topo_recall']:.4f} apls={result['apls']:.4f}")

    csv_path = out_dir / "results.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["tile", "topo_precision",
                                               "topo_recall",
                                               "topo_overall_recall", "apls"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{len(rows)} tiles scored, {len(skipped)} skipped "
          f"(empty GT / missing prediction) -> {csv_path}")
    for key in ("topo_precision", "topo_recall", "topo_overall_recall", "apls"):
        vals = np.array([r[key] for r in rows], dtype=float)
        if np.isfinite(vals).any():
            print(f"mean {key}: {np.nanmean(vals):.4f}")


if __name__ == "__main__":
    main()
