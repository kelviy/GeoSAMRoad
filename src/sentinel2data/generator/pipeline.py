from abc import ABC, abstractmethod
from pathlib import Path

import geopandas as gpd

from sentinel2data.generator.config import (
    SPLIT_NAMES,
    WGS84,
    DatasetPaths,
    TileSpec,
)
from sentinel2data.generator.helper import scan_rasters
from sentinel2data.generator.io import write_geoparquet, write_split_csvs
from sentinel2data.generator.labels import RasterMaskLabeler
from sentinel2data.generator.naming import assign_zone_metadata
from sentinel2data.generator.processor import ROSAProcessor
from sentinel2data.generator.tagging import RoadDensityClassifier


class ZoneSource(ABC):
    """Collects list of (split, cog paths) pairs."""

    directory: Path

    @abstractmethod
    def enumerate(self) -> list:
        pass

    def skipped_files(self) -> list:
        """Rasters found but not assigned to any split."""
        return []


class PreSplitImagerySource(ZoneSource):
    """Images are already pre split so track split folders"""

    def __init__(self, imagery_dir, splits=SPLIT_NAMES):
        self.directory = Path(imagery_dir)
        self.splits = tuple(splits)

    def enumerate(self) -> list:
        image_split_pairs, missing = [], []
        for split in self.splits:
            split_dir = self.directory / split
            paths = scan_rasters(split_dir) if split_dir.is_dir() else []
            if not paths:
                missing.append(split)
                continue
            image_split_pairs.append((split, paths))

        if len(missing) == len(self.splits):
            raise FileNotFoundError(
                f"No split directories found under {self.directory}. Expected "
                f"{'/'.join(self.splits)} subdirectories of COGs"
            )
        if missing:
            raise FileNotFoundError(
                f"Missing or empty split director{'y' if len(missing) == 1 else 'ies'} "
                f"under {self.directory}: {', '.join(missing)}"
            )
        return image_split_pairs

    def skipped_files(self) -> list:
        return scan_rasters(self.directory)


class RosaPipeline:
    """Pipeline for ROSA Dataset Creation"""

    def __init__(self, *, source, processor, paths):
        self.source = source
        self.processor = processor
        self.paths = paths

    def run(self):
        # Collect (split, COGs) pairs from the source
        jobs = self.source.enumerate()
        if not jobs:
            print(f"Aborting. No satellite imagery found in {self.source.directory}")
            return None
        counts = {split: len(paths) for split, paths in jobs}
        print(f"Found {sum(counts.values())} source COG(s): {counts}")

        # Parse filename for metadata
        flat = [(split, path) for split, paths in jobs for path in paths]
        zone_meta = assign_zone_metadata(flat)

        # Create artifacts and metadata per tile
        rows = []
        for zone_id, (split, path) in enumerate(flat):
            print("-" * 50)
            zone_rows = self.processor.process(
                zone_id, path, split, zone_meta[(split, path)], self.paths
            )
            rows.extend(zone_rows)

        if not rows:
            print("Warning: No images produced; nothing written.")
            return None

        gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=WGS84)
        gdf["image_id"] = range(len(gdf))

        # Road density over the whole dataset (Jenks breaks).
        density = RoadDensityClassifier()
        gdf[density.column] = density.tag(gdf)

        for split, _ in jobs:
            if not (gdf["split_set"] == split).any():
                print(f"WARNING: split {split!r} produced no tiles -- its CSV will be missing.")

        # Writing metadata
        schema = self.processor.schema
        gdf = gdf.rename_geometry(schema.geometry_col)
        gdf = gdf[schema.columns]
        write_geoparquet(gdf, self.paths.metadata_path)
        write_split_csvs(gdf, self.paths.splits_dir, schema.split_csv_columns)

        print("-" * 50)
        print(f"Wrote {len(gdf)} images to {self.paths.metadata_path}")
        return self.paths.metadata_path


def _make_processor(roads_parquet_path, tile_size, patch_size, enhance_cfg):
    return ROSAProcessor(
        roads_parquet_path,
        tile_spec=TileSpec(tile_size=tile_size, patch_size=patch_size),
        mask_labeler=RasterMaskLabeler(),
        enhance_cfg=enhance_cfg,
    )


def make_rosa_pipeline(
    imagery_dir,
    output_dir,
    roads_parquet_path,
    tile_size=512,
    patch_size=256,
    enhance_cfg=None,
):
    """Build a dataset from a pre-split ``<imagery_dir>/{train,val,test}`` root."""
    return RosaPipeline(
        source=PreSplitImagerySource(imagery_dir),
        processor=_make_processor(roads_parquet_path, tile_size, patch_size, enhance_cfg),
        paths=DatasetPaths(output_dir),
    )
