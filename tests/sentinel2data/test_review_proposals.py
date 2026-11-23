"""End-to-end tests of accepting and rejecting proposals in the review core,
on the train zone GeoPackage after ``detect``, read back from the GeoPackage."""

import shutil

import pandas as pd
import pyogrio
import pytest
from shapely.geometry import Point

from sentinel2data.cleaning import review
from tests.sentinel2data.synthetic_zone import TRAIN_ZONE
from tests.sentinel2data.test_detect import gpkg, run_detect, update_in_place
from tests.sentinel2data.test_detect_gap import (
    in_one_road_graph_component,
    stored_roads,
)
from tests.sentinel2data.test_geometry_detectors import add_roads, zone_line
from tests.sentinel2data.test_package_zones import run_package
from tests.sentinel2data.test_review import assert_same_rows, read


@pytest.fixture
def zone(zones):
    run_detect(zones)
    return gpkg(zones)


def test_the_queue_lists_proposals_then_flags_then_tiles(zone):
    kinds = [item.kind for item in review.build_queue(zone)]
    assert kinds[0] == "proposal"
    assert "flag" in kinds and "tile" in kinds
    assert kinds == sorted(kinds, key=["proposal", "flag", "tile"].index)


def gap_proposal(zone, shared_synthetic_zone):
    """The queue item of the gap case's proposal, and its labels fid."""
    end = Point(shared_synthetic_zone.cases["gap"].geometries[0].coords[-1])
    flags = read(zone, "flags")
    (item,) = [
        i
        for i in review.build_queue(zone)
        if i.kind == "proposal" and flags.geometry.loc[i.fid].distance(end) < 25.0
    ]
    return item, int(flags.loc[item.fid, "proposal_fid"])


def test_accepting_a_proposal_adds_its_connector_and_joins_the_two_roads(
    zone, zones, shared_synthetic_zone
):
    item, row = gap_proposal(zone, shared_synthetic_zone)
    before = read(zone, "labels")

    review.decide_flag(zone, item.fid, "real", annotator="Thandi")

    labels = read(zone, "labels")
    added = labels.loc[row]
    assert (added["status"], added["defect"], added["annotator"]) == (
        "added",
        "gap",
        "Thandi",
    )
    assert added.geometry.equals_exact(before.geometry.loc[row], 0)
    flag = read(zone, "flags").loc[item.fid]
    assert (flag["decision"], flag["annotator"]) == ("real", "Thandi")
    a, b = stored_roads(zones, shared_synthetic_zone.cases["gap"].geometries)
    assert in_one_road_graph_component([a, b, added.geometry])
    assert_same_rows(labels.drop(index=row), before.drop(index=row))


def t_junction_with_a_gap(zones):
    """A kept through road and a branch ending 15 m short of it, in a free part
    of tile r0_c1, after ``detect``. Returns the through road's labels fid and
    the branch's gap proposal item."""
    add_roads(zones, zone_line((150, 8), (240, 8)), zone_line((195, 24), (195, 9.5)))
    through = max(read(gpkg(zones), "labels").index) - 1
    update_in_place(zones, "labels", "status", "kept", [through])
    run_detect(zones)
    labels = read(gpkg(zones), "labels")
    branch_end = Point(labels.geometry.loc[through + 1].coords[-1])
    flags = read(gpkg(zones), "flags")
    (item,) = [
        i
        for i in review.build_queue(gpkg(zones))
        if i.kind == "proposal" and flags.geometry.loc[i.fid].distance(branch_end) < 1
    ]
    return through, item


def test_accepting_a_connector_that_ends_mid_road_adds_a_vertex_to_that_road(zones):
    through, item = t_junction_with_a_gap(zones)
    zone = gpkg(zones)
    before = read(zone, "labels").loc[through]

    review.decide_flag(zone, item.fid, "real", annotator="Thandi")

    labels = read(zone, "labels")
    road = labels.loc[through]
    assert (road["status"], road["defect"], road["annotator"]) == (
        "modified",
        "gap",
        "Thandi",
    )
    connector = labels.geometry.loc[
        int(read(zone, "flags").loc[item.fid, "proposal_fid"])
    ]
    assert road.geometry.equals(before.geometry)  # same line, one more vertex
    assert len(road.geometry.coords) == len(before.geometry.coords) + 1
    branch = labels.geometry.loc[through + 1]
    assert in_one_road_graph_component([road.geometry, branch, connector])


def test_rejecting_a_proposal_removes_its_row_and_nothing_else(
    zone, shared_synthetic_zone
):
    item, row = gap_proposal(zone, shared_synthetic_zone)
    before = read(zone, "labels")

    review.decide_flag(zone, item.fid, "not_defect", annotator="Thandi")

    labels = read(zone, "labels")
    assert row not in labels.index
    assert_same_rows(labels, before.drop(index=row))
    flag = read(zone, "flags").loc[item.fid]
    assert (flag["decision"], flag["annotator"]) == ("not_defect", "Thandi")


@pytest.mark.parametrize(
    "first, then",
    [
        ("real", "not_defect"),
        ("not_defect", "real"),
        ("real", "needs_qgis"),
        ("not_defect", "needs_qgis"),
    ],
)
def test_changing_a_proposal_decision_undoes_the_first_one(
    zones, tmp_path, first, then
):
    _, item = t_junction_with_a_gap(zones)
    zone, direct = gpkg(zones), tmp_path / "direct.gpkg"
    shutil.copy(zone, direct)
    review.decide_flag(direct, item.fid, then, annotator="Thandi")

    review.decide_flag(zone, item.fid, first, annotator="Sipho")
    review.decide_flag(zone, item.fid, then, annotator="Thandi")

    assert_same_rows(read(zone, "labels"), read(direct, "labels"))
    assert_same_rows(read(zone, "flags"), read(direct, "flags"))


@pytest.mark.parametrize("decision", ["real", "not_defect"])
def test_detect_again_leaves_a_decided_proposal_alone(
    zone, zones, shared_synthetic_zone, decision
):
    item, _ = gap_proposal(zone, shared_synthetic_zone)
    review.decide_flag(zone, item.fid, decision, annotator="Thandi")
    labels, flags = read(zone, "labels"), read(zone, "flags")

    run_detect(zones)

    assert_same_rows(read(zone, "labels"), labels)
    assert_same_rows(read(zone, "flags"), flags)


def test_a_zone_with_an_accepted_proposal_is_not_repackaged_without_overwrite(
    zone, zones, shared_synthetic_zone, baseline
):
    item, row = gap_proposal(zone, shared_synthetic_zone)
    review.decide_flag(zone, item.fid, "real", annotator="Thandi")

    result = run_package(
        shared_synthetic_zone, baseline, zones, "--zone", TRAIN_ZONE, expect_ok=False
    )

    assert result.exit_code != 0 and "--overwrite" in result.output
    assert read(zone, "labels").loc[row, "status"] == "added"


def test_a_restored_proposal_is_found_by_its_area(zone, shared_synthetic_zone):
    item, row = gap_proposal(zone, shared_synthetic_zone)
    connector = read(zone, "labels").geometry.loc[row]
    review.decide_flag(zone, item.fid, "not_defect", annotator="Thandi")
    review.decide_flag(zone, item.fid, "real", annotator="Thandi")

    found = pyogrio.read_dataframe(
        zone, layer="labels", bbox=connector.bounds, fid_as_index=True
    )
    assert row in found.index
    assert found.geometry.loc[row].equals_exact(connector, 0)


def test_accepting_a_proposal_edited_in_qgis_keeps_the_edit(
    zone, zones, shared_synthetic_zone
):
    item, row = gap_proposal(zone, shared_synthetic_zone)
    update_in_place(zones, "labels", "status", "deleted", [row])
    before = read(zone, "labels")

    review.decide_flag(zone, item.fid, "real", annotator="Thandi")

    assert_same_rows(read(zone, "labels"), before)
    assert read(zone, "flags").loc[item.fid, "decision"] == "real"


def test_a_road_two_connectors_end_on_keeps_each_vertex_until_its_own_undo(zones):
    # Two branches ending 15 m short of one kept through road.
    add_roads(
        zones,
        zone_line((150, 8), (240, 8)),
        zone_line((170, 24), (170, 9.5)),
        zone_line((220, 24), (220, 9.5)),
    )
    through = max(read(gpkg(zones), "labels").index) - 2
    update_in_place(zones, "labels", "status", "kept", [through])
    run_detect(zones)
    zone = gpkg(zones)
    before = read(zone, "labels").loc[through]
    flags = read(zone, "flags")
    first, second = [
        i
        for i in review.build_queue(zone)
        if i.kind == "proposal"
        and flags.geometry.loc[i.fid].distance(before.geometry) < 20
    ]
    review.decide_flag(zone, first.fid, "real", annotator="Thandi")
    review.decide_flag(zone, second.fid, "real", annotator="Sipho")

    review.decide_flag(zone, first.fid, "not_defect", annotator="Thandi")
    road = read(zone, "labels").loc[through]
    assert (road["status"], road["defect"], road["annotator"]) == (
        "modified",
        "gap",
        "Sipho",
    )
    assert len(road.geometry.coords) == len(before.geometry.coords) + 1

    review.decide_flag(zone, second.fid, "not_defect", annotator="Sipho")
    road = read(zone, "labels").loc[through]
    assert road["status"] == "kept" and pd.isna(road["annotator"])
    assert road.geometry.equals_exact(before.geometry, 0)
