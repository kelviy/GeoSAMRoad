"""End-to-end tests of the ``detect`` command on the packaged synthetic zone.

Each test copies the packaged zone GeoPackages (and nothing else, so the
detectors can read only the GeoPackage and the config), runs ``detect`` and
inspects the ``flags`` and ``detector_runs`` layers.
"""

import json
import shutil
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


def raw_id_of(zones, line):
    raw = gpd.read_file(gpkg(zones), layer="raw_labels")
    same = (raw.geometry.distance(line) < 1e-3) & (
        (raw.length - line.length).abs() < 1e-3
    )
    (raw_id,) = raw.loc[same, "raw_id"].tolist()
    return int(raw_id)


def test_dangling_stub_gets_a_stub_flag_at_its_dead_end(zones, shared_synthetic_zone):
    run_detect(zones)

    flags = read_flags(zones)
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
    assert len(read_flags(zones)) == expected


def test_a_disabled_detector_places_no_flags(zones, tmp_path):
    run_detect(
        zones,
        "--detector-config",
        str(write_config(tmp_path / "detectors.yaml", enabled=False)),
    )
    assert len(read_flags(zones)) == 0


def flags_near(flags, line, distance=1.0):
    return flags[flags.geometry.distance(line) <= distance]


@pytest.fixture
def long_stub_flags(zones, tmp_path):
    """Flags with a stub threshold long enough to make every dead-end road in the
    train zone a stub, edge-cut and out-of-scope roads included."""
    run_detect(
        zones,
        "--detector-config",
        str(write_config(tmp_path / "detectors.yaml", max_length_m=1000.0)),
    )
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
    assert f"{TRAIN_ZONE}: stub 1" in result.output
    assert f"{VAL_ZONE}: no flags" in result.output
    assert "All zones: stub 1" in result.output


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


def test_a_rerun_replaces_pending_flags_instead_of_adding_copies(zones):
    run_detect(zones)
    run_detect(zones)
    assert len(read_flags(zones)) == 1


def decide_all_flags(zones, decision, annotator="ann"):
    flags = gpd.read_file(gpkg(zones), layer="flags")
    flags["decision"], flags["annotator"] = decision, annotator
    flags.to_file(gpkg(zones), layer="flags", driver="GPKG")


@pytest.mark.parametrize("decision", ["real", "not_defect", "needs_qgis"])
def test_a_rerun_never_touches_a_decided_flag(decision, zones):
    run_detect(zones)
    decide_all_flags(zones, decision)
    before = read_flags(zones)

    run_detect(zones)
    after = read_flags(zones)
    pd.testing.assert_frame_equal(after, before)


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
