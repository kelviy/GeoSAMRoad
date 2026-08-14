"""window reads + per-image standardisation"""
import numpy as np
from pathlib import Path
import pandas as pd
from rasterio.enums import Resampling

def train_crop_bounds(crop_size, image_size, dataset_length):
    image_idx = np.random.randint(0, dataset_length)
    x = np.random.randint(0, image_size - crop_size + 1)
    y = np.random.randint(0, image_size - crop_size + 1)
    return (image_idx, x, y, crop_size)

def eval_crop_bounds(idx, crop_size, image_size):
    n = image_size // crop_size
    patch_per_tile = n * n
    image_idx = idx // patch_per_tile
    patch = idx % (patch_per_tile)
    y = (patch // n) * crop_size
    x = (patch % n) * crop_size
    return (image_idx, x, y, crop_size)


def d4_augment(img, mask):
    """D4 geometric transformation like https://albumentations.ai/docs/examples/example-d4/
    Does 90-270 degree rotations and horizontal flips

    Returns views of arrays
    """
    k = np.random.randint(0, 4)
    if k:
        img = np.rot90(img, k, axes=(1, 2))
        mask = np.rot90(mask, k, axes=(0, 1))

    flip = np.random.random() < 0.5
    if flip:
        img = np.flip(img, axis=2)
        mask = np.flip(mask, axis=1)

    return img, mask, {"rot_k": k, "flip": flip}


def read_window(src, bands, window):
    """Read ``(C, h, w)`` float32 from an open rasterio dataset, NaN/inf -> 0.

    The COG contains N/A values, convert to 0
    """
    arr = src.read(bands, window=window).astype("float32")
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)

def read_upsampled_window(src, bands, window, out_size):
    """Bicubic-read a native ``window`` to ``out_size`` px (NaN/inf -> 0)."""
    arr = src.read(
        bands, window=window,
        out_shape=(len(bands), out_size, out_size),
        resampling=Resampling.cubic,
    ).astype("float32")
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)


def standardize(img):
    """Per-image, per-channel standardisation of a ``(C, H, W)`` float array."""
    mean = img.mean(axis=(1, 2), keepdims=True)
    std = img.std(axis=(1, 2), keepdims=True) + 1e-6
    return (img - mean) / std


def apply_norm(img, bands, mean=None, std=None):
    """Standardise a ``(C, h, w)`` array (``C == len(bands)``).

    If no mean or std are given, standardise per-image. 
    Otherwise, use the given per-band mean/std.
    """
    if mean is None or std is None:
        return standardize(img)
    idx = [b - 1 for b in bands]
    m = np.asarray(mean, dtype="float32")[idx].reshape(-1, 1, 1)
    s = np.asarray(std, dtype="float32")[idx].reshape(-1, 1, 1)
    s = np.where(s > 1e-6, s, 1.0)  # guard degenerate/constant bands
    return ((img - m) / s).astype("float32")


def read_split_csv(dataset_dir, split):
    csv = Path(dataset_dir) / "splits" / f"{split}.csv"
    if not csv.exists():
        raise FileNotFoundError(f"Split CSV not found: {csv}")
    return pd.read_csv(csv)