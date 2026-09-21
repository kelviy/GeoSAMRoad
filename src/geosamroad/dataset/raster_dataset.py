"""Custom Raster Dataset Loader"""
import lightning.pytorch as pl
import pandas as pd
from pandas import DataFrame
from typing import cast
from torch.utils.data import DataLoader, Dataset
import torch
import rasterio
from rasterio import Affine, features
from rasterio.windows import Window, bounds, transform
import numpy as np
import geopandas as gpd
from pathlib import Path
from pydantic import BaseModel
from functools import lru_cache

from geosamroad.dataset.bands import RGB
from geosamroad.dataset.helper import (
    apply_norm, 
    d4_augment,
    read_window,
    read_upsampled_window,
    read_split_csv,
    train_crop_bounds,
    eval_crop_bounds,
)
from sentinel2data.generator.helper import window_bounds

_BUFFER_COL = "buffer"

FINAL_SPLIT_SOURCES = {
    "train": ("train", "val"),
    "val": ("test",),
    "test": ("test",),
}

# Only used during upsampling
@lru_cache(maxsize=256)
def _load_graph(path_str):
    """Cached road vector graphs per image"""
    return gpd.read_parquet(path_str)


def _graph_mask(graph_path, src, window, out_size, upscale):
    """Rasterise the tile's buffered centrelines over the crop at output resolution. For upscale version."""
    out_shape = (out_size, out_size)
    roads = _load_graph(str(graph_path))
    if roads.empty:
        return np.zeros(out_shape, dtype="float32")
    if _BUFFER_COL not in roads.columns:
        raise ValueError(
            f"masks_graph parquet {graph_path} lacks a '{_BUFFER_COL}' column; "
            "regenerate the dataset with the current road-graph labeler."
        )
    minx, miny, maxx, maxy = window_bounds(window, src.transform)
    cand = roads.cx[minx:maxx, miny:maxy]
    if cand.empty:
        return np.zeros(out_shape, dtype="float32")
    up_tf = transform(window, src.transform) * Affine.scale(1.0 / upscale)
    buffered = cand.geometry.buffer(cand[_BUFFER_COL].to_numpy(dtype="float64"))
    mask = features.rasterize(
        ((g, 1) for g in buffered), out_shape=out_shape, transform=up_tf,
        fill=0, all_touched=True, dtype="uint8",
    )
    return (mask > 0).astype("float32")



class RasterDatasetConfig(BaseModel):
    dataset_dir: str | Path                     # root dataset directory 
    bands: tuple = RGB                          # geotiff index bands to read

    train_len_factor: float  = 10.0             # dataset length factor (random augmentations)
    min_road_density: float = 0.0               # Image level road density filter
    augment: bool = True                        # d4 flip/rotation augmentation

    crop_size: int = 128                        # patch size for random crop augmentation
    upscale: int = 2                            # upscaling factor for random crop

    # Random crop resampling on low/empty road regions
    min_crop_road_px: int = 0
    max_crop_attempts: int = 20

    normalize: bool = False
    norm_mean: list[float] | None = None            # precomputed dataset stats
    norm_std: list[float] | None = None

    final_train: bool = False

class RasterRoadDataset(Dataset):
    """
    Train: Random pixel crops from the 512x512 tiles.
    Eval: Sliding window crops with no overlap.

    Length by default is 10 * len(train_tiles)
    """

    def __init__(self, config: RasterDatasetConfig, split: str, debug: bool = False):
        self.config = config
        self.debug = debug

        self.split = split
        if self.split == "train":
            self.is_train = True
        elif self.split in ["val", "test"]:
            self.is_train = False
        else:
            raise ValueError("split value should be `train`, `val` or `test`")
        
        self.image_size = 512
        self.non_overlapping_patch_per_tile = int((self.image_size // self.config.crop_size) ** 2)

        self.bands = list(self.config.bands)
        self.upscaled_image_size = self.config.crop_size * self.config.upscale

        # Read list of images in dataset 
        sources = FINAL_SPLIT_SOURCES[self.split] if self.config.final_train else (self.split,)
        df = pd.concat(
            [read_split_csv(self.config.dataset_dir, s) for s in sources],
            ignore_index=True,
        )
        if self.config.final_train:
            print(f"[final] {self.split} split <- "
                  f"{' + '.join(s + '.csv' for s in sources)} ({len(df)} tiles)", flush=True)

        min_road_density = self.config.min_road_density
        if min_road_density > 0 and "road_density" in df.columns:
            n0 = len(df)
            df = df[df["road_density"] >= min_road_density]
            print(f"Filter road_density operation with min-threshold ({min_road_density}): "
                  f"kept {len(df)}/{n0} tiles")
        self.df: DataFrame = cast(DataFrame, df.reset_index(drop=True))
        if self.df.empty:
            raise ValueError("No train tiles left after the min_road_density filter.")

        if self.is_train:
            # length depends by factor * number of tiles
            length_factor = self.config.train_len_factor
            self.length: int = int(length_factor * len(self.df))
        else:
            self.length: int = int(len(self.df) * self.non_overlapping_patch_per_tile)


        # check for normalisation config given
        if self.config.normalize and (self.config.norm_mean is None or self.config.norm_std is None):
            raise ValueError("No frozen train stats given")

        if self.config.upscale < 1:
            raise ValueError("Upscale factor must be >= 1")

    def __len__(self):
        return self.length

    def _sample_train_crop(self, patch_size, data_dir):
        """Random crops during training"""
        thr = self.config.min_crop_road_px
        attempts = self.config.max_crop_attempts if thr > 0 else 1
        for _ in range(attempts):
            image_idx, x, y, s = train_crop_bounds(patch_size, self.image_size, len(self.df))
            if thr <= 0:
                return image_idx, x, y, s
            row = self.df.iloc[image_idx]
            with rasterio.open(data_dir / row["mask_path"]) as m:
                road_px = int((m.read(1, window=Window(x, y, patch_size, patch_size)) > 0).sum())
            if road_px >= thr:
                return image_idx, x, y, s
        return image_idx, x, y, s  # utilise last crop

    def __getitem__(self, idx):
        """
        Random crop and normalise. Upscaled (bicubic) random crops of images have road masks re-rastered from road vectors with buffer."""

        patch_size = self.config.crop_size
        output = {}
        data_dir = Path(self.config.dataset_dir)

        # Bound calculation
        if self.is_train:
            image_idx, x, y, s = self._sample_train_crop(patch_size, data_dir)
        else:
            image_idx, x, y, s = eval_crop_bounds(idx, patch_size, self.image_size)
        row = self.df.iloc[image_idx]
        win = Window(x, y, s, s)

        # reading image (upscale if specified)
        if self.config.upscale == 1:
            with rasterio.open(data_dir / row["image_path"]) as src, \
                    rasterio.open(data_dir / row["mask_path"]) as msrc:
    
                img = read_window(src, self.bands, win)                 # (C, h, w)
                mask = (msrc.read(1, window=win) > 0).astype("float32")  # (h, w)
        else:
            with rasterio.open(data_dir / row["image_path"]) as src:
                img = read_upsampled_window(src, self.bands, win, self.upscaled_image_size)
                mask = _graph_mask(
                    data_dir / row["mask_graph_path"], src, win,
                    self.upscaled_image_size, self.config.upscale,
                )

        if self.debug: 
            c, h, w = img.shape
            if (h, w) != (patch_size, patch_size) and self.config.upscale == 1: # image check
                raise ValueError(f"Unexpected crop size {h}x{w} from tile {row['image_path']} at {win}")
            elif (h, w) != (patch_size * self.config.upscale, patch_size * self.config.upscale) and self.config.upscale > 1: # image check
                raise ValueError(f"Unexpected crop size {h}x{w} from tile {row['image_path']} at {win} with upscale {self.config.upscale}")

        if self.config.augment and self.is_train:
            img, mask, augment_applied = d4_augment(img, mask) # flip / rotation
            output.update(augment_applied)
        if self.config.normalize:
            img = apply_norm(img, self.bands, self.config.norm_mean, self.config.norm_std)
        image = torch.from_numpy(np.ascontiguousarray(img))
        mask = torch.from_numpy(np.ascontiguousarray(mask))

        output.update({
            "image": image,
            "mask": mask.squeeze(0).long(),  # (H, W)
            "image_idx": image_idx,
            "crop_x": int(x),
            "crop_y": int(y),
        })

        return output


class RasterRoadDataModule(pl.LightningDataModule):
    """Train - random native crops; val/test - deterministic non-overlapping crops."""

    def __init__(self, dataset_config: RasterDatasetConfig, batch_size: int = 16, num_workers: int = 2):
        super().__init__()
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.dataset_config = dataset_config

    def _dataloader(self, split):
        ds = RasterRoadDataset(self.dataset_config, split, debug=True)

        drop_last = True if split == "train" else False
        
        return DataLoader(
            ds,
            batch_size=self.batch_size,
            shuffle=False,
            num_workers=self.num_workers,
            pin_memory=torch.cuda.is_available(),
            persistent_workers=self.num_workers > 0,
            drop_last=drop_last,
        )

    def train_dataloader(self):
        return self._dataloader("train")

    def val_dataloader(self):
        return self._dataloader("val")

    def test_dataloader(self):
        return self._dataloader("test")