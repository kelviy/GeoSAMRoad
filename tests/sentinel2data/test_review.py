"""End-to-end tests of the review core: ``detect`` on the packaged synthetic
zone, then the review core driven on the train zone GeoPackage, with every
assertion read back from the GeoPackage.

After ``detect`` with the labels-only detectors, the train zone has flags in
both of its kept tiles, r0_c0 and r0_c1.
"""

import shutil
import subprocess
import sys

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
import shapely
import yaml
from typer.testing import CliRunner

from sentinel2data.cleaning import review
from sentinel2data.cleaning.contract import FLAG_FIELDS
from sentinel2data.cli import app
from tests.sentinel2data.synthetic_zone import TRAIN_ZONE
from tests.sentinel2data.test_detect import (
    base_config,
    gpkg,
    raw_id_of,
    run_detect,
    update_in_place,
)


@pytest.fixture
def zone(zones):
    """The train zone GeoPackage after ``detect``."""
    run_detect(zones)
    return gpkg(zones)


def read(zone, layer):
    return gpd.read_file(zone, layer=layer, fid_as_index=True)


def assert_same_rows(a, b):
    """Same values, ignoring dtype: a column read back with one value set gets
    a string dtype, and its empty cells read as NaN, not None."""

    def values(frame):
        return frame.astype(object).where(frame.notna(), None)

    pd.testing.assert_frame_equal(values(a), values(b))


def tile_of(tiles, point):
    """(row, col) of the tile holding ``point``, the northernmost-westernmost
    one when it lies on a shared edge."""
    hits = tiles[tiles.geometry.intersects(point)]
    return min(zip(hits["row"], hits["col"]))


def test_the_queue_lists_flags_then_kept_tiles_each_in_spatial_order(zone):
    queue = review.build_queue(zone)

    flags, tiles = read(zone, "flags"), read(zone, "tiles")
    kept = tiles.loc[tiles["status"] == "kept"].sort_values(by=["row", "col"])
    kinds = [type(item) for item in queue]
    assert kinds == [review.FlagItem] * len(flags) + [review.TileItem] * len(kept)
    flag_items = queue[: len(flags)]
    assert sorted(i.fid for i in flag_items) == sorted(flags.index)
    assert [i.fid for i in queue[len(flags) :]] == kept.index.tolist()

    # Proposals come first. Each group sweeps the kept tiles in row then column
    # order, north to south and west to east inside each tile.
    points = shapely.point_on_surface(
        flags.geometry.loc[[i.fid for i in flag_items]].to_numpy()
    )
    order = [
        (i.kind != "proposal", *tile_of(tiles, p), -shapely.get_y(p), shapely.get_x(p))
        for i, p in zip(flag_items, points)
    ]
    assert order == sorted(order)
    assert {o[1:3] for o in order} == {(0, 0), (0, 1)}
    assert review.build_queue(zone) == queue


def test_the_current_item_is_the_first_one_not_done_and_its_extent_holds_it(zone):
    queue = review.build_queue(zone)
    first, second = queue[0], queue[1]
    assert review.current_item(zone) == first
    flag = read(zone, "flags").geometry.loc[first.fid]
    assert shapely.contains(
        shapely.box(*first.extent), shapely.buffer(flag, review.FLAG_MARGIN_M - 1)
    )

    review.decide_flag(zone, first.fid, "not_defect", annotator="ann")
    assert review.current_item(zone) == second


@pytest.mark.parametrize("decision", ["real", "not_defect", "needs_qgis"])
def test_each_flag_decision_is_recorded_with_the_annotator(zone, decision):
    flag = review.build_queue(zone)[0]
    review.decide_flag(zone, flag.fid, decision, annotator="Thandi")

    stored = read(zone, "flags").loc[flag.fid]
    assert (stored["decision"], stored["annotator"]) == (decision, "Thandi")
    others = read(zone, "flags").drop(index=flag.fid)
    assert bool((others["decision"] == "pending").all())
    assert bool(others["annotator"].isna().all())


def flag_of_type(zone, flag_type):
    (item,) = [
        i for i in review.build_queue(zone) if getattr(i, "type", None) == flag_type
    ]
    return item


def test_real_on_a_stub_flag_deletes_its_label_row(zone, zones, shared_synthetic_zone):
    stub_road = shared_synthetic_zone.cases["dangling_stub"].geometries[1]
    stub_raw_id = raw_id_of(zones, stub_road)
    before = read(zone, "labels")

    review.decide_flag(zone, flag_of_type(zone, "stub").fid, "real", annotator="Thandi")

    after = read(zone, "labels")
    assert len(after) == len(before)  # a deletion never removes a row
    deleted = after[after["status"] == "deleted"]
    assert deleted["raw_id"].tolist() == [stub_raw_id]
    assert list(zip(deleted["defect"], deleted["annotator"])) == [("stub", "Thandi")]
    untouched = after.index != deleted.index[0]
    assert_same_rows(after[untouched], before[untouched])


def add_phantom_flag(zone, area, raw_ids):
    """Append a pending ``phantom?`` flag over ``area`` naming ``raw_ids``, the
    way the second-source detector writes one. Returns its fid."""
    flag = {name: None for name in FLAG_FIELDS}
    flag |= {
        "flag_key": f"overture:phantom?:{','.join(map(str, raw_ids))}@test",
        "type": "phantom?",
        "detector": "overture",
        "source": "overture",
        "raw_ids": ",".join(map(str, sorted(raw_ids))),
        "decision": "pending",
    }
    row = gpd.GeoDataFrame([flag], geometry=[area], crs=read(zone, "labels").crs)
    pyogrio.write_dataframe(row.astype(FLAG_FIELDS), zone, layer="flags", append=True)
    return int(read(zone, "flags").index.to_numpy().max())


def split_dead_end(zones, shared_synthetic_zone):
    """The two rows of the 720 m dead-end Street: (long row, 20 m last row),
    each with its raw_id."""
    long, last = sorted(
        shared_synthetic_zone.cases["split_dead_end"].geometries,
        key=lambda g: -g.length,
    )
    return (long, raw_id_of(zones, long)), (last, raw_id_of(zones, last))


def phantom_area(*lines):
    """The area the second-source detector puts over ``lines``: their bounding
    box grown by the 15 m match tolerance."""
    return shapely.box(*shapely.union_all(lines).buffer(15.0).bounds)


def test_real_on_a_phantom_flag_deletes_the_rows_it_names(
    zone, zones, shared_synthetic_zone
):
    (long, long_id), (last, last_id) = split_dead_end(zones, shared_synthetic_zone)
    area = phantom_area(long, last)
    fid = add_phantom_flag(zone, area, [long_id, last_id])

    deleted = review.decide_flag(zone, fid, "real", annotator="Thandi")

    labels = read(zone, "labels")
    rows = labels[labels["raw_id"].isin([long_id, last_id])]
    assert sorted(deleted) == sorted(rows.index)
    assert (
        list(zip(rows["status"], rows["defect"], rows["annotator"]))
        == [("deleted", "phantom", "Thandi")] * 2
    )


def test_real_on_a_phantom_flag_keeps_named_rows_that_run_outside_it(
    zone, zones, shared_synthetic_zone
):
    (_, long_id), (last, last_id) = split_dead_end(zones, shared_synthetic_zone)
    fid = add_phantom_flag(zone, last.buffer(15.0).envelope, [long_id, last_id])

    review.decide_flag(zone, fid, "real", annotator="Thandi")

    status = read(zone, "labels").set_index("raw_id")["status"]
    assert (status[last_id], status[long_id]) == ("deleted", "kept")


def test_real_on_a_phantom_flag_with_no_named_row_inside_it_is_refused(
    zone, zones, shared_synthetic_zone
):
    (long, long_id), _ = split_dead_end(zones, shared_synthetic_zone)
    middle = shapely.line_interpolate_point(long, 0.5, normalized=True)
    fid = add_phantom_flag(zone, middle.buffer(50.0).envelope, [long_id])
    before = read(zone, "labels")

    with pytest.raises(review.NothingToDelete, match="needs_qgis"):
        review.decide_flag(zone, fid, "real", annotator="Thandi")

    assert_same_rows(read(zone, "labels"), before)
    assert read(zone, "flags").loc[fid, "decision"] == "pending"


@pytest.mark.parametrize("decision", ["not_defect", "needs_qgis"])
@pytest.mark.parametrize("flag_type", ["stub", "phantom?"])
def test_not_defect_and_needs_qgis_change_no_label(
    zone, zones, shared_synthetic_zone, decision, flag_type
):
    if flag_type == "stub":
        fid = flag_of_type(zone, "stub").fid
    else:
        (long, long_id), (last, last_id) = split_dead_end(zones, shared_synthetic_zone)
        area = phantom_area(long, last)
        fid = add_phantom_flag(zone, area, [long_id, last_id])
    before = read(zone, "labels")

    assert review.decide_flag(zone, fid, decision, annotator="Thandi") == ()

    assert_same_rows(read(zone, "labels"), before)
    assert read(zone, "flags").loc[fid, "decision"] == decision


def test_the_same_decision_again_changes_nothing(zone):
    fid = flag_of_type(zone, "stub").fid
    review.decide_flag(zone, fid, "real", annotator="Thandi")
    labels = read(zone, "labels")

    assert review.decide_flag(zone, fid, "real", annotator="Sipho") == ()

    assert_same_rows(read(zone, "labels"), labels)
    assert read(zone, "flags").loc[fid, "annotator"] == "Thandi"


@pytest.mark.parametrize(
    ("decision", "annotator"),
    [("pending", "Thandi"), ("maybe", "Thandi"), ("real", " ")],
)
def test_a_bad_decision_or_annotator_is_refused(zone, decision, annotator):
    fid = flag_of_type(zone, "stub").fid
    with pytest.raises(ValueError):
        review.decide_flag(zone, fid, decision, annotator=annotator)
    assert read(zone, "flags").loc[fid, "decision"] == "pending"


def test_an_unknown_flag_is_refused(zone):
    with pytest.raises(ValueError, match="no flag"):
        review.decide_flag(zone, 9999, "real", annotator="Thandi")


def kept_tiles(zone):
    return [i for i in review.build_queue(zone) if isinstance(i, review.TileItem)]


def test_marking_a_kept_tile_reviewed_stores_the_note_and_annotator(zone):
    first, second = kept_tiles(zone)
    review.review_tile(
        zone, first.fid, annotator="Thandi", note="Dirt road in NE corner"
    )
    review.review_tile(zone, second.fid, annotator="Sipho")

    tiles = read(zone, "tiles")
    assert tiles.loc[first.fid, ["reviewed", "note", "annotator"]].tolist() == [
        True,
        "Dirt road in NE corner",
        "Thandi",
    ]
    assert bool(tiles.loc[second.fid, "reviewed"]) and pd.isna(
        tiles.loc[second.fid, "note"]
    )
    assert tiles.loc[second.fid, "annotator"] == "Sipho"
    others = tiles.drop(index=[first.fid, second.fid])
    assert not bool(others["reviewed"].any())
    assert bool(others["annotator"].isna().all())


def test_only_kept_tiles_can_be_reviewed(zone):
    tiles = read(zone, "tiles")
    dropped = int(tiles.index[tiles["status"] != "kept"][0])
    with pytest.raises(ValueError, match="not a kept tile"):
        review.review_tile(zone, dropped, annotator="Thandi")
    assert not bool(read(zone, "tiles")["reviewed"].any())


def test_progress_counts_a_mix_of_decisions(zone):
    flags = [i for i in review.build_queue(zone) if isinstance(i, review.FlagItem)]
    assert len(flags) >= 3
    start = review.progress(zone)
    assert (start.flags_decided, start.flags_total) == (0, len(flags))
    assert (start.tiles_reviewed, start.tiles_kept) == (0, 2)

    review.decide_flag(zone, flags[0].fid, "real", annotator="Thandi")
    review.decide_flag(zone, flags[1].fid, "needs_qgis", annotator="Thandi")
    review.review_tile(zone, kept_tiles(zone)[1].fid, annotator="Thandi")

    now = review.progress(zone)
    assert (now.flags_decided, now.tiles_reviewed) == (2, 1)
    assert (now.done, now.total, now.remaining) == (3, len(flags) + 2, len(flags) - 1)


def test_a_copy_of_the_geopackage_shows_the_same_decisions(zone, tmp_path):
    """Nothing is kept in memory only: a copy of the file, opened fresh, has
    the same queue."""
    queue = review.build_queue(zone)
    review.decide_flag(zone, queue[0].fid, "not_defect", annotator="Thandi")
    review.review_tile(zone, kept_tiles(zone)[0].fid, annotator="Thandi", note="ok")

    copy = shutil.copy(zone, tmp_path / "copy.gpkg")
    assert review.build_queue(copy) == review.build_queue(zone)
    assert review.current_item(copy) == queue[1]
    assert review.progress(copy).done == 2


def test_the_review_core_imports_nothing_from_qgis():
    code = (
        "import sys, sentinel2data.cleaning.review\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('qgis', 'PyQt5', 'PyQt6'))\n"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_progress_command_prints_each_zones_progress(zones):
    run_detect(zones)
    zone = gpkg(zones)
    queue = review.build_queue(zone)
    review.decide_flag(zone, queue[0].fid, "real", annotator="Thandi")
    review.review_tile(zone, kept_tiles(zone)[0].fid, annotator="Thandi")
    n_flags = sum(isinstance(i, review.FlagItem) for i in queue)

    result = CliRunner().invoke(
        app, ["progress", "--zones-dir", str(zones), "--zone", TRAIN_ZONE]
    )

    assert result.exit_code == 0, result.output
    expected = (
        f"{TRAIN_ZONE}: 2/{n_flags + 2} done, {n_flags} remaining "
        f"(flags 1/{n_flags} decided, kept tiles 1/2 reviewed)"
    )
    assert result.output.splitlines() == [expected]


def test_reviewing_a_tile_again_without_a_note_keeps_its_note(zone):
    tile = kept_tiles(zone)[0]
    review.review_tile(zone, tile.fid, annotator="Thandi", note="Check the NE track")
    review.review_tile(zone, tile.fid, annotator="Sipho")
    stored = read(zone, "tiles").loc[tile.fid]
    assert (stored["note"], stored["annotator"]) == ("Check the NE track", "Sipho")


def test_real_leaves_a_row_already_deleted_as_it_is(zone, zones, shared_synthetic_zone):
    (long, long_id), (last, last_id) = split_dead_end(zones, shared_synthetic_zone)
    fid = add_phantom_flag(zone, phantom_area(long, last), [long_id, last_id])
    labels = read(zone, "labels")
    (last_fid,) = labels.index[labels["raw_id"] == last_id]
    # Deleted in QGIS, as a misalignment.
    for column, value in [
        ("status", "deleted"),
        ("defect", "misalignment"),
        ("annotator", "Sipho"),
    ]:
        update_in_place(zones, "labels", column, value, [int(last_fid)])

    deleted = review.decide_flag(zone, fid, "real", annotator="Thandi")

    stored = read(zone, "labels").loc[last_fid]
    assert last_fid not in deleted
    assert (stored["defect"], stored["annotator"]) == ("misalignment", "Sipho")


def test_changing_real_to_not_defect_on_a_stub_restores_its_label_row(zone):
    fid = flag_of_type(zone, "stub").fid
    before = read(zone, "labels")
    review.decide_flag(zone, fid, "real", annotator="Thandi")

    review.decide_flag(zone, fid, "not_defect", annotator="Sipho")

    assert_same_rows(read(zone, "labels"), before)
    stored = read(zone, "flags").loc[fid]
    assert (stored["decision"], stored["annotator"]) == ("not_defect", "Sipho")


def test_changing_a_real_stub_restores_a_modified_row_with_its_own_defect(
    zone, zones, shared_synthetic_zone
):
    stub_road = shared_synthetic_zone.cases["dangling_stub"].geometries[1]
    labels = read(zone, "labels")
    (row,) = labels.index[labels["raw_id"] == raw_id_of(zones, stub_road)]
    for column, value in [
        ("status", "modified"),
        ("defect", "misalignment"),
        ("annotator", "Sipho"),
    ]:
        update_in_place(zones, "labels", column, value, [int(row)])
    before = read(zone, "labels")
    fid = flag_of_type(zone, "stub").fid

    assert review.decide_flag(zone, fid, "real", annotator="Thandi") == (row,)
    review.decide_flag(zone, fid, "needs_qgis", annotator="Thandi")

    assert_same_rows(read(zone, "labels"), before)


def test_changing_not_defect_to_real_applies_the_deletion(zone):
    fid = flag_of_type(zone, "stub").fid
    review.decide_flag(zone, fid, "not_defect", annotator="Thandi")

    deleted = review.decide_flag(zone, fid, "real", annotator="Sipho")

    labels = read(zone, "labels")
    assert labels.index[labels["status"] == "deleted"].tolist() == list(deleted)
    assert len(deleted) == 1
    assert labels.loc[deleted[0], ["defect", "annotator"]].tolist() == ["stub", "Sipho"]
    assert read(zone, "flags").loc[fid, "annotator"] == "Sipho"


def test_undo_keeps_a_row_changed_in_qgis_after_the_deletion(zone, zones):
    fid = flag_of_type(zone, "stub").fid
    (row,) = review.decide_flag(zone, fid, "real", annotator="Thandi")
    # Restored by hand in QGIS, then redrawn.
    update_in_place(zones, "labels", "status", "modified", [int(row)])
    update_in_place(zones, "labels", "defect", "misalignment", [int(row)])

    review.decide_flag(zone, fid, "not_defect", annotator="Thandi")

    stored = read(zone, "labels").loc[row]
    assert (stored["status"], stored["defect"]) == ("modified", "misalignment")


@pytest.mark.parametrize(
    ("queue_filter", "wanted"),
    [
        (review.QueueFilter(kind="proposal"), lambda f: f["proposal_fid"].notna()),
        (review.QueueFilter(kind="flag"), lambda f: f["proposal_fid"].isna()),
        (review.QueueFilter(flag_type="stub"), lambda f: f["type"] == "stub"),
        (review.QueueFilter(source="overture"), lambda f: f["source"] == "overture"),
        (
            review.QueueFilter(kind="flag", source="labels"),
            lambda f: f["proposal_fid"].isna() & (f["source"] == "labels"),
        ),
    ],
)
def test_a_filter_keeps_exactly_its_flags_in_the_default_order(
    zone, zones, shared_synthetic_zone, queue_filter, wanted
):
    (long, long_id), (last, last_id) = split_dead_end(zones, shared_synthetic_zone)
    add_phantom_flag(zone, phantom_area(long, last), [long_id, last_id])
    flags = read(zone, "flags")
    expected = set(flags.index[wanted(flags)])
    assert expected and expected != set(flags.index)

    filtered = review.build_queue(zone, queue_filter)

    everything = review.build_queue(zone)
    assert filtered == [i for i in everything if i.fid in expected and i.kind != "tile"]


def test_the_tile_filter_keeps_only_the_kept_tiles(zone):
    tiles = review.build_queue(zone, review.QueueFilter(kind="tile"))
    assert tiles == kept_tiles(zone)
    assert len(tiles) == 2


def test_an_unknown_kind_flag_type_or_source_is_refused():
    with pytest.raises(ValueError, match="kind"):
        review.QueueFilter(kind="proposals")
    with pytest.raises(ValueError, match="flag type"):
        review.QueueFilter(flag_type="phantom")
    with pytest.raises(ValueError, match="source"):
        review.QueueFilter(source="osm")


def detect_with_stub_hidden(zones):
    config = base_config()
    config["stub"]["hidden"] = True
    path = zones.parent / "stub-hidden.yaml"
    path.write_text(yaml.safe_dump(config))
    run_detect(zones, "--detector-config", str(path))
    return gpkg(zones)


def test_a_hidden_detectors_flags_leave_the_queue_and_progress_only(zones):
    zone = detect_with_stub_hidden(zones)

    flags = read(zone, "flags")
    stubs = set(flags.index[flags["detector"] == "stub"])
    assert stubs  # still in the GeoPackage
    queue = review.build_queue(zone)
    assert not stubs & {i.fid for i in queue if i.kind != "tile"}
    assert len(queue) == len(flags) - len(stubs) + 2
    assert review.progress(zone).flags_total == len(flags) - len(stubs)
    assert review.build_queue(zone, review.QueueFilter(flag_type="stub")) == []


def test_showing_a_detector_again_brings_its_flags_back(zones):
    zone = detect_with_stub_hidden(zones)
    run_detect(zones)
    assert review.progress(zone).flags_total == len(read(zone, "flags"))


def test_going_back_steps_to_the_previous_item_and_reopening_resumes_there(
    zone, tmp_path
):
    queue = review.build_queue(zone)
    for item in queue[:3]:
        review.decide_flag(zone, item.fid, "not_defect", annotator="Thandi")
    queue = review.build_queue(zone)
    assert review.current_item(zone) == queue[3]

    assert review.go_back(zone) == queue[2]
    assert review.go_back(zone) == queue[1]

    copy = shutil.copy(zone, tmp_path / "reopened.gpkg")
    assert review.current_item(copy) == queue[1]
    assert review.go_forward(copy) == queue[2]
    assert review.current_item(copy) == queue[2]


def test_changing_a_decision_after_going_back_returns_to_the_first_item_not_done(
    zone,
):
    queue = review.build_queue(zone)
    review.decide_flag(zone, queue[0].fid, "real", annotator="Thandi")
    review.decide_flag(zone, queue[1].fid, "real", annotator="Thandi")
    queue = review.build_queue(zone)

    assert review.go_back(zone) == queue[1]
    review.decide_flag(zone, queue[1].fid, "not_defect", annotator="Sipho")

    assert review.current_item(zone) == queue[2]


def test_going_back_from_the_first_item_or_forward_from_the_last_stays_put(zone):
    queue = review.build_queue(zone)
    assert review.go_back(zone) is None
    assert review.current_item(zone) == queue[0]

    for item in queue:
        if item.kind == "tile":
            review.review_tile(zone, item.fid, annotator="Thandi")
        else:
            review.decide_flag(zone, item.fid, "not_defect", annotator="Thandi")
    assert review.current_item(zone) is None
    queue = review.build_queue(zone)
    assert review.go_back(zone) == queue[-1]
    assert review.go_forward(zone) is None
    assert review.current_item(zone) == queue[-1]


def test_going_back_under_a_filter_steps_through_the_filtered_queue(zone):
    tiles = review.QueueFilter(kind="tile")
    first, second = kept_tiles(zone)
    review.review_tile(zone, first.fid, annotator="Thandi")
    review.review_tile(zone, second.fid, annotator="Thandi")
    first, second = kept_tiles(zone)

    assert review.go_back(zone, tiles) == second
    assert review.go_back(zone, tiles) == first
    # Unfiltered, the queue starts at its first flag, still pending; the
    # stored tile is in this queue too, so it stays current.
    assert review.current_item(zone) == first
    stubs = review.QueueFilter(flag_type="stub")
    assert review.current_item(zone, stubs) == flag_of_type(zone, "stub")


def test_the_needs_qgis_list_has_every_such_flag_with_its_location(zones):
    zone = detect_with_stub_hidden(zones)
    flags = read(zone, "flags")
    stub = int(flags.index[flags["detector"] == "stub"][0])
    other = review.build_queue(zone)[0].fid
    for fid in (stub, other):
        update_in_place(zones, "flags", "decision", "needs_qgis", [fid])
    review.decide_flag(zone, review.build_queue(zone)[1].fid, "real", annotator="T")

    items = review.needs_qgis_items(zone)

    # A hidden detector's flag still needs fixing.
    assert sorted(i.fid for i in items) == sorted([stub, other])
    for item in items:
        assert shapely.Point(item.location).within(
            flags.geometry.loc[item.fid].buffer(1e-6)
        )


def test_the_needs_qgis_command_lists_each_zones_flags(zone, zones):
    (flag, *_) = review.build_queue(zone, review.QueueFilter(flag_type="stub"))
    assert isinstance(flag, review.FlagItem)
    review.decide_flag(zone, flag.fid, "needs_qgis", annotator="Thandi")
    x, y = flag.location

    result = CliRunner().invoke(app, ["needs-qgis", "--zones-dir", str(zones)])

    assert result.exit_code == 0, result.output
    assert f"{TRAIN_ZONE}: flag {flag.fid} {flag.type} at {x:.1f}, {y:.1f}" in (
        result.output.splitlines()
    )


def test_undo_keeps_a_row_another_real_flag_also_deletes(
    zone, zones, shared_synthetic_zone
):
    (long, long_id), (last, last_id) = split_dead_end(zones, shared_synthetic_zone)
    first = add_phantom_flag(zone, phantom_area(long, last), [long_id, last_id])
    second = add_phantom_flag(zone, last.buffer(15.0).envelope, [last_id])
    review.decide_flag(zone, first, "real", annotator="Thandi")
    review.decide_flag(zone, second, "real", annotator="Thandi")

    review.decide_flag(zone, first, "not_defect", annotator="Thandi")
    status = read(zone, "labels").set_index("raw_id")["status"]
    assert (status[long_id], status[last_id]) == ("kept", "deleted")

    review.decide_flag(zone, second, "not_defect", annotator="Thandi")
    assert read(zone, "labels").set_index("raw_id")["status"][last_id] == "kept"
