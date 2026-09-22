"""SAM-Road dataset for ROSA: raster crop + keypoint mask + graph topo labels."""
from pathlib import Path
import pickle
import cv2
import numpy as np
import torch
import rasterio
from rasterio.windows import Window
import lightning.pytorch as pl
from pydantic import model_validator
from torch.utils.data import DataLoader

from geosamroad.dataset.bands import resolve_input
from geosamroad.dataset.raster_dataset import (
    RasterDatasetConfig,
    RasterRoadDataset,
    _load_graph,
)
from geosamroad.dataset.graph_dataset import (
    GraphLabelGenerator,
    GraphLabelGeneratorConfig,
    graph_collate_fn,
)
from geosamroad.dataset.bands import HIGH_RES_SOURCE
from geosamroad.dataset.helper import read_window
from sentinel2data.generator.artifacts import BORDER_EPS


class SamRoadDatasetConfig(RasterDatasetConfig):
    """Config for a sam_road dataset, with graph labels."""
    DATASET: str = "ROSA"
    TOPO_SAMPLE_NUM: int = 512
    TOPONET_VERSION: str = "normal"
    ROAD_NMS_RADIUS: int = 16
    NEIGHBOR_RADIUS: int = 64
    MAX_NEIGHBOR_QUERIES: int = 16
    ITSC_NMS_RADIUS: int = 8            # not sure, seems to be only used during inference
    SUBDIVIDE_RESOLUTION: int = 4       # graph label constants, see graph_dataset
    CROSSOVER_EXCLUDE_RADIUS: int = 4
    INTERESTING_RADIUS: int = 32
    NOISE_SCALE: float = 1.0
    keypoint_buffer_m: float = 10.0     # keypoint disk radius (m) for upscale re-rasterisation
    RGB_INPUT: bool = False
    preload_graphs: bool = False        # build and cache all graph generators before training
    # patch_size config from SAMRad is calculated from crop size*upsample  
    model_encoder: str = "sam"          # sam, terramind, unet, sgcn
    # Model encoder in dataset config is autoset/linked from model config

    @model_validator(mode="after")
    def _resolve_bands(self):
        """Read bands from prespecified bands for each model in bands.py"""
        if self.rgb_source is None:
            self.rgb_source = "enhanced"
        bands = resolve_input(self.model_encoder, self.RGB_INPUT, self.rgb_source)
        self.bands = tuple(bands)
        return self


def upscale_adj_graph_list(graph: dict, upscale: int) -> dict:
    """Scale graph to specified upscale value"""
    if upscale == 1:
        return graph
    return {
        (kx * upscale, ky * upscale): [(nx * upscale, ny * upscale) for nx, ny in nbrs]
        for (kx, ky), nbrs in graph.items()
    }


def _identity_coords(v):
    """Function to represent identity transformation"""
    return v


def _empty_topo(cfg: GraphLabelGeneratorConfig):
    """Topo labels for empty tile"""
    q = cfg.MAX_NEIGHBOR_QUERIES
    fake_points = np.array([[0.0, 0.0]], dtype=np.float32)
    fake_sample = ([[0, 0]] * q, [False] * q, [False] * q)
    return fake_points, [fake_sample] * cfg.TOPO_SAMPLE_NUM


class SAMROAD_Dataset(RasterRoadDataset):
    NATIVE_RES_M = 10.0  # Sentinel imagery spatial resolution
    
    def __init__(self, config: SamRoadDatasetConfig, split: str, dev_run: bool = False):
        super().__init__(config, split)
        self.samroad_config = config

        if config.RGB_INPUT and len(self.bands) != 3:
            raise ValueError(
                f"rgb needs exactly 3 bands (e.g. ENHANCED_RGB), got {self.bands}")

        if dev_run:
            self.df = self.df.head(4).reset_index(drop=True)
            self.length = (
                int(self.config.train_len_factor * len(self.df))
                if self.is_train
                else len(self.df) * self.non_overlapping_patch_per_tile
            )

        self.graph_config = GraphLabelGeneratorConfig(
            PATCH_SIZE=self.upscaled_image_size,   # patch edge = crop_size * upscale
            ROAD_NMS_RADIUS=config.ROAD_NMS_RADIUS,
            NEIGHBOR_RADIUS=config.NEIGHBOR_RADIUS,
            TOPO_SAMPLE_NUM=config.TOPO_SAMPLE_NUM,
            MAX_NEIGHBOR_QUERIES=config.MAX_NEIGHBOR_QUERIES,
            SUBDIVIDE_RESOLUTION=config.SUBDIVIDE_RESOLUTION,
            CROSSOVER_EXCLUDE_RADIUS=config.CROSSOVER_EXCLUDE_RADIUS,
            INTERESTING_RADIUS=config.INTERESTING_RADIUS,
            NOISE_SCALE=config.NOISE_SCALE,
        )
        self.coord_transform = _identity_coords
        self._gen_cache: dict = {}

        # Prebuild and cache graph generators
        if config.preload_graphs:
            data_dir = Path(config.dataset_dir)
            print(f"[preload] building {len(self.df)} graph generators ({split}) ...", flush=True)
            for row in self.df.itertuples(index=False):
                self._get_generator(str(data_dir / row.mask_adj_graph_path)) # sam-road graph generators 
                if config.upscale > 1:
                    _load_graph(str(data_dir / row.mask_graph_path)) # upscale road graph masks parquets
            n_ready = sum(g is not None for g in self._gen_cache.values())
            print(f"[preload] done: {n_ready}/{len(self._gen_cache)} non-empty.", flush=True)

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_gen_cache"] = {}
        return state

    def _keypoint_disks(self, gen, ox: int, oy: int, out: int) -> np.ndarray:
        """Rerasterise keypoint points. Note that keypoints at the edges are ignored"""
        kp = np.zeros((out, out), dtype="uint8")
        if gen is None:
            return kp
        g = gen.full_graph_origin
        pts = np.asarray(g.vs["point"], dtype="float64").reshape(-1, 2)  # upscaled px
        deg = np.asarray(g.degree())
        # Border filter 
        native = pts / self.config.upscale
        on_border = (
            (native[:, 0] <= BORDER_EPS) | (native[:, 0] >= self.image_size - 1 - BORDER_EPS)
            | (native[:, 1] <= BORDER_EPS) | (native[:, 1] >= self.image_size - 1 - BORDER_EPS)
        )
        res_up = self.NATIVE_RES_M / self.config.upscale  # m/px at output resolution
        radius = max(1, round(self.samroad_config.keypoint_buffer_m / res_up))
        for x, y in pts[(deg != 2) & ~on_border] - np.array([ox, oy]):
            cv2.circle(kp, (int(round(x)), int(round(y))), radius, 1, -1)
        return kp

    def _get_generator(self, adj_path: str):
        """ Guards against empty graphs and caches generator."""
        if adj_path not in self._gen_cache:
            with open(adj_path, "rb") as f:
                adj = pickle.load(f)
            if len(adj) == 0:
                self._gen_cache[adj_path] = None
            else:
                adj = upscale_adj_graph_list(adj, self.config.upscale)
                self._gen_cache[adj_path] = GraphLabelGenerator(
                    self.graph_config, adj, self.coord_transform
                )
        return self._gen_cache[adj_path]

    def __getitem__(self, idx):
        raster = super().__getitem__(idx)  # image, mask, crop_x/y, image_idx, rot_k/flip
        image = raster["image"]
        road_mask = raster["mask"].to(torch.float32)
        row = self.df.iloc[raster["image_idx"]]

        # Apply same augumentations from raster
        k = raster.get("rot_k", 0)
        flip = raster.get("flip", False)

        u = self.config.upscale
        out = self.upscaled_image_size
        data_dir = Path(self.config.dataset_dir)
        cx, cy = raster["crop_x"], raster["crop_y"]

        gen = self._get_generator(str(data_dir / row["mask_adj_graph_path"]))
        x0, y0 = cx * u, cy * u

        # Keypoint mask
        if u == 1:
            win = Window(cx, cy, self.config.crop_size, self.config.crop_size)
            with rasterio.open(data_dir / row["mask_keypoint_path"]) as ksrc:
                kp = (read_window(ksrc, [1], win)[0] > 0).astype("float32")
        else:
            kp = self._keypoint_disks(gen, x0, y0, out).astype("float32")
        if k:
            kp = np.rot90(kp, k, axes=(0, 1))
        if flip:
            kp = np.flip(kp, axis=1)
        # same treatment as the road mask: no keypoint labels over zero-padded imagery
        if self.config.mask_nodata_labels:
            kp = np.where(raster["nodata"].numpy(), 0.0, kp).astype("float32", copy=False)
        keypoint_mask = torch.from_numpy(np.ascontiguousarray(kp))

        # Graph topo labels
        patch = ((x0, y0), (x0 + out, y0 + out))
        if gen is None:
            graph_points, topo_samples = _empty_topo(self.graph_config)
        else:
            graph_points, topo_samples = gen.sample_patch(patch, k, flip)
        pairs, connected, valid = zip(*topo_samples)

        out = {
            "keypoint_mask": keypoint_mask,
            "road_mask": road_mask,
            "graph_points": torch.tensor(np.ascontiguousarray(graph_points), dtype=torch.float32),
            "pairs": torch.tensor(pairs, dtype=torch.int32),
            "connected": torch.tensor(connected, dtype=torch.bool),
            "valid": torch.tensor(valid, dtype=torch.bool),
        }
        
        out["image"] = image
        return out


class SamRoadDataModule(pl.LightningDataModule):
    def __init__(self, config: SamRoadDatasetConfig, batch_size: int = 16,
                 num_workers: int = 2, dev_run: bool = False):
        super().__init__()
        self.config = config
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.dev_run = dev_run

    def _dataloader(self, split):
        ds = SAMROAD_Dataset(self.config, split, dev_run=self.dev_run)
        return DataLoader(
            ds,
            batch_size=self.batch_size,
            shuffle=False,  
            num_workers=self.num_workers,
            pin_memory=torch.cuda.is_available(),
            persistent_workers=self.num_workers > 0,
            drop_last=(split == "train"),
            collate_fn=graph_collate_fn,  
        )

    def train_dataloader(self):
        return self._dataloader("train")

    def val_dataloader(self):
        return self._dataloader("val")

    def test_dataloader(self):
        return self._dataloader("test")
