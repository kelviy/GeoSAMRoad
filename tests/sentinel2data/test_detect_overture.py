"""End-to-end tests of the ``overture`` detector: ``detect`` on the packaged
synthetic zone against an Overture layer each test writes.

Overture roads are given in train zone pixels (one pixel is 10 m). Unless a
test says otherwise, the layer also holds a copy of every train zone label
shifted 5 m east and 5 m north, well within the 15 m match tolerance, so
labels are matched and only the test's own roads can produce flags.
"""

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
import yaml
from shapely import affinity

from tests.sentinel2data.synthetic_zone import (
    CRS,
    TRAIN_ZONE,
    VAL_ZONE,
    train_pixels,
    write_overture,
)
from tests.sentinel2data.test_detect import (
    DEFAULT_CONFIG,
    decide,
    raw_id_of,
    read_flags,
    read_flags_by_fid,
    run_detect,
)

# r0_c1 is kept; cols 128-255, rows 0-127.
LONG_ROAD = train_pixels([(140, 15), (240, 15)])  # 1000 m, no label within 150 m
SHORT_ROAD = train_pixels([(140, 120), (147, 120)])  # 70 m
# Crosses the near_duplicate Streets (row 30), so it is matched there and
# unmatched for about 235 m on each side.
CROSSING_ROAD = train_pixels([(190, 5), (190, 55)])
# r1_c1 is dropped (no roads).
DROPPED_TILE_ROAD = train_pixels([(140, 170), (240, 170)])


def label_copies(zone, skip=()):
    """An Overture copy of every train zone label, 5 m off, except the cases in ``skip``."""
    return [
        ("residential", affinity.translate(g, 5.0, 5.0))
        for case in zone.cases.values()
        if case.name not in skip and case.name not in ("track", "slipway")
        for g in case.geometries
    ]


def default_missing_classes():
    return yaml.safe_load(DEFAULT_CONFIG.read_text())["overture"]["missing_classes"]


def overture_config(tmp_path, roads, **overture):
    """The default config with only the overture detector on, reading ``roads``,
    its section updated with ``overture``."""
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    for section in config.values():
        if isinstance(section, dict) and "enabled" in section:
            section["enabled"] = False
    config["overture"]["enabled"] = True
    config["overture"].update(
        path=str(write_overture(tmp_path / "overture.parquet", roads)), **overture
    )
    path = tmp_path / "overture.yaml"
    path.write_text(yaml.safe_dump(config))
    return str(path)


def detect_overture(zones, tmp_path, roads, **overture):
    run_detect(
        zones,
        "--zone",
        TRAIN_ZONE,
        "--detector-config",
        overture_config(tmp_path, roads, **overture),
    )
    return read_flags(zones)


def test_an_overture_road_with_no_label_gets_one_missing_flag(
    zones, tmp_path, shared_synthetic_zone
):
    flags = detect_overture(
        zones,
        tmp_path,
        [*label_copies(shared_synthetic_zone), ("residential", LONG_ROAD)],
    )
    assert flags["type"].tolist() == ["missing?"]
    flag = flags.iloc[0]
    assert flag.geometry.geom_type == "Polygon"
    assert flag.geometry.covers(LONG_ROAD)
    assert (flag["detector"], flag["source"], flag["decision"]) == (
        "overture",
        "overture",
        "pending",
    )


def test_an_overture_road_shorter_than_the_minimum_gets_no_flag(
    zones, tmp_path, shared_synthetic_zone
):
    roads = [*label_copies(shared_synthetic_zone), ("residential", SHORT_ROAD)]
    assert detect_overture(zones, tmp_path, roads).empty
    flags = detect_overture(zones, tmp_path, roads, missing_min_length_m=60.0)
    assert flags["type"].tolist() == ["missing?"]


def test_an_overture_road_outside_the_kept_tiles_gets_no_flag(
    zones, tmp_path, shared_synthetic_zone
):
    roads = [*label_copies(shared_synthetic_zone), ("residential", DROPPED_TILE_ROAD)]
    assert detect_overture(zones, tmp_path, roads).empty


def test_labels_matched_by_overture_within_the_tolerance_get_no_flag(
    zones, tmp_path, shared_synthetic_zone
):
    assert detect_overture(zones, tmp_path, label_copies(shared_synthetic_zone)).empty


def test_each_unmatched_stretch_of_a_road_gets_its_own_flag(
    zones, tmp_path, shared_synthetic_zone
):
    flags = detect_overture(
        zones,
        tmp_path,
        [*label_copies(shared_synthetic_zone), ("residential", CROSSING_ROAD)],
    )
    assert flags["type"].tolist() == ["missing?", "missing?"]
    north = shapely.Point(train_pixels([(190, 5), (190, 6)]).coords[0])
    south = shapely.Point(train_pixels([(190, 55), (190, 56)]).coords[0])
    assert sorted(flags.geometry.covers(north)) == [False, True]
    assert sorted(flags.geometry.covers(south)) == [False, True]


def test_a_label_with_no_overture_road_nearby_gets_a_phantom_flag(
    zones, tmp_path, shared_synthetic_zone
):
    # A 720 m dead-end Street stored as two rows.
    case = shared_synthetic_zone.cases["split_dead_end"]
    roads = label_copies(shared_synthetic_zone, skip={"split_dead_end"})
    flags = detect_overture(zones, tmp_path, roads)

    assert flags["type"].tolist() == ["phantom?"]
    flag = flags.iloc[0]
    assert all(flag.geometry.covers(g) for g in case.geometries)
    ids = sorted(raw_id_of(zones, g) for g in case.geometries)
    assert flag["raw_ids"] == ",".join(map(str, ids))
    assert (flag["detector"], flag["source"]) == ("overture", "overture")

    assert detect_overture(zones, tmp_path, roads, phantom_min_length_m=800.0).empty


def test_switching_a_class_off_removes_its_missing_flags(
    zones, tmp_path, shared_synthetic_zone
):
    # The label copies are residential too: labels are still matched against them.
    roads = [*label_copies(shared_synthetic_zone), ("residential", LONG_ROAD)]
    defaults = default_missing_classes()
    flags = detect_overture(
        zones, tmp_path, roads, missing_classes={**defaults, "residential": False}
    )
    assert flags.empty


def test_a_class_switched_off_by_default_gets_no_missing_flag(
    zones, tmp_path, shared_synthetic_zone
):
    roads = [*label_copies(shared_synthetic_zone), ("track", LONG_ROAD)]
    assert detect_overture(zones, tmp_path, roads).empty


def test_flags_are_single_part_areas_holding_no_overture_vertex(
    zones, tmp_path, shared_synthetic_zone
):
    roads = [("residential", g) for g in (LONG_ROAD, CROSSING_ROAD, SHORT_ROAD)]
    flags = detect_overture(zones, tmp_path, roads, missing_min_length_m=50.0)
    assert set(flags["type"]) == {"missing?", "phantom?"}
    assert (flags.geom_type == "Polygon").all()
    assert (flags["source"] == "overture").all()

    overture = gpd.read_parquet(tmp_path / "overture.parquet").to_crs(CRS)
    vertices = shapely.get_coordinates(overture.geometry.to_numpy())
    corners = shapely.get_coordinates(flags.geometry.to_numpy())
    gaps = np.abs(corners[:, None, :] - vertices[None, :, :]).max(axis=2)
    assert gaps.min() > 1e-6


def test_rerunning_keeps_flags_and_keeps_decided_ones(
    zones, tmp_path, shared_synthetic_zone
):
    roads = [
        *label_copies(shared_synthetic_zone, skip={"split_dead_end"}),
        ("residential", LONG_ROAD),
    ]
    detect_overture(zones, tmp_path, roads)
    first = read_flags_by_fid(zones)
    assert sorted(first["type"]) == ["missing?", "phantom?"]

    detect_overture(zones, tmp_path, roads)
    pd.testing.assert_frame_equal(read_flags_by_fid(zones), first)

    decide(zones, first.index[first["type"] == "missing?"], "real")
    decided = read_flags_by_fid(zones)
    defaults = default_missing_classes()
    detect_overture(
        zones, tmp_path, roads, missing_classes={**defaults, "residential": False}
    )
    pd.testing.assert_frame_equal(read_flags_by_fid(zones), decided)


def test_a_zone_with_no_overture_roads_gets_phantom_flags_for_its_labels(
    zones, tmp_path, shared_synthetic_zone
):
    config = overture_config(tmp_path, label_copies(shared_synthetic_zone))
    run_detect(zones, "--detector-config", config)
    assert read_flags(zones).empty
    assert read_flags(zones, VAL_ZONE)["type"].tolist() == ["phantom?"]


def test_a_missing_overture_file_is_rejected_before_any_zone_runs(zones, tmp_path):
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    config["overture"]["path"] = str(tmp_path / "nowhere.parquet")
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(config))
    result = run_detect(zones, "--detector-config", str(path), expect_ok=False)
    # The error box wraps long paths.
    text = "".join(c for c in result.output if c.isalnum() or c in "._")
    assert (
        result.exit_code != 0 and "overture.path" in text and "nowhere.parquet" in text
    )
