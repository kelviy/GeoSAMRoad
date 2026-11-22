"""End-to-end test of the ``generate`` command on the synthetic zone.

The synthetic zone lives in ``tests/sentinel2data/synthetic_zone.py`` and is
exposed as the ``synthetic_zone`` and ``shared_synthetic_zone`` fixtures (see
``conftest.py``). That module explains how to add a new synthetic case.
"""
import pickle

import geopandas as gpd
import pandas as pd
import pytest
import rasterio
from typer.testing import CliRunner

from sentinel2data.cli import app
from tests.sentinel2data.synthetic_zone import (
    BAND_COUNT,
    EMPTY_TILE,
    TEST_ZONE,
    TRAIN_ZONE,
    VAL_ZONE,
    SyntheticZone,
)

ENHANCED_RGB_BAND_COUNT = 3  # appended by the generator

SUBDIR_SUFFIXES = {
    "imagery": ".tif",
    "masks_raster": ".tif",
    "masks_graph": ".parquet",
    "mask_adj_graphs": ".pkl",
    "mask_keypoints": ".tif",
}

# Tiles the generator should write, per split. Written out by hand from the
# fixture layout: the main zone is 2x2 tiles with one road-free tile (r1_c1),
# and the val and test zones are one tile each.
EXPECTED_TILES = {
    "train": {f"{TRAIN_ZONE}_r0_c0", f"{TRAIN_ZONE}_r0_c1", f"{TRAIN_ZONE}_r1_c0"},
    "val": {f"{VAL_ZONE}_r0_c0"},
    "test": {f"{TEST_ZONE}_r0_c0"},
}


@pytest.fixture(scope="module")
def dataset(shared_synthetic_zone, tmp_path_factory):
    """Run ``generate`` once on the shared zone; returns the output dataset root."""
    return run_generate(shared_synthetic_zone, tmp_path_factory.mktemp("dataset"))


def run_generate(zone: SyntheticZone, output_dir):
    result = CliRunner().invoke(
        app,
        [
            "generate",
            "--roads", str(zone.roads_path),
            "--imagery-dir", str(zone.imagery_dir),
            "--output-dir", str(output_dir),
            "--tile-size", str(zone.tile_size),
            "--patch-size", str(zone.patch_size),
        ],
    )
    assert result.exit_code == 0, result.output + repr(result.exception)
    return output_dir


def written_stems(output_dir, split, subdir):
    suffix = SUBDIR_SUFFIXES[subdir]
    return {p.name.removesuffix(suffix) for p in (output_dir / split / subdir).glob(f"*{suffix}")}


def test_generate_writes_every_artifact_for_every_road_tile(dataset):
    for split, expected in EXPECTED_TILES.items():
        for subdir in SUBDIR_SUFFIXES:
            assert written_stems(dataset, split, subdir) == expected, (split, subdir)


def test_generate_drops_road_free_tile_and_partial_edge_strip(dataset, shared_synthetic_zone):
    train_tiles = written_stems(dataset, "train", "imagery")
    assert shared_synthetic_zone.tile_stem(EMPTY_TILE) not in train_tiles
    # The edge strip has a road but is narrower than a tile, so no r2/c2 tile.
    assert not any("_r2_" in t or t.endswith("_c2") for t in train_tiles)


def test_generate_writes_metadata_and_split_csvs(dataset):
    meta = gpd.read_parquet(dataset / "metadata.parquet")
    assert len(meta) == sum(len(t) for t in EXPECTED_TILES.values())
    for split, expected in EXPECTED_TILES.items():
        csv = pd.read_csv(dataset / "splits" / f"{split}.csv")
        assert len(csv) == len(expected)
        for rel in csv["image_path"]:
            assert (dataset / rel).is_file()


def test_generate_tile_artifacts_contain_the_roads(dataset, shared_synthetic_zone):
    ts = shared_synthetic_zone.tile_size

    for split, expected in EXPECTED_TILES.items():
        for stem in expected:
            with rasterio.open(dataset / split / "imagery" / f"{stem}.tif") as img:
                assert (img.count, img.height, img.width) == (BAND_COUNT + ENHANCED_RGB_BAND_COUNT, ts, ts)
            with rasterio.open(dataset / split / "masks_raster" / f"{stem}.tif") as msk:
                assert msk.read(1).any()
            roads = gpd.read_parquet(dataset / split / "masks_graph" / f"{stem}.parquet")
            assert not roads.empty
            with open(dataset / split / "mask_adj_graphs" / f"{stem}.pkl", "rb") as f:
                adj = pickle.load(f)
            assert isinstance(adj, dict) and adj
