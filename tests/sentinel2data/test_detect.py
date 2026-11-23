"""End-to-end tests of the ``detect`` command on the packaged synthetic zone.

Each test copies the packaged zone GeoPackages (and nothing else, so the
detectors can read only the GeoPackage and the config), runs ``detect`` and
inspects the ``flags`` and ``detector_runs`` layers.
"""

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
import yaml
from shapely.geometry import Point
from typer.testing import CliRunner

from sentinel2data.cli import app
from tests.sentinel2data.synthetic_zone import TEST_ZONE, TRAIN_ZONE, VAL_ZONE
from tests.sentinel2data.test_generate import run_generate
from tests.sentinel2data.test_package_zones import run_package

DEFAULT_CONFIG = (
    Path(__file__).parents[2] / "src" / "sentinel2data" / "configs" / "detectors.yaml"
)
ALL_ZONES = (TRAIN_ZONE, VAL_ZONE, TEST_ZONE)


@pytest.fixture(scope="module")
def baseline(shared_synthetic_zone, tmp_path_factory):
    return run_generate(shared_synthetic_zone, tmp_path_factory.mktemp("baseline"))


@pytest.fixture(scope="module")
def packaged(shared_synthetic_zone, baseline, tmp_path_factory):
    """All zones packaged once. Don't modify its files."""
    out = tmp_path_factory.mktemp("packaged")
    run_package(shared_synthetic_zone, baseline, out)
    return out


@pytest.fixture
def zones(packaged, tmp_path):
    """A zones folder holding only each zone's GeoPackage."""
    out = tmp_path / "zones"
    for zone in ALL_ZONES:
        (out / zone).mkdir(parents=True)
        shutil.copy(packaged / zone / f"{zone}.gpkg", out / zone)
    return out


def gpkg(zones, zone=TRAIN_ZONE):
    return zones / zone / f"{zone}.gpkg"


def run_detect(zones, *args, expect_ok=True):
    result = CliRunner().invoke(app, ["detect", "--zones-dir", str(zones), *args])
    if expect_ok:
        assert result.exit_code == 0, result.output + repr(result.exception)
    return result


def write_config(path, **stub):
    """The default detector config with the ``stub`` section changed."""
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    config["stub"].update(stub)
    path.write_text(yaml.safe_dump(config))
    return path


def read_flags(zones, zone=TRAIN_ZONE):
    return gpd.read_file(gpkg(zones, zone), layer="flags")


def read_stub_flags(zones, zone=TRAIN_ZONE):
    flags = read_flags(zones, zone)
    return flags[flags["type"] == "stub"]


def raw_id_of(zones, line):
    raw = gpd.read_file(gpkg(zones), layer="raw_labels")
    same = (raw.geometry.distance(line) < 1e-3) & (
        (raw.length - line.length).abs() < 1e-3
    )
    (raw_id,) = raw.loc[same, "raw_id"].tolist()
    return int(raw_id)


def test_dangling_stub_gets_a_stub_flag_at_its_dead_end(zones, shared_synthetic_zone):
    run_detect(zones)

    flags = read_stub_flags(zones)
    stub_road = shared_synthetic_zone.cases["dangling_stub"].geometries[1]
    dead_end = Point(stub_road.coords[-1])
    assert len(flags) == 1
    flag = flags.iloc[0]
    assert flag.geometry.geom_type == "Point"
    assert flag.geometry.distance(dead_end) < 1e-3
    assert (flag["type"], flag["detector"], flag["source"], flag["decision"]) == (
        "stub",
        "stub",
        "labels",
        "pending",
    )
    assert flag["raw_ids"] == str(raw_id_of(zones, stub_road))
    assert flag["flag_key"]
    assert pd.isna(flag["annotator"]) and pd.isna(flag["proposal_fid"])


@pytest.mark.parametrize(("max_length_m", "expected"), [(20.0, 0), (30.0, 1)])
def test_the_stub_threshold_in_the_config_sets_which_dead_ends_are_stubs(
    max_length_m, expected, zones, tmp_path
):
    # The dangling stub is 25 m long.
    config = write_config(tmp_path / "detectors.yaml", max_length_m=max_length_m)
    run_detect(zones, "--detector-config", str(config))
    assert len(read_stub_flags(zones)) == expected


def test_a_disabled_detector_places_no_flags(zones, tmp_path):
    run_detect(
        zones,
        "--detector-config",
        str(write_config(tmp_path / "detectors.yaml", enabled=False)),
    )
    assert len(read_stub_flags(zones)) == 0


def flags_near(flags, line, distance=1.0):
    return flags[flags.geometry.distance(line) <= distance]


def stub_only_config(path, max_length_m):
    """Only the stub detector, with ``max_length_m``. The re-run tests change
    this one threshold and count every flag."""
    write_config(path, max_length_m=max_length_m)
    config = yaml.safe_load(path.read_text())
    config["duplicate"]["enabled"] = config["crossing"]["enabled"] = False
    path.write_text(yaml.safe_dump(config))
    return str(path)


def long_config(tmp_path):
    return stub_only_config(tmp_path / "long.yaml", max_length_m=1000.0)


def short_config(tmp_path):
    return stub_only_config(tmp_path / "short.yaml", max_length_m=30.0)


@pytest.fixture
def long_stub_flags(zones, tmp_path):
    """Flags with a stub threshold long enough to make every dead-end road in the
    train zone a stub, edge-cut and out-of-scope roads included."""
    run_detect(zones, "--detector-config", long_config(tmp_path))
    return read_flags(zones)


def test_long_threshold_flags_the_dead_ends_of_the_gap_roads(
    long_stub_flags, shared_synthetic_zone
):
    for road in shared_synthetic_zone.cases["gap"].geometries:
        assert len(flags_near(long_stub_flags, road)) == 1


def test_a_road_the_zone_edge_cut_gets_no_flag(long_stub_flags, shared_synthetic_zone):
    # Its south end is a real dead end, but its length past the zone edge is unknown.
    (road,) = shared_synthetic_zone.cases["zone_edge"].geometries
    assert flags_near(long_stub_flags, road).empty


def test_a_road_only_in_tiles_that_are_not_kept_gets_no_flag(
    long_stub_flags, shared_synthetic_zone
):
    (road,) = shared_synthetic_zone.cases["edge_strip"].geometries
    assert flags_near(long_stub_flags, road).empty


def read_runs(zones, zone=TRAIN_ZONE):
    return pyogrio.read_dataframe(gpkg(zones, zone), layer="detector_runs")


def test_each_run_is_recorded_with_the_config_it_used(zones, tmp_path):
    run_detect(zones)
    run_detect(
        zones,
        "--detector-config",
        str(write_config(tmp_path / "detectors.yaml", max_length_m=20.0)),
    )

    runs = read_runs(zones)
    assert len(runs) == 2
    configs = [json.loads(c) for c in runs["config"]]
    assert configs[0] == yaml.safe_load(DEFAULT_CONFIG.read_text())
    assert configs[1]["stub"] == {"enabled": True, "max_length_m": 20.0}
    assert all(pd.Timestamp(t).tzinfo is not None for t in runs["run_at"])


def test_detect_runs_every_zone_by_default_and_prints_counts_per_zone_and_type(zones):
    result = run_detect(zones)
    for zone in ALL_ZONES:
        assert len(read_runs(zones, zone)) == 1
    assert f"{TRAIN_ZONE}: crossing 1, duplicate 1, stub 1" in result.output
    assert f"{VAL_ZONE}: no flags" in result.output
    assert "All zones: crossing 1, duplicate 1, stub 1" in result.output


def test_zone_selects_the_zones_to_run_on(zones):
    result = run_detect(zones, "--zone", TRAIN_ZONE)
    assert len(read_runs(zones)) == 1
    assert len(read_runs(zones, VAL_ZONE)) == 0
    assert VAL_ZONE not in result.output


def test_an_unknown_zone_is_rejected(zones):
    result = run_detect(zones, "--zone", "Nowhere", expect_ok=False)
    assert result.exit_code != 0 and "Nowhere" in result.output


def test_an_unknown_config_key_is_rejected(zones, tmp_path):
    result = run_detect(
        zones,
        "--detector-config",
        str(write_config(tmp_path / "c.yaml", max_lenght_m=5)),
        expect_ok=False,
    )
    assert result.exit_code != 0 and "max_lenght_m" in result.output


def read_flags_by_fid(zones, zone=TRAIN_ZONE):
    return pyogrio.read_dataframe(gpkg(zones, zone), layer="flags", fid_as_index=True)


def update_in_place(zones, layer, column, value, fids):
    """Set ``column`` on rows ``fids`` of ``layer`` the way QGIS saves an edit:
    in place, so fids stay.

    Any UPDATE compiles GDAL's R-tree triggers, which call ``ST_*`` functions
    plain SQLite lacks. Those trigger bodies run only when the fid changes, so
    stand-ins that are never called are enough.
    """
    with closing(sqlite3.connect(gpkg(zones))) as con, con:
        for name in ("ST_IsEmpty", "ST_MinX", "ST_MaxX", "ST_MinY", "ST_MaxY"):
            con.create_function(name, 1, lambda _, n=name: pytest.fail(f"{n} called"))
        con.executemany(
            f'UPDATE "{layer}" SET "{column}" = ? WHERE fid = ?',
            [(value, int(fid)) for fid in fids],
        )


def decide(zones, fids, decision, annotator="ann"):
    update_in_place(zones, "flags", "decision", decision, fids)
    update_in_place(zones, "flags", "annotator", annotator, fids)


def test_running_detect_twice_with_the_same_config_leaves_the_flags_unchanged(
    zones, tmp_path
):
    run_detect(zones, "--detector-config", long_config(tmp_path))
    before = read_flags_by_fid(zones)
    assert len(before) > 1

    run_detect(zones, "--detector-config", long_config(tmp_path))
    pd.testing.assert_frame_equal(read_flags_by_fid(zones), before)


def test_loosening_a_threshold_adds_pending_flags_without_duplicating(zones, tmp_path):
    run_detect(zones, "--detector-config", short_config(tmp_path))
    short = read_flags_by_fid(zones)
    run_detect(zones, "--detector-config", long_config(tmp_path))
    loose = read_flags_by_fid(zones)

    assert len(short) == 1 and len(loose) > 1
    assert not loose["flag_key"].duplicated().any()
    pd.testing.assert_frame_equal(loose.loc[short.index], short)
    assert (loose["decision"] == "pending").all()


def test_tightening_a_threshold_removes_only_the_pending_flags_it_no_longer_finds(
    zones, tmp_path
):
    run_detect(zones, "--detector-config", short_config(tmp_path))
    short = read_flags_by_fid(zones)
    run_detect(zones, "--detector-config", long_config(tmp_path))
    run_detect(zones, "--detector-config", short_config(tmp_path))

    pd.testing.assert_frame_equal(read_flags_by_fid(zones), short)


@pytest.mark.parametrize("decision", ["real", "not_defect", "needs_qgis"])
def test_a_decided_flag_survives_a_rerun_that_no_longer_finds_it(
    decision, zones, tmp_path
):
    run_detect(zones, "--detector-config", long_config(tmp_path))
    decide(zones, read_flags_by_fid(zones).index, decision)
    decided = read_flags_by_fid(zones)

    run_detect(zones, "--detector-config", short_config(tmp_path))
    pd.testing.assert_frame_equal(read_flags_by_fid(zones), decided)


def test_tightening_keeps_decided_flags_and_drops_only_pending_ones(zones, tmp_path):
    run_detect(zones, "--detector-config", short_config(tmp_path))
    (still_found,) = read_flags_by_fid(zones).index
    run_detect(zones, "--detector-config", long_config(tmp_path))
    decided_fid = read_flags_by_fid(zones).index.drop(still_found)[0]
    decide(zones, [decided_fid], "real")

    run_detect(zones, "--detector-config", short_config(tmp_path))
    after = read_flags_by_fid(zones)
    assert set(after.index) == {still_found, decided_fid}
    assert after.loc[decided_fid, ["decision", "annotator"]].tolist() == ["real", "ann"]
    assert after.loc[still_found, "decision"] == "pending"


def test_a_pending_flag_whose_roads_changed_is_replaced_with_the_current_raw_ids(
    zones, tmp_path, shared_synthetic_zone
):
    # An isolated branch of two rows, flagged at its start. Deleting its last
    # row leaves the start, and so the flag location, where it was.
    first, last = shared_synthetic_zone.cases["split_dead_end"].geometries
    run_detect(zones, "--detector-config", long_config(tmp_path))
    labels = pyogrio.read_dataframe(gpkg(zones), layer="labels", fid_as_index=True)
    last_fid = labels.index[labels["raw_id"] == raw_id_of(zones, last)]
    update_in_place(zones, "labels", "status", "deleted", last_fid)

    run_detect(zones, "--detector-config", long_config(tmp_path))
    assert flags_near(read_flags(zones), first)["raw_ids"].tolist() == [
        str(raw_id_of(zones, first))
    ]


def test_tightening_until_nothing_is_found_removes_every_pending_flag(zones, tmp_path):
    run_detect(zones, "--detector-config", short_config(tmp_path))
    run_detect(
        zones,
        "--detector-config",
        stub_only_config(tmp_path / "c.yaml", max_length_m=20.0),
    )
    assert len(read_flags(zones)) == 0


def test_a_disabled_detector_keeps_its_pending_flags(zones, tmp_path):
    run_detect(zones)
    before = read_flags_by_fid(zones)
    run_detect(
        zones,
        "--detector-config",
        str(write_config(tmp_path / "c.yaml", enabled=False)),
    )
    pd.testing.assert_frame_equal(read_flags_by_fid(zones), before)


def test_a_pending_flag_with_a_decided_flags_key_is_removed(zones, tmp_path):
    # Two flags with one key, as an older run or a manual edit could leave.
    # The pending one comes first.
    run_detect(zones, "--detector-config", short_config(tmp_path))
    (pending_fid,) = read_flags_by_fid(zones).index
    copy = read_flags(zones)
    pyogrio.write_dataframe(copy, gpkg(zones), layer="flags", append=True)
    decided_fid = read_flags_by_fid(zones).index.drop(pending_fid)[0]
    decide(zones, [decided_fid], "real")

    run_detect(zones, "--detector-config", short_config(tmp_path))
    assert read_flags_by_fid(zones).index.tolist() == [decided_fid]


def test_a_zone_open_in_qgis_is_refused_and_the_others_still_run(zones):
    wal = gpkg(zones).with_name(gpkg(zones).name + "-wal")
    wal.touch()
    result = run_detect(zones, expect_ok=False)
    wal.unlink()

    assert result.exit_code == 1 and TRAIN_ZONE in result.output
    assert len(read_runs(zones)) == 0 and len(read_flags(zones)) == 0
    assert len(read_runs(zones, VAL_ZONE)) == 1


def test_a_zone_with_only_detector_output_repackages_without_overwrite(
    zones, shared_synthetic_zone, baseline
):
    run_detect(zones, "--zone", TRAIN_ZONE)
    run_package(shared_synthetic_zone, baseline, zones, "--zone", TRAIN_ZONE)
    assert len(read_flags(zones)) == 0


def test_a_zone_packaged_before_the_flag_key_existed_is_refused(zones):
    old = read_flags(zones).drop(columns="flag_key")
    pyogrio.write_dataframe(old, gpkg(zones), layer="flags", geometry_type="Unknown")

    result = run_detect(zones, expect_ok=False)
    assert result.exit_code == 1
    assert TRAIN_ZONE in result.output and "package-zones" in result.output
    assert len(read_runs(zones)) == 0
    assert len(read_runs(zones, VAL_ZONE)) == 1


def test_a_long_dead_end_road_stored_as_several_rows_is_not_a_stub(
    zones, shared_synthetic_zone
):
    # Its last row is 20 m long, but the dead-end branch is 720 m.
    run_detect(zones)
    for road in shared_synthetic_zone.cases["split_dead_end"].geometries:
        assert flags_near(read_flags(zones), road).empty
