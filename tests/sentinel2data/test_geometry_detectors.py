"""End-to-end tests of the flag-only geometry detectors, ``duplicate`` and
``crossing``, run through ``detect`` on the packaged synthetic zone."""

from typing import cast

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
import yaml
from shapely.geometry import LineString

from tests.sentinel2data import test_detect
from tests.sentinel2data.synthetic_zone import RESOLUTION, TRAIN_ORIGIN
from tests.sentinel2data.test_detect import DEFAULT_CONFIG, gpkg, read_flags, run_detect

# The packaged zones, shared with the stub tests.
baseline = test_detect.baseline
packaged = test_detect.packaged
zones = test_detect.zones


def config_with(path, **sections):
    """The default detector config with some keys of some sections changed."""
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    for name, values in sections.items():
        config[name].update(values)
    path.write_text(yaml.safe_dump(config))
    return str(path)


def flags_of_type(zones, flag_type, *config) -> gpd.GeoDataFrame:
    run_detect(zones, *(("--detector-config", config[0]) if config else ()))
    flags = read_flags(zones)
    return cast(gpd.GeoDataFrame, flags[flags["type"] == flag_type])


def raw_ids_of(zones, lines):
    """The ``raw_ids`` field a flag on these raw label lines holds."""
    raw = gpd.read_file(gpkg(zones), layer="raw_labels")
    ids = []
    for line in lines:
        (raw_id,) = raw.loc[raw.geometry.hausdorff_distance(line) < 1e-3, "raw_id"]
        ids.append(int(raw_id))
    return ",".join(map(str, sorted(ids)))


def zone_line(*pixels):
    """A line in train-zone pixels (col, row), in zone CRS metres."""
    x0, y0 = TRAIN_ORIGIN
    return LineString([(x0 + c * RESOLUTION, y0 - r * RESOLUTION) for c, r in pixels])


def add_roads(zones, *lines):
    """Append ``added`` label rows, the way an annotator draws new roads."""
    labels = pyogrio.read_dataframe(gpkg(zones), layer="labels")
    new = labels.iloc[[0] * len(lines)].reset_index(drop=True)
    for column in new.columns.drop(["geometry", "status"]):
        new[column] = None
    new["status"] = "added"
    new = new.set_geometry(list(lines), crs=labels.crs)
    pyogrio.write_dataframe(new, gpkg(zones), layer="labels", append=True)


def near(flags, geom, distance=1.0) -> gpd.GeoDataFrame:
    return cast(gpd.GeoDataFrame, flags[flags.geometry.distance(geom) <= distance])


# Duplicate


def test_a_near_duplicate_gets_one_duplicate_flag_naming_both_rows(
    zones, shared_synthetic_zone
):
    roads = shared_synthetic_zone.cases["near_duplicate"].geometries
    flags = flags_of_type(zones, "duplicate")

    assert len(flags) == 1
    flag = flags.iloc[0]
    assert flag.geometry.geom_type == "Point"
    assert all(flag.geometry.distance(road) <= 1.0 for road in roads)
    assert flag["raw_ids"] == raw_ids_of(zones, roads)
    assert (flag["detector"], flag["source"], flag["decision"]) == (
        "duplicate",
        "labels",
        "pending",
    )
    assert flag["flag_key"]


def test_carriageways_further_apart_than_the_duplicate_distance_get_no_flag(
    zones, shared_synthetic_zone
):
    # They are 20 m apart; the default distance is 10 m.
    roads = shared_synthetic_zone.cases["parallel_carriageways"].geometries
    flags = flags_of_type(zones, "duplicate")
    assert all(near(flags, road).empty for road in roads)


def test_a_wider_duplicate_distance_flags_the_carriageways_and_proposes_nothing(
    zones, shared_synthetic_zone, tmp_path
):
    roads = shared_synthetic_zone.cases["parallel_carriageways"].geometries
    config = config_with(tmp_path / "c.yaml", duplicate={"max_distance_m": 25.0})
    flags = flags_of_type(zones, "duplicate", config)

    carriageways = flags["raw_ids"] == raw_ids_of(zones, roads)
    assert carriageways.sum() == 1
    assert flags.loc[carriageways, "proposal_fid"].isna().all()
    labels = pyogrio.read_dataframe(gpkg(zones), layer="labels")
    assert not (labels["status"] == "proposed").any()


@pytest.mark.parametrize(("share", "flagged"), [(0.8, False), (0.45, True)])
def test_the_overlap_share_in_the_config_sets_which_rows_are_duplicates(
    share, flagged, zones, shared_synthetic_zone, tmp_path
):
    # Half of the split dead end's 20 m last row lies within 10 m of its first
    # row. The minimum length is lowered so that the row, a hair under 20 m in
    # UTM floats, is checked.
    roads = shared_synthetic_zone.cases["split_dead_end"].geometries
    config = config_with(
        tmp_path / "c.yaml",
        duplicate={"min_overlap_share": share, "min_length_m": 10.0},
    )
    flags = flags_of_type(zones, "duplicate", config)
    assert (raw_ids_of(zones, roads) in set(flags["raw_ids"])) == flagged


def test_a_duplicate_outside_the_kept_tiles_gets_no_flag(zones, shared_synthetic_zone):
    (road,) = shared_synthetic_zone.cases["edge_strip"].geometries
    add_roads(zones, road.offset_curve(2.0))
    assert near(flags_of_type(zones, "duplicate"), road, distance=5.0).empty


def test_a_pair_whose_shorter_row_is_outside_the_kept_tiles_is_flagged_on_the_longer(
    zones,
):
    # Each row covers the other. The shorter row's middle lies in the dropped
    # east strip (from col 256), the longer row's in the kept tile r0_c1.
    longer = zone_line((236, 10), (272, 10))
    shorter = zone_line((242, 10.5), (276, 10.5))
    add_roads(zones, longer, shorter)
    flags = near(flags_of_type(zones, "duplicate"), longer)
    assert len(flags) == 1
    assert (
        flags.geometry.iloc[0].distance(longer.interpolate(0.5, normalized=True)) < 1e-3
    )


# Crossing


def test_an_unnoded_crossing_gets_one_crossing_flag_at_the_crossing(
    zones, shared_synthetic_zone
):
    a, b = shared_synthetic_zone.cases["unnoded_crossing"].geometries
    flags = flags_of_type(zones, "crossing")

    assert len(flags) == 1
    flag = flags.iloc[0]
    assert flag.geometry.geom_type == "Point"
    assert flag.geometry.distance(a.intersection(b)) < 1e-3
    assert flag["raw_ids"] == raw_ids_of(zones, (a, b))
    assert (flag["detector"], flag["source"], flag["decision"]) == (
        "crossing",
        "labels",
        "pending",
    )
    assert pd.isna(flag["proposal_fid"])


def test_roads_crossing_at_a_shared_vertex_get_no_crossing_flag(zones):
    a = zone_line((180, 5), (190, 15), (200, 25))
    b = zone_line((180, 25), (190, 15), (200, 5))
    add_roads(zones, a, b)
    assert near(flags_of_type(zones, "crossing"), a).empty


def test_a_crossing_outside_the_kept_tiles_gets_no_flag(zones):
    a = zone_line((260, 140), (290, 170))
    b = zone_line((260, 170), (290, 140))
    add_roads(zones, a, b)
    assert near(flags_of_type(zones, "crossing"), a).empty


# Both


def test_the_detectors_never_change_the_labels(zones):
    before = pyogrio.read_dataframe(gpkg(zones), layer="labels", fid_as_index=True)
    run_detect(zones)
    after = pyogrio.read_dataframe(gpkg(zones), layer="labels", fid_as_index=True)
    pd.testing.assert_frame_equal(after, before)


@pytest.mark.parametrize("detector", ["duplicate", "crossing"])
def test_a_disabled_geometry_detector_places_no_flags(detector, zones, tmp_path):
    config = config_with(tmp_path / "c.yaml", **{detector: {"enabled": False}})
    assert flags_of_type(zones, detector, config).empty
