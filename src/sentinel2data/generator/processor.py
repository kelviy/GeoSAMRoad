"""Processes one COG image into outputs per tile (image, mask, graph, adjacency, keypoints)"""
import math
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.windows import transform as window_transform
from shapely.geometry import box

from geosamroad.dataset.bands import ENHANCED_RGB_BANDS, GEE_GLOBALURBANMAPPER_BANDS
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
    TILE_STATUS_COLUMNS,
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
    exclude_road_classes,
    load_zone_roads,
)
from sentinel2data.generator.naming import ZoneMetadata


@dataclass
class ZoneResult:
    """What processing one zone produced."""
    rows: list                      # metadata rows of kept tiles
    tile_status: gpd.GeoDataFrame   # one row per tile position, zone CRS
    excluded: dict = field(default_factory=dict)  # road class -> {segments, length_m}
    band_names: list = field(default_factory=list)  # written band order


def zone_band_names(img) -> list:
    """Names of a zone COG's bands, mapped by position onto the GEE layout.

    Zone COGs carry their own band descriptions (e.g. ``B4`` not ``B4_R``), so
    names are positional. Where the file does name a band, the S1 bands must
    agree, so a reordered source can't silently drop the wrong bands.
    """
    names = list(GEE_GLOBALURBANMAPPER_BANDS)
    if img.count != len(names):
        raise ValueError(
            f"{img.name}: {img.count} bands, expected the {len(names)}-band GEE layout"
        )
    for i, (own, expected) in enumerate(zip(img.descriptions, names), start=1):
        if own and own.startswith(("VV_", "VH_")) and own != expected:
            raise ValueError(f"{img.name}: band {i} is {own!r}, expected {expected!r}")
    return names


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
                 enhance_cfg=None, excluded_road_classes=(), dropped_bands=()):
        self.roads_parquet_path = Path(roads_parquet_path)
        self.dropped_bands = tuple(dropped_bands)
        self.excluded_road_classes = tuple(excluded_road_classes)
        self.tile_spec = tile_spec or TileSpec()
        self.mask_labeler = mask_labeler or RasterMaskLabeler()
        self.enhance_cfg = enhance_cfg or RGBEnhanceConfig()

    def process(self, zone_id, sat_cog_path, split, zone_meta: ZoneMetadata, paths) -> ZoneResult:
        """One zone COG -> per-tile outputs, metadata rows and its tile-status table."""
        sat_cog_path = Path(sat_cog_path)
        print(f"[{zone_id}] {zone_meta.zone_filename} ({zone_meta.zone_name}) -> {split}")
        layout = paths.split_dirs(split)

        # Load the zone's roads and rasterize to a full-zone mask
        zone = load_zone_roads(sat_cog_path, self.roads_parquet_path)
        zone, excluded = exclude_road_classes(zone, self.excluded_road_classes)
        full_mask = self.mask_labeler.rasterize(zone)  # (H, W)
        sindex = zone.roads.sindex if not zone.roads.empty else None

        # Tiles zone
        with rasterio.open(sat_cog_path) as img:
            result = self._tile_zone(
                zone, sindex, img, full_mask, zone_meta, split, layout, paths
            )
        result.excluded = excluded
        return result

    def _tile_zone(self, zone, sindex, img, full_mask, zone_meta: ZoneMetadata, split, layout, paths):
        """Tiles into 512px tiles and writes the five per-tile artifacts.

        Returns the kept tiles' metadata rows and a status for every tile
        position, including the partial edge positions that are never tiled.
        """
        ts = self.tile_spec.tile_size
        patch = self.tile_spec.patch_size

        # Bands to read and write, by name
        src_names = zone_band_names(img)
        kept = [n for n in src_names if n not in self.dropped_bands]
        read_idx = [src_names.index(n) + 1 for n in kept]
        rgb_idx = [kept.index(src_names[b - 1]) for b in self.enhance_cfg.rgb_bands]
        nan_exempt = [i for i, n in enumerate(kept) if n in NAN_EXEMPT_BANDS]
        band_names = kept + list(ENHANCED_RGB_BANDS)
        n_rows = img.height // ts  # full tiles only -> edge remainder untiled
        n_cols = img.width // ts
        n_row_positions = math.ceil(img.height / ts)  # includes the partial edge
        n_col_positions = math.ceil(img.width / ts)

        rows, statuses = [], []
        for r in range(n_row_positions):
            for c in range(n_col_positions):
                stem = f"{zone_meta.zone_filename}_r{r}_c{c}"
                win = Window(c * ts, r * ts, min(ts, img.width - c * ts), min(ts, img.height - r * ts))
                status = {
                    "tile_id": stem, "zone_filename": zone_meta.zone_filename,
                    "zone_name": zone_meta.zone_name, "split_set": split,
                    "row": r, "col": c, "geometry": window_box(win, img.transform),
                }
                statuses.append(status)
                if r >= n_rows or c >= n_cols:
                    status["status"] = "untiled"
                    continue

                mask_arr = full_mask[r * ts : (r + 1) * ts, c * ts : (c + 1) * ts]
                keep, road_px, img_arr = self._tile_verdict(
                    mask_arr, lambda w=win: img.read(read_idx, window=w).astype("float32"),
                    nan_exempt=nan_exempt,
                )
                if keep != True: # skip tile
                    status["status"] = "dropped_no_road" if road_px == 0 else "dropped_nan"
                    continue
                status["status"] = "kept"

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
                img_path = layout["imagery"] / f"{stem}.tif"
                msk_path = layout["masks_raster"] / f"{stem}.tif"
                gph_path = layout["masks_graph"] / f"{stem}.parquet"
                adj_path = layout["mask_adj_graphs"] / f"{stem}.pkl"
                kpt_path = layout["mask_keypoints"] / f"{stem}.tif"
                write_image_cog(img_path, out, img.profile, transform=win_tf, blockxsize=patch, blockysize=patch, band_names=band_names)
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

        tile_status = gpd.GeoDataFrame(
            statuses, columns=TILE_STATUS_COLUMNS + ["geometry"], geometry="geometry", crs=img.crs,
        )
        return ZoneResult(rows=rows, tile_status=tile_status, band_names=band_names)

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

    
    def _tile_verdict(self, mask_arr, read_window, nan_exempt=()):
        """Check tile drop criteria (no roads, na).

        ``nan_exempt`` holds 0-based positions in the read array that may be NaN.

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
        if nan_exempt:
            keep = np.ones(img_arr.shape[0], dtype=bool)
            keep[list(nan_exempt)] = False
            checked = img_arr[keep]
        if np.isnan(checked).any():
            return False, road_px, img_arr # skip na
        return True, road_px, img_arr
