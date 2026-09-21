"""Generates label masks in a per-zone basis"""
from dataclasses import dataclass
from pathlib import Path
import geopandas as gpd
import numpy as np
import rasterio
from rasterio import features
from shapely.geometry import box
from sentinel2data.generator.config import WGS84
from sentinel2data.generator.io import write_mask_cog
from abc import ABC, abstractmethod


@dataclass
class ZoneRoads:
    """Stores clipped roads and the image metadata"""

    roads: gpd.GeoDataFrame
    image_metadata: dict  # {meta, crs, bounds, blockxsize, blockysize}

    @property
    def crs(self):
        return self.image_metadata["crs"]

    @property
    def is_empty(self) -> bool:
        return self.roads.empty


def _read_image_metadata(image_cog_path) -> dict:
    with rasterio.open(image_cog_path) as src:
        return {
            "meta": src.meta.copy(),
            "crs": src.crs,
            "bounds": src.bounds,
            "blockxsize": src.profile.get("blockxsize", 256),
            "blockysize": src.profile.get("blockysize", 256),
        }


def load_zone_roads(sat_cog_path, roads_parquet_path) -> ZoneRoads:
    """Read the COG's metadata and the roads intersecting its footprint."""
    sat_meta = _read_image_metadata(sat_cog_path)
    sat_crs = sat_meta["crs"]
    footprint = box(*sat_meta["bounds"])

    footprint_gdf = gpd.GeoDataFrame({"geometry": [footprint]}, crs=sat_crs)
    minx, miny, maxx, maxy = footprint_gdf.to_crs(WGS84).total_bounds

    print("Filtering road parquet to COG footprint...")
    try:
        roads = gpd.read_parquet(roads_parquet_path, bbox=(minx, miny, maxx, maxy))
    except (ValueError, KeyError):
        print("Warning: parquet lacks bbox column; read all roads and filter in memory.")
        roads = gpd.read_parquet(roads_parquet_path)
        roads = roads.cx[minx:maxx, miny:maxy]

    if roads.empty:
        print("Warning: no roads found in footprint.")
        return ZoneRoads(gpd.GeoDataFrame({"geometry": []}, crs=sat_crs), sat_meta)

    print(f"Reprojecting and clipping {len(roads)} road segments...")
    roads = roads.to_crs(sat_crs)
    return ZoneRoads(gpd.clip(roads, footprint), sat_meta)



class LabelGenerator(ABC):
    """Create tiled output labels (raster, graph) and return written paths."""
    name: str

    @abstractmethod
    def generate(self, zone: ZoneRoads, out_path: Path) -> Path:
        pass


class RasterMaskLabeler(LabelGenerator):
    """Rasterize buffered roads into a binary COG mask."""

    name = "mask_raster"
    BUFFER_COL = "buffer"

    def buffer_distances(self, roads):
        if self.BUFFER_COL not in roads.columns:
            raise ValueError(
                f"Road frame is missing the '{self.BUFFER_COL}' column required for "
                "buffering; regenerate the road vector with the current roads extractor."
            )
        return roads[self.BUFFER_COL].to_numpy(dtype="float64")

    def rasterize(self, zone: ZoneRoads) -> np.ndarray:
        """Rasterise the binary mask image"""
        meta = zone.image_metadata["meta"]
        roads = zone.roads
        if roads.empty:
            print("Warning: No roads to rasterize. Creating an empty mask.")
            return np.zeros((meta["height"], meta["width"]), dtype="uint8")

        distances = self.buffer_distances(roads)
        print(
            f"Rasterizing {len(roads)} roads with per-class buffers "
            f"({distances.min():.1f}-{distances.max():.1f} m half-width)..."
        )
        buffered = roads.geometry.buffer(distances)
        return features.rasterize(
            shapes=((geom, 1) for geom in buffered),
            out_shape=(meta["height"], meta["width"]),
            transform=meta["transform"],
            fill=0,
            all_touched=True,
            dtype="uint8",
        )

    def generate(self, zone: ZoneRoads, out_path) -> Path:
        mask = self.rasterize(zone)
        print(f"Writing tiled road-mask COG to {out_path}...")
        write_mask_cog(
            out_path,
            mask,
            zone.image_metadata["meta"],
            blockxsize=zone.image_metadata["blockxsize"],
            blockysize=zone.image_metadata["blockysize"],
        )
        print("Raster mask complete.")
        return Path(out_path)


class RoadGraphLabeler(LabelGenerator):
    """Write cropped tile road vector graph to parquet."""

    name = "mask_graph"

    def generate(self, zone: ZoneRoads, out_path) -> Path:
        roads = zone.roads
        if not roads.empty:
            tile_box = box(*zone.image_metadata["bounds"])
            roads = self._clip_roads(roads, tile_box, roads.sindex)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Writing {len(roads)} road segments (cropped to tile) to {out_path}...")
        roads.to_parquet(out_path)
        print("Road graph complete.")
        return out_path

    @staticmethod
    def _clip_roads(roads, patch_box, sindex):
        # check empty
        empty = roads.iloc[0:0]
        if sindex is None:
            return empty
        candidates = roads.iloc[list(sindex.query(patch_box))]
        if candidates.empty:
            return empty

        clipped_geom = candidates.geometry.intersection(patch_box)
        keep = ~clipped_geom.is_empty & (clipped_geom.length > 0) # drop empty and points
        if not keep.any():
            return empty

        out = candidates.loc[keep].copy()
        out.geometry = clipped_geom.loc[keep]
        return out
