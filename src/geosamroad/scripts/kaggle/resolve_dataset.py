"""Resolve a ROSA dataset root from whatever is mounted under /kaggle/input.

Kaggle mounts each attached dataset separately and read-only, and the high-res
aerial tiles may live in their own dataset. This walks the mounts, works out
which one supplies each piece, and stitches a single writable-looking root out
of symlinks so the training config only needs one DATASET_DIR.

    python resolve_dataset.py [--require-highres] [--out /kaggle/working/dataset]

Writes the resolved path to /kaggle/working/geosamroad_dataset_dir.txt.
"""
import argparse
import shutil
import sys
from pathlib import Path

SPLITS = ("train", "val", "test")

# What the high-res SAM-Road config actually reads per split.
#   high_res_rgb    -> the 2.5m imagery
#   masks_graph     -> road centrelines, re-rasterised per crop
#   mask_adj_graphs -> TopoNet labels and the keypoint disks
#   masks_raster    -> only the MIN_CROP_ROAD_PX crop filter
REQUIRED_SUBDIRS = ("high_res_rgb", "masks_graph", "mask_adj_graphs", "masks_raster")
# Unused at UPSCALE > 1 but linked when present so other configs still work.
OPTIONAL_SUBDIRS = ("imagery", "mask_keypoints")


# How deep under the mount root a dataset root may sit. Kaggle's "Add Input"
# mounts at /kaggle/input/<slug>/, so ROSADataset/ lands at depth 2; a kagglehub
# cache layout (datasets/<owner>/<name>/versions/<n>/ROSADataset) reaches depth 5.
MAX_SEARCH_DEPTH = 6


def find_provider(search_root: Path, relative: str, max_depth: int = MAX_SEARCH_DEPTH):
    """Shallowest directory under `search_root` that contains `relative`.

    Globs for the marker itself rather than enumerating every directory, so the
    walk only descends path components that can match and never lists the
    thousands of tiles inside a split folder.
    """
    depth_of_relative = len(Path(relative).parts)
    for depth in range(max_depth + 1):
        prefix = "/".join(["*"] * depth)
        pattern = f"{prefix}/{relative}" if prefix else relative
        hits = sorted(search_root.glob(pattern))
        if hits:
            # strip the relative part back off to get the dataset root
            return hits[0].parents[depth_of_relative - 1]
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input-root", default="/kaggle/input")
    ap.add_argument("--out", default="/kaggle/working/dataset")
    ap.add_argument("--path-file", default="/kaggle/working/geosamroad_dataset_dir.txt")
    ap.add_argument("--require-highres", action="store_true",
                    help="fail unless every split has high_res_rgb")
    args = ap.parse_args()

    mounts = Path(args.input_root)
    if not mounts.is_dir():
        sys.exit(f"ERROR: {mounts} does not exist. Attach the dataset to the "
                 f"notebook (Add Input), or pass --input-root.")

    splits_root = find_provider(mounts, "splits/train.csv")
    if splits_root is None:
        print(f"Searched {mounts} to depth {MAX_SEARCH_DEPTH}; what is there:")
        for depth in (1, 2, 3):
            for p in sorted(mounts.glob("/".join(["*"] * depth)))[:15]:
                if p.is_dir():
                    print("   ", p)
        sys.exit("\nERROR: no mounted dataset contains splits/train.csv. "
                 "Attach the ROSA dataset that holds splits/.")

    out = Path(args.out)
    if out.is_symlink():
        out.unlink()
    elif out.exists():
        # the tree is only symlinks and empty dirs; rmtree unlinks rather than
        # follows them, so nothing in /kaggle/input is touched
        shutil.rmtree(out)
    out.mkdir(parents=True)

    (out / "splits").symlink_to(splits_root / "splits")
    print(f"splits/            <- {splits_root}")
    for name in ("metadata.parquet", "norm_stats.yaml", "dataset_summary.yaml"):
        src = find_provider(mounts, name)
        if src is not None:
            (out / name).symlink_to(src / name)

    missing, report = [], {}
    for split in SPLITS:
        (out / split).mkdir()
        for sub in REQUIRED_SUBDIRS + OPTIONAL_SUBDIRS:
            provider = find_provider(mounts, f"{split}/{sub}")
            if provider is None:
                if sub in REQUIRED_SUBDIRS:
                    missing.append(f"{split}/{sub}")
                continue
            (out / split / sub).symlink_to(provider / split / sub)
            report.setdefault(sub, set()).add(str(provider))

    print("\nresolved components:")
    for sub in REQUIRED_SUBDIRS + OPTIONAL_SUBDIRS:
        where = report.get(sub)
        status = " / ".join(sorted(where)) if where else "MISSING"
        flag = "  " if where or sub in OPTIONAL_SUBDIRS else "!!"
        print(f"  {flag} {sub:16s} <- {status}")

    if missing:
        print(f"\nERROR: no mounted dataset provides: {', '.join(missing)}")
        print("Attach the dataset(s) holding those folders and re-run this cell.")
        sys.exit(1)

    # tile counts, so a half-uploaded dataset shows up now and not mid-epoch
    print("\ntile counts:")
    for split in SPLITS:
        hr = out / split / "high_res_rgb"
        n_hr = len(list(hr.glob("*.tif"))) if hr.is_dir() else 0
        n_csv = sum(1 for _ in open(out / "splits" / f"{split}.csv")) - 1
        flag = "  " if n_hr >= n_csv else "!!"
        print(f"  {flag} {split:5s} csv={n_csv:5d}  high_res_rgb={n_hr:5d}")
        if args.require_highres and n_hr < n_csv:
            print(f"     ({n_csv - n_hr} tiles have no high-res image; "
                  f"the dataloader drops them)")

    Path(args.path_file).write_text(str(out) + "\n")
    print(f"\nDATASET_DIR = {out}  (written to {args.path_file})")


if __name__ == "__main__":
    main()
