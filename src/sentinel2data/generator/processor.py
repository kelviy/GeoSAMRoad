"""Processes one COG image into outputs per tile (image, mask, graph, adjacency, keypoints)"""
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.windows import transform as window_transform
from shapely.geometry import box

from geosamroad.dataset.bands import ROSA_BANDS
from sentinel2data.generator.artifacts import (
    keypoint_mask_from_adjacency,
    native_adjacency,
)
from sentinel2data.generator.config import (
    EMPTY_LABEL,
    HARD_CODE_DATE_RANGE,
    NAN_EXEMPT_BANDS,
    ROSA_SCHEMA,
    WGS84,
    RGBEnhanceConfig,
    TileSpec,
)
from sentinel2data.generator.helper import (
    enhance_rgb,
    reproject_bounds,
    window_bounds,
    window_box,
)
from sentinel2data.generator.io import (
    write_image_cog,
    write_mask_cog,
    write_pickle,
    write_roads_parquet,
)
from sentinel2data.generator.labels import (
    RasterMaskLabeler,
    RoadGraphLabeler,
    load_zone_roads,
)
from sentinel2data.generator.naming import ZoneMetadata


def _rel(root, path):
    """Relative path to dataset root"""
    path = Path(path)
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


class ROSAProcessor:
    """Creates a non-overlapping 512x512px tiled grid per zone COG image. 

    Tile artifacts:
        - Tiled Satellite COG imagery + enhanced-RGB bands (CLAHE+gamma).
        - Raster Road Mask
        - Road Vector Centerlines (GeoParquet)
        - Road Adjacency Dict / List in pixel coordinates
        - Intersection / endpoint masks
        - Metadata per tile
    """

    schema = ROSA_SCHEMA

    def __init__(self, roads_parquet_path, tile_spec=None, mask_labeler=None,
                 enhance_cfg=None):
        self.roads_parquet_path = Path(roads_parquet_path)
        self.tile_spec = tile_spec or TileSpec()
        self.mask_labeler = mask_labeler or RasterMaskLabeler()
        self.enhance_cfg = enhance_cfg or RGBEnhanceConfig()

    def process(self, zone_id, sat_cog_path, split, zone_meta: ZoneMetadata, paths):
        """One zone COG -> per-tile outputs and metadata rows, plus its tile stats."""
        sat_cog_path = Path(sat_cog_path)
        print(f"[{zone_id}] {zone_meta.zone_filename} ({zone_meta.zone_name}) -> {split}")
        layout = paths.split_dirs(split)

        # Load the zone's roads and rasterize to a full-zone mask
        zone = load_zone_roads(sat_cog_path, self.roads_parquet_path)
        full_mask = self.mask_labeler.rasterize(zone)  # (H, W)
        sindex = zone.roads.sindex if not zone.roads.empty else None

        # Tiles zone
        with rasterio.open(sat_cog_path) as img:
            return self._tile_zone(
                zone, sindex, img, full_mask, zone_meta, split, layout, paths
            )

    def _tile_zone(self, zone, sindex, img, full_mask, zone_meta: ZoneMetadata, split, layout, paths):
        "Tiles into 512px tiles, writes the five per-tile artifacts, returns rows + stats."
        ts = self.tile_spec.tile_size
        patch = self.tile_spec.patch_size
        rgb_idx = [b - 1 for b in self.enhance_cfg.rgb_bands]
        n_rows = img.height // ts  # full tiles only -> edge remainder dropped
        n_cols = img.width // ts

        rows = []
        for r in range(n_rows):
            for c in range(n_cols):
                win = Window(c * ts, r * ts, ts, ts)
                mask_arr = full_mask[r * ts : (r + 1) * ts, c * ts : (c + 1) * ts]

                keep, road_px, img_arr = self._tile_verdict(
                    mask_arr, lambda w=win: img.read(window=w).astype("float32"),
                    nan_exempt_bands=NAN_EXEMPT_BANDS,
                )
                if keep != True: # skip tile
                    continue

                # tile imagery + enhance RGB
                win_tf = window_transform(win, img.transform)
                enhanced = enhance_rgb(img_arr[rgb_idx], self.enhance_cfg)
                out = np.concatenate([img_arr, enhanced], axis=0)  # (C+3, ts, ts)

                # graph artifacts
                tile_roads = RoadGraphLabeler._clip_roads(
                    zone.roads, window_box(win, img.transform), sindex
                )
                adj = {} if tile_roads.empty else native_adjacency(tile_roads, win_tf)
                keypoint_mask = keypoint_mask_from_adjacency(adj, ts)

                # write outputs
                stem = f"{zone_meta.zone_filename}_r{r}_c{c}"
                img_path = layout["imagery"] / f"{stem}.tif"
                msk_path = layout["masks_raster"] / f"{stem}.tif"
                gph_path = layout["masks_graph"] / f"{stem}.parquet"
                adj_path = layout["mask_adj_graphs"] / f"{stem}.pkl"
                kpt_path = layout["mask_keypoints"] / f"{stem}.tif"
                write_image_cog(img_path, out, img.profile, transform=win_tf, blockxsize=patch, blockysize=patch, band_names=list(ROSA_BANDS))
                write_mask_cog(msk_path, mask_arr, img.meta, transform=win_tf, blockxsize=patch, blockysize=patch)
                write_roads_parquet(gph_path, tile_roads)
                write_pickle(adj_path, adj)
                write_mask_cog(kpt_path, keypoint_mask, img.meta, transform=win_tf, blockxsize=patch, blockysize=patch)

                # generate metadata
                rows.append(self._row(
                    paths=paths, zone_meta=zone_meta, split=split,
                    img_path=img_path, msk_path=msk_path, gph_path=gph_path,
                    adj_path=adj_path, kpt_path=kpt_path,
                    bounds=window_bounds(win, img.transform), src_crs=img.crs,
                    res=abs(win_tf.a), total_px=ts * ts, road_px=road_px,
                    tile_size=ts, band_count=out.shape[0],
                ))

        return rows

    def _row(self, *, paths, zone_meta, split, img_path, msk_path, gph_path,
             adj_path, kpt_path, bounds, src_crs, res, total_px, road_px,
             tile_size, band_count):
        west, south, east, north = reproject_bounds(bounds, src_crs, WGS84)
        return {
            "image_id": -1,  # assigned by the split-first pipeline
            "zone_name": zone_meta.zone_name,
            "split_set": split,
            "image_path": _rel(paths.root, img_path),
            "mask_path": _rel(paths.root, msk_path),
            "mask_graph_path": _rel(paths.root, gph_path),
            "mask_adj_graph_path": _rel(paths.root, adj_path),
            "mask_keypoint_path": _rel(paths.root, kpt_path),
            "tile_size": tile_size,
            "band_count": band_count,
            "spatial_resolution": res,
            "road_pixels": road_px,
            "road_density": road_px / total_px,
            "road_density_classification": EMPTY_LABEL,  # set over the whole dataset by the tagger
            "urbanisation_classification": zone_meta.urban_class,
            "biome": zone_meta.biome,
            "satellite_image_dates": list(HARD_CODE_DATE_RANGE),
            "crs": src_crs.to_string(),
            "geometry": box(west, south, east, north),
        }

    
    def _tile_verdict(self, mask_arr, read_window, nan_exempt_bands=()):
        """Check tile drop criteria (no roads, na). 
        
        Returns: 
            - Drop / Not drop (bool)
            - road_px count
            - tiled image
        """
        road_px = int(np.count_nonzero(mask_arr > 0))
        if road_px == 0:
            return False, road_px, None # skip no roads
    
        img_arr = read_window()
        checked = img_arr
        if nan_exempt_bands:
            keep = np.ones(img_arr.shape[0], dtype=bool)
            keep[[b - 1 for b in nan_exempt_bands if 0 < b <= img_arr.shape[0]]] = False
            checked = img_arr[keep]
        if np.isnan(checked).any():
            return False, road_px, img_arr # skip na
        return True, road_px, img_arr
    

