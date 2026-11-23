"""End-to-end tests of the ``gap`` detector and its connector proposals, run
through ``detect`` on the packaged synthetic zone."""

import sqlite3
from contextlib import closing
from typing import cast

import geopandas as gpd
import pyogrio
from affine import Affine
from pandas.testing import assert_frame_equal
from shapely.geometry import Point

from sentinel2data.generator.artifacts import native_adjacency
from tests.sentinel2data.synthetic_zone import TRAIN_ZONE
from tests.sentinel2data.test_detect import (
    gpkg,
    read_flags,
    run_detect,
    update_in_place,
)
from tests.sentinel2data.test_geometry_detectors import (
    add_roads,
    config_with,
    raw_ids_of,
    zone_line,
)
from tests.sentinel2data.test_package_zones import run_package


def read_labels(zones) -> gpd.GeoDataFrame:
    return cast(
        gpd.GeoDataFrame,
        pyogrio.read_dataframe(gpkg(zones), layer="labels", fid_as_index=True),
    )


def gap_flags(zones) -> gpd.GeoDataFrame:
    flags = read_flags(zones)
    return cast(gpd.GeoDataFrame, flags[flags["type"] == "gap"])


def proposed(zones) -> gpd.GeoDataFrame:
    labels = read_labels(zones)
    return cast(gpd.GeoDataFrame, labels[labels["status"] == "proposed"])


def flags_at(flags, point, distance=1.0) -> gpd.GeoDataFrame:
    return cast(gpd.GeoDataFrame, flags[flags.geometry.distance(point) <= distance])


def test_the_gap_case_gets_one_gap_flag_linked_to_one_proposed_connector(
    zones, shared_synthetic_zone
):
    roads = shared_synthetic_zone.cases["gap"].geometries
    run_detect(zones)

    ends = [Point(roads[0].coords[-1]), Point(roads[1].coords[0])]
    flags = gap_flags(zones)
    at_gap = flags[flags.geometry.distance(ends[0]) <= 25.0]
    assert len(at_gap) == 1
    flag = at_gap.iloc[0]
    assert (flag["type"], flag["detector"], flag["source"], flag["decision"]) == (
        "gap",
        "gap",
        "labels",
        "pending",
    )
    assert flag["raw_ids"] == raw_ids_of(zones, roads)

    rows = proposed(zones)
    linked = rows.loc[[int(flag["proposal_fid"])]]
    assert len(linked) == 1
    row = linked.iloc[0]
    assert (row["defect"], row["road_class"]) == ("gap", "Other Road")
    assert row["raw_id"] is None or row["raw_id"] != row["raw_id"]
    assert row["annotator"] is None or row["annotator"] != row["annotator"]
    assert rows.geometry.distance(ends[0]).min() < 1e-6


def linked_proposal(zones, flag):
    return read_labels(zones).loc[int(flag["proposal_fid"])].geometry


def stored_roads(zones, lines):
    """The labels rows for ``lines``, as stored in the GeoPackage."""
    labels = read_labels(zones)
    return [
        labels.loc[labels.geometry.hausdorff_distance(line) < 1e-3].geometry.iloc[0]
        for line in lines
    ]


def in_one_road_graph_component(lines) -> bool:
    """Whether ``lines`` form one component of the road graph ``generate``
    builds, which joins lines only at shared vertices."""
    adj = native_adjacency(gpd.GeoDataFrame(geometry=list(lines)), Affine.identity())
    start = next(iter(adj))
    seen, todo = {start}, [start]
    while todo:
        for node in adj[todo.pop()]:
            if node not in seen:
                seen.add(node)
                todo.append(node)
    return len(seen) == len(adj)


def test_the_gap_connector_runs_exactly_between_the_two_roads(
    zones, shared_synthetic_zone
):
    run_detect(zones)
    a, b = stored_roads(zones, shared_synthetic_zone.cases["gap"].geometries)
    (connector,) = [
        linked_proposal(zones, f)
        for _, f in flags_at(gap_flags(zones), Point(a.coords[-1]), 25.0).iterrows()
    ]

    ends = {connector.coords[0][:2], connector.coords[-1][:2]}
    assert ends == {a.coords[-1][:2], b.coords[0][:2]}
    assert not in_one_road_graph_component([a, b])
    assert in_one_road_graph_component([a, b, connector])


def test_a_road_ending_short_of_another_gets_a_connector_onto_it(zones):
    # A T junction with a 15 m gap, in a free part of tile r0_c1.
    through = zone_line((150, 8), (240, 8))
    branch = zone_line((195, 24), (195, 9.5))
    add_roads(zones, through, branch)
    run_detect(zones)

    through, branch = stored_roads(zones, (through, branch))
    flags = flags_at(gap_flags(zones), Point(branch.coords[-1]))
    assert len(flags) == 1
    connector = linked_proposal(zones, flags.iloc[0])
    assert connector.coords[0][:2] == branch.coords[-1][:2]
    assert connector.distance(through) == 0
    assert connector.length == 15.0


def test_a_road_the_zone_edge_cut_gets_no_gap_flag(zones, shared_synthetic_zone):
    (road,) = shared_synthetic_zone.cases["zone_edge"].geometries
    run_detect(zones)
    assert flags_at(gap_flags(zones), road).empty


def test_an_end_on_the_zone_edge_gets_no_gap_flag_however_far_gaps_reach(
    zones, shared_synthetic_zone, tmp_path
):
    # The road's south end is a real dangling end; its north end is the cut.
    (road,) = shared_synthetic_zone.cases["zone_edge"].geometries
    run_detect(zones, "--detector-config", reach(tmp_path, 1000.0))
    flags = gap_flags(zones)
    assert flags_at(flags, Point(road.coords[-1])).empty
    assert len(flags_at(flags, Point(road.coords[0]))) == 1


def test_a_road_outside_the_kept_tiles_gets_no_gap_flag(
    zones, shared_synthetic_zone, tmp_path
):
    (road,) = shared_synthetic_zone.cases["edge_strip"].geometries
    run_detect(zones, "--detector-config", reach(tmp_path, 1000.0))
    assert flags_at(gap_flags(zones), road).empty


def test_a_stub_gets_no_connector_back_onto_the_road_it_hangs_off(
    zones, shared_synthetic_zone
):
    # Its dead end is 25 m from the Other Road it starts on.
    stub = shared_synthetic_zone.cases["dangling_stub"].geometries[1]
    run_detect(zones)
    assert flags_at(gap_flags(zones), Point(stub.coords[-1])).empty


def test_a_dead_end_gets_no_connector_onto_its_own_road(zones, shared_synthetic_zone):
    # The last row is 20 m long, so its end is 20 m from the first row.
    last = shared_synthetic_zone.cases["split_dead_end"].geometries[1]
    run_detect(zones)
    assert flags_at(gap_flags(zones), Point(last.coords[-1])).empty


def reach(tmp_path, max_distance_m):
    """The base config with the gap distance set to ``max_distance_m``."""
    name = f"gap-{max_distance_m:g}.yaml"
    return config_with(tmp_path / name, gap={"max_distance_m": max_distance_m})


def read_by_fid(zones, layer):
    return pyogrio.read_dataframe(gpkg(zones), layer=layer, fid_as_index=True)


def test_rerunning_with_the_same_config_leaves_flags_and_proposals_unchanged(zones):
    run_detect(zones)
    flags, labels = read_by_fid(zones, "flags"), read_by_fid(zones, "labels")
    assert (flags["type"] == "gap").any()

    run_detect(zones)
    assert_frame_equal(read_by_fid(zones, "flags"), flags)
    assert_frame_equal(read_by_fid(zones, "labels"), labels)


def test_a_tighter_gap_distance_removes_the_pending_flag_and_its_proposal(
    zones, tmp_path
):
    # The synthetic gaps are all 20 m.
    run_detect(zones)
    assert len(proposed(zones)) > 0
    run_detect(zones, "--detector-config", reach(tmp_path, 10.0))
    assert gap_flags(zones).empty
    assert proposed(zones).empty


def test_a_pending_proposal_is_refreshed_when_the_road_it_ends_on_moves(zones):
    through, branch = zone_line((150, 8), (240, 8)), zone_line((195, 24), (195, 9.5))
    add_roads(zones, through, branch)
    run_detect(zones)
    # Redraw the through road 10 m further away. Added rows have no raw_id, so
    # the flag key stays the same.
    labels = read_labels(zones)
    (old_fid,) = labels.index[labels.geometry.hausdorff_distance(through) < 1e-3]
    update_in_place(zones, "labels", "status", "deleted", [old_fid])
    add_roads(zones, zone_line((150, 7), (240, 7)))
    run_detect(zones)

    flags = flags_at(gap_flags(zones), Point(branch.coords[-1]))
    assert len(flags) == 1
    assert linked_proposal(zones, flags.iloc[0]).length == 25.0
    assert len(proposed(zones)) == len(gap_flags(zones))


def test_a_proposal_an_annotator_saved_survives_a_rerun_that_no_longer_finds_it(
    zones, tmp_path
):
    run_detect(zones)
    flags = gap_flags(zones)
    fid = int(flags["proposal_fid"].iloc[0])
    update_in_place(zones, "labels", "annotator", "ann", [fid])

    run_detect(zones, "--detector-config", reach(tmp_path, 10.0))
    assert proposed(zones).index.tolist() == [fid]
    assert gap_flags(zones)["proposal_fid"].tolist() == [fid]


def test_a_zone_with_only_pending_gap_proposals_repackages_without_overwrite(
    zones, shared_synthetic_zone, baseline, tmp_path
):
    only_gaps = config_with(
        tmp_path / "gaps.yaml",
        stub={"enabled": False},
        duplicate={"enabled": False},
        crossing={"enabled": False},
    )
    run_detect(zones, "--zone", TRAIN_ZONE, "--detector-config", only_gaps)
    assert len(proposed(zones)) > 0 and set(read_flags(zones)["type"]) == {"gap"}

    run_package(shared_synthetic_zone, baseline, zones, "--zone", TRAIN_ZONE)
    assert read_flags(zones).empty and proposed(zones).empty


def delete_rows(zones, layer, fids):
    with closing(sqlite3.connect(gpkg(zones))) as con, con:
        con.executemany(f'DELETE FROM "{layer}" WHERE fid = ?', [(f,) for f in fids])


def test_a_flag_whose_proposal_was_deleted_by_hand_is_kept(zones, tmp_path):
    run_detect(zones)
    flag_fid, proposal_fid = next(
        (fid, int(f["proposal_fid"]))
        for fid, f in read_by_fid(zones, "flags").iterrows()
        if f["type"] == "gap"
    )
    delete_rows(zones, "labels", [proposal_fid])

    run_detect(zones, "--detector-config", reach(tmp_path, 10.0))
    assert read_by_fid(zones, "flags").loc[flag_fid, "proposal_fid"] == proposal_fid
    assert proposed(zones).empty


def test_two_ends_facing_each_other_off_line_get_one_connector(zones):
    # Neither end's nearest point on the other road is near that road's end.
    a = zone_line((150, 8), (195, 8))
    b = zone_line((193, 10), (240, 10))
    add_roads(zones, a, b)
    run_detect(zones)
    flags = gap_flags(zones)
    assert len(flags_at(flags, Point(a.coords[-1]), 30.0)) == 1
