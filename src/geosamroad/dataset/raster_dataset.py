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

from geosamroad.dataset.bands import RGB, HIGH_RES_SOURCE, high_res_path
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
_HIGH_RES_COL = "high_res_path"

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


def _graph_mask(graph_path, src, window, out_size):
    """Rasterise the tile's buffered centrelines over the crop at output resolution.

    ``window`` is in ``src``'s own pixel grid, so the output pixel size is derived
    from the window/out_size ratio. That covers both the upsampled-Sentinel case
    (128px window -> 256px output) and the high-res case (512px window -> 512px
    output, i.e. no resampling at all).
    """
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
    up_tf = transform(window, src.transform) * Affine.scale(window.width / out_size)
    buffered = cand.geometry.buffer(cand[_BUFFER_COL].to_numpy(dtype="float64"))
    mask = features.rasterize(
        ((g, 1) for g in buffered), out_shape=out_shape, transform=up_tf,
        fill=0, all_touched=True, dtype="uint8",
    )
    return (mask > 0).astype("float32")


def nodata_from_image(img):
    """Padding mask for a crop: a pixel is nodata when every band reads 0."""
    return np.all(img == 0, axis=0)



class RasterDatasetConfig(BaseModel):
    dataset_dir: str | Path                     # root dataset directory
    bands: tuple = RGB                          # geotiff index bands to read
    rgb_source: str | None = None               # raw | enhanced | highres

    train_len_factor: float  = 10.0             # dataset length factor (random augmentations)
    min_road_density: float = 0.0               # Image level road density filter
    augment: bool = True                        # d4 flip/rotation augmentation

    crop_size: int = 128                        # patch size for random crop augmentation
    upscale: int = 2                            # upscaling factor for random crop

    # Random crop resampling on low/empty road regions
    min_crop_road_px: int = 0
    max_crop_attempts: int = 20

    # Zero-padded (nodata) imagery, near the edge of the country
    max_crop_nodata_frac: float = 1.0           # reject train crops above this; 1.0 disables
    mask_nodata_labels: bool = True             # zero road/keypoint labels on padded pixels

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
        # high_res_rgb/ already sits at crop_size * upscale px, so its crops are
        # read straight off disk rather than bicubic-upsampled from imagery/.
        self.high_res = self.config.rgb_source == HIGH_RES_SOURCE

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
        if self.high_res:
            df = self._attach_high_res_paths(df)
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

    def _attach_high_res_paths(self, df):
        """Resolve the high_res_rgb/ tile per row, dropping rows with no tile.

        The split CSVs predate high_res_rgb/, so the path is derived from
        image_path rather than read from a column.
        """
        data_dir = Path(self.config.dataset_dir)
        paths = df["image_path"].map(high_res_path)
        present = paths.map(lambda p: (data_dir / p).exists())
        df = df.assign(**{_HIGH_RES_COL: paths})
        if not present.all():
            missing = df.loc[~present, "image_path"].tolist()
            print(f"[{self.split}] high_res_rgb missing for {len(missing)}/{len(df)} tiles, "
                  f"dropping them (e.g. {missing[:3]})", flush=True)
            df = df[present]
        return df

    def __len__(self):
        return self.length

    def _read_crop(self, row, x, y, s, data_dir):
        """Read one crop as (image (C, H, W), road mask (H, W)) at output resolution."""
        win = Window(x, y, s, s)
        if self.high_res:
            # the high-res tile is the native grid scaled by upscale, so the crop
            # window scales with it and the pixels are read as-is -- no resampling
            u = self.config.upscale
            hr_win = Window(x * u, y * u, s * u, s * u)
            with rasterio.open(data_dir / row[_HIGH_RES_COL]) as src:
                return (read_window(src, self.bands, hr_win),
                        _graph_mask(data_dir / row["mask_graph_path"], src, hr_win,
                                    self.upscaled_image_size))
        if self.config.upscale == 1:
            with rasterio.open(data_dir / row["image_path"]) as src, \
                    rasterio.open(data_dir / row["mask_path"]) as msrc:
                return (read_window(src, self.bands, win),                  # (C, h, w)
                        (msrc.read(1, window=win) > 0).astype("float32"))   # (h, w)
        with rasterio.open(data_dir / row["image_path"]) as src:
            return (read_upsampled_window(src, self.bands, win, self.upscaled_image_size),
                    _graph_mask(data_dir / row["mask_graph_path"], src, win,
                                self.upscaled_image_size))

    def _sample_train_crop(self, patch_size, data_dir):
        """Random crops during training, resampled off road-free and padded regions.

        The padding test reuses the crop that was read rather than probing the tile
        first, so a clean crop -- almost all of them -- costs a single read.
        """
        thr = self.config.min_crop_road_px
        nodata_thr = self.config.max_crop_nodata_frac
        check_nodata = self.high_res and nodata_thr < 1.0
        attempts = self.config.max_crop_attempts if (thr > 0 or check_nodata) else 1
        for attempt in range(attempts):
            image_idx, x, y, s = train_crop_bounds(patch_size, self.image_size, len(self.df))
            row = self.df.iloc[image_idx]
            last = attempt == attempts - 1  # out of attempts: take this crop as-is
            if thr > 0 and not last:
                with rasterio.open(data_dir / row["mask_path"]) as m:
                    road_px = int((m.read(1, window=Window(x, y, patch_size, patch_size)) > 0).sum())
                if road_px < thr:
                    continue
            img, mask = self._read_crop(row, x, y, s, data_dir)
            if check_nodata and not last and nodata_from_image(img).mean() > nodata_thr:
                continue
            return image_idx, x, y, s, img, mask
        raise RuntimeError("unreachable: the last crop attempt always returns")

    def __getitem__(self, idx):
        """
        Random crop and normalise. Upscaled (bicubic) random crops of images have road masks re-rastered from road vectors with buffer."""

        patch_size = self.config.crop_size
        output = {}
        data_dir = Path(self.config.dataset_dir)

        # Bound calculation
        if self.is_train:
            image_idx, x, y, s, img, mask = self._sample_train_crop(patch_size, data_dir)
        else:
            image_idx, x, y, s = eval_crop_bounds(idx, patch_size, self.image_size)
            img, mask = self._read_crop(self.df.iloc[image_idx], x, y, s, data_dir)
        row = self.df.iloc[image_idx]
        win = Window(x, y, s, s)

        if self.debug:
            c, h, w = img.shape
            if (h, w) != (patch_size, patch_size) and self.config.upscale == 1: # image check
                raise ValueError(f"Unexpected crop size {h}x{w} from tile {row['image_path']} at {win}")
            elif (h, w) != (patch_size * self.config.upscale, patch_size * self.config.upscale) and self.config.upscale > 1: # image check
                raise ValueError(f"Unexpected crop size {h}x{w} from tile {row['image_path']} at {win} with upscale {self.config.upscale}")

        if self.config.augment and self.is_train:
            img, mask, augment_applied = d4_augment(img, mask) # flip / rotation
            output.update(augment_applied)

        # Imagery near the edge of the country is zero-padded, but the labels are
        # rasterised from vectors that do cover it. Derive the padding mask from the
        # (already augmented) crop so it lines up, and drop those labels: there is no
        # visual evidence of a road under the padding.
        nodata = (nodata_from_image(img) if self.high_res
                  else np.zeros(mask.shape, dtype=bool))
        if self.config.mask_nodata_labels:
            mask = np.where(nodata, 0.0, mask).astype("float32", copy=False)

        if self.config.normalize:
            img = apply_norm(img, self.bands, self.config.norm_mean, self.config.norm_std)
        image = torch.from_numpy(np.ascontiguousarray(img))
        mask = torch.from_numpy(np.ascontiguousarray(mask))

        output.update({
            "image": image,
            "mask": mask.squeeze(0).long(),  # (H, W)
            "nodata": torch.from_numpy(np.ascontiguousarray(nodata)),
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