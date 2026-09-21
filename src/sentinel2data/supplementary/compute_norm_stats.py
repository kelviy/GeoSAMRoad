from pathlib import Path
import numpy as np
import rasterio
from geosamroad.dataset.bands import ROSA_BANDS
from geosamroad.dataset.helper import read_split_csv

IMAGERY_COLS = "image_path"
SAR_NAMES = {"VV_ascending", "VH_ascending", "VV_descending", "VH_descending"}

def _band_names(n):
    names = list(ROSA_BANDS[:n])
    names += [f"band{i + 1}" for i in range(len(names), n)]
    return names

def _train_imagery_paths(dataset_dir):
    df = read_split_csv(dataset_dir, "train")

    if IMAGERY_COLS in df.columns:
        col = IMAGERY_COLS
    else:
        raise ValueError(
            f"train.csv has no imagery column {IMAGERY_COLS}; got {list(df.columns)}"
        )

    return df[col].drop_duplicates().tolist()


def compute(dataset_dir, exclude_zero=True, sar_clip=None):
    """Calculate mean and standard deviation over images per band."""
    root = Path(dataset_dir)
    image_paths = _train_imagery_paths(root)
    if not image_paths:
        raise ValueError(f"No training imagery found under {root}/splits/train.csv")

    with rasterio.open(root / image_paths[0]) as src:
        n = src.count
    names = _band_names(n)
    sar_idx = {i for i, nm in enumerate(names) if nm in SAR_NAMES}

    sum = np.zeros(n, "float64")
    squared_sum = np.zeros(n, "float64")
    count = np.zeros(n, "float64")

    for relative_path in image_paths:
        path = root / relative_path
        if not path.exists():
            print(f"[stats] skip {relative_path}: not found")
            continue

        with rasterio.open(path) as src:
            arr = src.read().astype("float64")  # (C, H, W)
            nodata = src.nodata

        if arr.shape[0] != n:
            raise ValueError(f"{path}: {arr.shape[0]} bands, expected {n}")

        for b in range(n):
            band = arr[b]
            valid = np.isfinite(band)
            if nodata is not None:
                valid &= band != nodata
            elif exclude_zero:
                valid &= band != 0
            image = band[valid]

            # clip normalisation
            if sar_clip and b in sar_idx:
                image = np.clip(image, sar_clip[0], sar_clip[1])

            sum[b] += image.sum()
            squared_sum[b] += (image * image).sum()
            count[b] += image.size
        # print(f"[current file] {Path(rel).stem}")

    count[count == 0] = 1.0
    mean = sum / count
    std = np.sqrt(np.maximum(squared_sum / count - mean ** 2, 0.0))
    return names, mean.astype("float32"), std.astype("float32")


def _yaml_list(values):
    return "[" + ", ".join(f"{float(x):.6g}" for x in values) + "]"

def write_stats_yaml(out_path, mean, std):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "data:\n"
        f"  norm_mean: {_yaml_list(mean)}\n"
        f"  norm_std: {_yaml_list(std)}\n"
    )
    return out

def print_table(names, mean, std):
    for name, m, sd in zip(names, mean, std):
        print(f"  {name:>14}: mean={m:12.3f}  std={sd:12.3f}")
