"""End-to-end test of the ``generate`` command on the synthetic zone.

The synthetic zone lives in ``tests/sentinel2data/synthetic_zone.py`` and is
exposed as the ``synthetic_zone`` and ``shared_synthetic_zone`` fixtures (see
``conftest.py``). That module explains how to add a new synthetic case.
"""
import pickle

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
import yaml
import rasterio.windows
from shapely.geometry import box
from typer.testing import CliRunner

from sentinel2data.cli import app
from tests.sentinel2data.synthetic_zone import (
    BAND_COUNT,
    EMPTY_TILE,
    EXCLUDED_ONLY_TILE,
    TEST_ZONE,
    TRAIN_ORIGIN,
    TRAIN_ZONE,
    VAL_ZONE,
    SyntheticZone,
)

ENHANCED_RGB_BAND_COUNT = 3  # appended by the generator
S1_DESCENDING = ("VV_descending", "VH_descending")

# Written band order of the baseline: the source bands minus S1 descending,
# then the enhanced RGB.
BASELINE_BANDS = (
    "B4_R", "B3_G", "B2_B", "B8_NIR", "B5", "B6", "B7", "B8A", "B11", "B12",
    "VV_ascending", "VH_ascending",
    "elevation", "slope", "aspect",
    "esa_urban_10m", "gisa_urban_10m", "wsf_urban_10m",
    "B4_R_enhanced", "B3_G_enhanced", "B2_B_enhanced",
)

SUBDIR_SUFFIXES = {
    "imagery": ".tif",
    "masks_raster": ".tif",
    "masks_graph": ".parquet",
    "mask_adj_graphs": ".pkl",
    "mask_keypoints": ".tif",
}

# Tiles the generator should write, per split. Written out by hand from the
# fixture layout: the main zone is 2x2 tiles with one road-free tile (r1_c1)
# and one tile with only Track and Slipway roads (r1_c0), and the val and
# test zones are one tile each.
EXPECTED_TILES = {
    "train": {f"{TRAIN_ZONE}_r0_c0", f"{TRAIN_ZONE}_r0_c1"},
    "val": {f"{VAL_ZONE}_r0_c0"},
    "test": {f"{TEST_ZONE}_r0_c0"},
}
# Without road-class exclusion, the Track/Slipway-only tile is kept too.
LEGACY_EXPECTED_TILES = {**EXPECTED_TILES, "train": EXPECTED_TILES["train"] | {f"{TRAIN_ZONE}_r1_c0"}}

EXCLUDED_CLASSES = {"Track", "Slipway"}


@pytest.fixture(scope="module")
def dataset(shared_synthetic_zone, tmp_path_factory):
    """Run ``generate`` once on the shared zone; returns the output dataset root."""
    return run_generate(shared_synthetic_zone, tmp_path_factory.mktemp("dataset"))


@pytest.fixture(scope="module")
def legacy_dataset(shared_synthetic_zone, tmp_path_factory):
    """``generate`` with road-class exclusion and the band drop turned off."""
    return run_generate(
        shared_synthetic_zone, tmp_path_factory.mktemp("legacy"),
        "--keep-all-road-classes", "--keep-s1-descending",
    )


def run_generate(zone: SyntheticZone, output_dir, *extra_args):
    result = CliRunner().invoke(
        app,
        [
            "generate",
            "--roads", str(zone.roads_path),
            "--imagery-dir", str(zone.imagery_dir),
            "--output-dir", str(output_dir),
            "--tile-size", str(zone.tile_size),
            "--patch-size", str(zone.patch_size),
            *extra_args,
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
                assert (img.count, img.height, img.width) == (len(BASELINE_BANDS), ts, ts)
            with rasterio.open(dataset / split / "masks_raster" / f"{stem}.tif") as msk:
                assert msk.read(1).any()
            roads = gpd.read_parquet(dataset / split / "masks_graph" / f"{stem}.parquet")
            assert not roads.empty
            with open(dataset / split / "mask_adj_graphs" / f"{stem}.pkl", "rb") as f:
                adj = pickle.load(f)
            assert isinstance(adj, dict) and adj


def test_excluded_road_classes_appear_in_no_label(dataset):
    for split, expected in EXPECTED_TILES.items():
        for stem in expected:
            roads = gpd.read_parquet(dataset / split / "masks_graph" / f"{stem}.parquet")
            assert not set(roads["road_class"]) & EXCLUDED_CLASSES, stem


def test_tile_with_only_excluded_roads_is_dropped(dataset, shared_synthetic_zone):
    assert shared_synthetic_zone.tile_stem(EXCLUDED_ONLY_TILE) not in written_stems(dataset, "train", "imagery")


def test_baseline_imagery_drops_s1_descending_bands(dataset):
    for split, expected in EXPECTED_TILES.items():
        for stem in expected:
            with rasterio.open(dataset / split / "imagery" / f"{stem}.tif") as img:
                assert img.descriptions == BASELINE_BANDS, stem
                assert not set(img.descriptions) & set(S1_DESCENDING)
    meta = gpd.read_parquet(dataset / "metadata.parquet")
    assert set(meta["band_count"]) == {len(BASELINE_BANDS)}


def test_baseline_imagery_keeps_the_source_pixels_of_kept_bands(dataset, shared_synthetic_zone):
    stem = shared_synthetic_zone.tile_stem("r0_c1")
    ts = shared_synthetic_zone.tile_size
    with rasterio.open(shared_synthetic_zone.zone_path) as zone:
        window = rasterio.windows.Window(ts, 0, ts, ts)
        vv_asc, elevation = zone.read(11, window=window), zone.read(15, window=window)
    with rasterio.open(dataset / "train" / "imagery" / f"{stem}.tif") as img:
        assert (img.read(BASELINE_BANDS.index("VV_ascending") + 1) == vv_asc).all()
        assert (img.read(BASELINE_BANDS.index("elevation") + 1) == elevation).all()


def test_legacy_options_keep_the_previous_tiles_and_bands(legacy_dataset):
    for split, expected in LEGACY_EXPECTED_TILES.items():
        for subdir in SUBDIR_SUFFIXES:
            assert written_stems(legacy_dataset, split, subdir) == expected, (split, subdir)
        for stem in expected:
            with rasterio.open(legacy_dataset / split / "imagery" / f"{stem}.tif") as img:
                assert img.count == BAND_COUNT + ENHANCED_RGB_BAND_COUNT
                assert img.descriptions[12:14] == S1_DESCENDING
    roads = gpd.read_parquet(legacy_dataset / "train" / "masks_graph" / f"{TRAIN_ZONE}_r1_c0.parquet")
    assert set(roads["road_class"]) == {"Track", "Slipway"}


# Every tile position of the 3x3 train grid: 2x2 full tiles plus the east and
# south edge strips, which are never cut into a full tile.
EXPECTED_TRAIN_STATUS = {
    "r0_c0": "kept", "r0_c1": "kept", "r0_c2": "untiled",
    "r1_c0": "dropped_no_road", "r1_c1": "dropped_no_road", "r1_c2": "untiled",
    "r2_c0": "untiled", "r2_c1": "untiled", "r2_c2": "untiled",
}


def read_tile_status(dataset, zone):
    return gpd.read_parquet(dataset / "tile_status" / f"{zone}.parquet")


def train_status(table):
    return {f"r{r}_c{c}": st for r, c, st in zip(table["row"], table["col"], table["status"])}


def test_tile_status_covers_every_tile_position_in_every_zone(dataset):
    train = read_tile_status(dataset, TRAIN_ZONE)
    assert train_status(train) == EXPECTED_TRAIN_STATUS
    assert set(train["tile_id"]) == {f"{TRAIN_ZONE}_{t}" for t in EXPECTED_TRAIN_STATUS}
    assert set(train["split_set"]) == {"train"}
    for zone, split in [(VAL_ZONE, "val"), (TEST_ZONE, "test")]:
        table = read_tile_status(dataset, zone)
        assert list(zip(table["tile_id"], table["status"], table["split_set"])) == [(f"{zone}_r0_c0", "kept", split)]


def test_tile_status_footprints_are_in_the_zone_crs_and_tile_the_zone(dataset):
    train = read_tile_status(dataset, TRAIN_ZONE).set_index(["row", "col"])
    assert train.crs.to_string() == "EPSG:32735"
    x0, y0 = TRAIN_ORIGIN  # 128 px tiles and a 40 px strip, at 10 m
    assert train.geometry[(0, 0)].equals(box(x0, y0 - 1280, x0 + 1280, y0))
    assert train.geometry[(0, 2)].equals(box(x0 + 2560, y0 - 1280, x0 + 2960, y0))
    assert train.geometry[(2, 2)].equals(box(x0 + 2560, y0 - 2960, x0 + 2960, y0 - 2560))
    zone = box(x0, y0 - 2960, x0 + 2960, y0)
    assert train.geometry.union_all().equals(zone)
    assert train.geometry.area.sum() == pytest.approx(zone.area)


def test_tile_status_marks_nan_tiles_and_ignores_nan_in_dropped_bands(synthetic_zone, tmp_path):
    ts = synthetic_zone.tile_size
    with rasterio.open(synthetic_zone.zone_path, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as zone:
        hole = np.full((1, 4, 4), np.nan, dtype="float32")
        zone.write(hole, indexes=[1], window=rasterio.windows.Window(ts + 10, 10, 4, 4))  # B4_R in r0_c1
        zone.write(hole, indexes=[13], window=rasterio.windows.Window(10, 10, 4, 4))  # VV_descending in r0_c0

    status = train_status(read_tile_status(run_generate(synthetic_zone, tmp_path / "out"), TRAIN_ZONE))
    assert status["r0_c1"] == "dropped_nan"
    assert status["r0_c0"] == "kept"


def read_report(dataset):
    return yaml.safe_load((dataset / "generation_report.yaml").read_text())


def test_report_lists_excluded_segments_per_zone(dataset):
    labels = read_report(dataset)["label_changes"]
    assert {"Track", "Slipway"} <= set(labels["excluded_road_classes"])
    train = labels["excluded_by_zone"][TRAIN_ZONE]
    # Track runs 80 px east; Slipway 80 px east and 10 px south. 10 m pixels.
    assert train["Track"] == {"segments": 1, "length_m": pytest.approx(800.0)}
    assert train["Slipway"] == {"segments": 1, "length_m": pytest.approx(806.2258, abs=1e-3)}
    for zone in (VAL_ZONE, TEST_ZONE):
        assert all(v["segments"] == 0 for v in labels["excluded_by_zone"][zone].values())


def test_report_notes_the_band_change_as_an_imagery_change(dataset):
    report = read_report(dataset)
    assert report["imagery_changes"] == {
        "dropped_bands": list(S1_DESCENDING),
        "band_count": len(BASELINE_BANDS),
        "band_names": list(BASELINE_BANDS),
    }
    assert "dropped_bands" not in report["label_changes"]


def test_report_counts_tile_statuses_per_zone(dataset):
    tiles = read_report(dataset)["tile_status"]
    assert tiles[TRAIN_ZONE] == {"kept": 2, "dropped_no_road": 2, "dropped_nan": 0, "untiled": 5}
    assert tiles[VAL_ZONE] == {"kept": 1, "dropped_no_road": 0, "dropped_nan": 0, "untiled": 0}


def test_legacy_report_records_no_exclusion_or_band_drop(legacy_dataset):
    report = read_report(legacy_dataset)
    assert report["label_changes"]["excluded_road_classes"] == []
    assert report["imagery_changes"]["dropped_bands"] == []
    assert report["imagery_changes"]["band_count"] == BAND_COUNT + ENHANCED_RGB_BAND_COUNT
