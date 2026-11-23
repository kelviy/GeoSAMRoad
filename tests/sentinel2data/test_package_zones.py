"""End-to-end tests of the ``package-zones`` command on the synthetic zone.

The baseline dataset is generated once from the synthetic zone, then each
test packages it and inspects the zone folder: the zone GeoPackage, the
Sentinel-2 RGB raster and the QGIS project.
"""
import dataclasses
import sqlite3
import json
import os
import shutil
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import geopandas as gpd
import pandas as pd
import numpy as np
import pyogrio
import pytest
import rasterio
from rasterio.windows import Window
import shapely
import shapely.affinity
from shapely.geometry import LineString, Point
from typer.testing import CliRunner

from sentinel2data.cli import app
from tests.sentinel2data.synthetic_zone import TEST_ZONE, TRAIN_ZONE, VAL_ZONE, SyntheticZone
from tests.sentinel2data.test_generate import run_generate

# The zone GeoPackage contract (KEL-5): layer -> fields, in order.
LABEL_ROAD_FIELDS = ["data_source", "road_class", "scale_class", "buffer", "surface"]
CONTRACT = {
    "raw_labels": ["raw_id", *LABEL_ROAD_FIELDS],
    "labels": ["raw_id", "status", "defect", "annotator", *LABEL_ROAD_FIELDS],
    "tiles": ["tile_id", "row", "col", "status", "reviewed", "note", "annotator"],
    "flags": ["flag_key", "type", "detector", "source", "raw_ids", "proposal_fid", "decision", "annotator"],
    "detector_runs": ["run_at", "config"],
}


@pytest.fixture(scope="module")
def baseline(shared_synthetic_zone, tmp_path_factory):
    """The baseline dataset generated from the shared synthetic zone."""
    return run_generate(shared_synthetic_zone, tmp_path_factory.mktemp("baseline"))


def run_package(zone: SyntheticZone, dataset_dir, output_dir, *extra_args, expect_ok=True):
    result = CliRunner().invoke(
        app,
        [
            "package-zones",
            "--roads", str(zone.roads_path),
            "--imagery-dir", str(zone.imagery_dir),
            "--dataset-dir", str(dataset_dir),
            "--output-dir", str(output_dir),
            *extra_args,
        ],
    )
    if expect_ok:
        assert result.exit_code == 0, result.output + repr(result.exception)
    return result


@pytest.fixture(scope="module")
def packaged(shared_synthetic_zone, baseline, tmp_path_factory):
    """All zones packaged once. Don't modify its files."""
    out = tmp_path_factory.mktemp("packaged")
    run_package(shared_synthetic_zone, baseline, out)
    return out


def gpkg_path(out, zone=TRAIN_ZONE):
    return out / zone / f"{zone}.gpkg"


def test_zone_geopackage_has_exactly_the_contract_layers_and_fields(packaged):
    path = gpkg_path(packaged)
    assert sorted(name for name, _ in pyogrio.list_layers(path)) == sorted(CONTRACT)
    for layer, fields in CONTRACT.items():
        assert list(pyogrio.read_info(path, layer=layer)["fields"]) == fields, layer


def read_layer(out, layer, zone=TRAIN_ZONE):
    return gpd.read_file(gpkg_path(out, zone), layer=layer)


def test_raw_labels_are_the_zone_baseline_roads_clipped_to_the_zone(packaged, shared_synthetic_zone):
    raw = read_layer(packaged, "raw_labels")
    assert raw.crs.to_string() == shared_synthetic_zone.crs
    # Every source road of the train zone except Track and Slipway.
    kept_cases = [c for c in shared_synthetic_zone.cases.values() if c.name not in ("track", "slipway")]
    assert len(raw) == sum(len(c.road_ids) for c in kept_cases)
    assert not set(raw["road_class"]) & {"Track", "Slipway"}
    assert sorted(raw["raw_id"]) == list(range(1, len(raw) + 1))
    # The zone-edge road runs 50 px inside the zone and 20 px past its north edge.
    edge = raw[raw["road_class"] == "Secondary Road"]
    assert edge.length.tolist() == [pytest.approx(500.0)]


def test_labels_mirror_raw_labels_row_for_row(packaged):
    raw = read_layer(packaged, "raw_labels").set_index("raw_id")
    labels = read_layer(packaged, "labels").set_index("raw_id")
    assert sorted(labels.index) == sorted(raw.index)
    assert set(labels["status"]) == {"kept"}
    assert labels["defect"].isna().all() and labels["annotator"].isna().all()
    for raw_id, row in raw.iterrows():
        assert labels.geometry[raw_id].equals_exact(row.geometry, 0)
        assert labels.loc[raw_id, ["road_class", "buffer"]].tolist() == [row["road_class"], row["buffer"]]


def raw_id_map(out):
    raw = read_layer(out, "raw_labels")
    return dict(zip(raw["raw_id"], raw.geometry.to_wkb()))


def test_raw_id_is_stable_across_repackaging_and_road_row_order(
    packaged, shared_synthetic_zone, baseline, tmp_path
):
    again = tmp_path / "again"
    run_package(shared_synthetic_zone, baseline, again)
    assert raw_id_map(again) == raw_id_map(packaged)

    shuffled = shared_synthetic_zone.read_roads().sample(frac=1.0, random_state=7)
    shuffled_path = tmp_path / "shuffled.parquet"
    shuffled.to_parquet(shuffled_path, write_covering_bbox=True)
    zone = dataclasses.replace(shared_synthetic_zone, roads_path=shuffled_path)
    run_package(zone, baseline, tmp_path / "shuffled")
    assert raw_id_map(tmp_path / "shuffled") == raw_id_map(packaged)


def test_tiles_layer_matches_the_tile_status_table_including_untiled_strips(packaged, baseline):
    for zone in (TRAIN_ZONE, VAL_ZONE, TEST_ZONE):
        tiles = read_layer(packaged, "tiles", zone).set_index("tile_id")
        table = gpd.read_parquet(baseline / "tile_status" / f"{zone}.parquet").set_index("tile_id")
        assert sorted(tiles.index) == sorted(table.index), zone
        assert tiles.crs == table.crs
        for tile_id, row in table.iterrows():
            assert tiles.loc[tile_id, "status"] == row["status"], tile_id
            assert (tiles.loc[tile_id, "row"], tiles.loc[tile_id, "col"]) == (row["row"], row["col"])
            assert tiles.geometry[tile_id].equals(row.geometry), tile_id
        assert not tiles["reviewed"].any()
        assert tiles["note"].isna().all() and tiles["annotator"].isna().all()
    train = read_layer(packaged, "tiles")
    assert (train["status"] == "untiled").sum() == 5


def test_flags_layer_is_empty_and_holds_points_or_areas(packaged):
    info = pyogrio.read_info(gpkg_path(packaged), layer="flags")
    assert info["features"] == 0
    assert info["geometry_type"] == "Unknown"


def test_detector_run_layer_is_an_empty_table(packaged):
    info = pyogrio.read_info(gpkg_path(packaged), layer="detector_runs")
    assert info["features"] == 0
    assert info["geometry_type"] is None


def test_every_zone_is_packaged_by_default_and_zone_selects_one(packaged, shared_synthetic_zone, baseline, tmp_path):
    for zone in (TRAIN_ZONE, VAL_ZONE, TEST_ZONE):
        assert gpkg_path(packaged, zone).is_file()
    run_package(shared_synthetic_zone, baseline, tmp_path, "--zone", VAL_ZONE)
    assert [p.name for p in tmp_path.iterdir()] == [VAL_ZONE]


def test_unknown_zone_is_rejected(shared_synthetic_zone, baseline, tmp_path):
    result = run_package(shared_synthetic_zone, baseline, tmp_path, "--zone", "Nowhere", expect_ok=False)
    assert result.exit_code != 0
    assert "Nowhere" in result.output


def edit_layer(layer, change):
    """An annotator's edit: read a layer of the train zone, change it, write it back."""
    def edit(out):
        gdf = read_layer(out, layer)
        gdf = change(gdf)
        gdf.to_file(gpkg_path(out), layer=layer, driver="GPKG")
    return edit


def set_value(column, value, where=lambda gdf: gdf.index == 0):
    def change(gdf):
        gdf.loc[where(gdf), column] = value
        return gdf
    return change


def move_first(gdf):
    gdf.loc[0, "geometry"] = shapely.affinity.translate(gdf.geometry[0], xoff=15.0)
    return gdf


def append(row):
    def change(gdf):
        return pd.concat([gdf, gpd.GeoDataFrame([row], geometry="geometry", crs=gdf.crs)], ignore_index=True)
    return change


def add_rows(out, layer, rows):
    """Append rows to a layer of the train zone, keeping the fids already there."""
    crs = pyogrio.read_info(gpkg_path(out), layer=layer)["crs"]
    pyogrio.write_dataframe(gpd.GeoDataFrame(rows, geometry="geometry", crs=crs), gpkg_path(out), layer=layer, append=True)


# An annotator name on a row stands for QGIS stamping it when someone saved it.

def add_proposed_label(out, annotator=None) -> int:
    """A ``proposed`` connector across a gap, as a detector writes it. Returns its fid."""
    add_rows(out, "labels", [{
        "status": "proposed", "defect": "gap", "annotator": annotator, "road_class": "Street", "buffer": 4.0,
        "geometry": LineString([(370_300, 6_249_300), (370_330, 6_249_300)]),
    }])
    labels = pyogrio.read_dataframe(gpkg_path(out), layer="labels", fid_as_index=True)
    return int(labels.index[labels["status"] == "proposed"].max())


def add_flag(decision, proposal=False, flag_annotator=None, label_annotator=None):
    """A detector's gap flag with ``decision``. With ``proposal``, the flag points
    at a new ``proposed`` label row."""
    def edit(out):
        add_rows(out, "flags", [{
            "flag_key": "gap:1", "type": "gap", "detector": "gap", "raw_ids": "1",
            "proposal_fid": add_proposed_label(out, label_annotator) if proposal else None,
            "decision": decision, "annotator": flag_annotator, "geometry": Point(370_300, 6_249_300),
        }])
    return edit


CLEANING_WORK = {
    "label status": edit_layer("labels", set_value("status", "deleted", lambda g: g["raw_id"] == 1)),
    "label annotator": edit_layer("labels", set_value("annotator", "ann")),
    "label geometry": edit_layer("labels", move_first),
    "added label": edit_layer("labels", append({
        "status": "added", "road_class": "Street", "buffer": 4.0,
        "geometry": LineString([(370_300, 6_249_300), (370_400, 6_249_300)]),
    })),
    "tile reviewed": edit_layer("tiles", set_value("reviewed", True, lambda g: g["status"] == "kept")),
    "tile note": edit_layer("tiles", set_value("note", "check")),
    **{f"{decision} flag": add_flag(decision) for decision in ("real", "not_defect", "needs_qgis")},
    "flag with no decision": add_flag(None),
    "proposal with a decided flag": add_flag("real", proposal=True),
    "proposed label with no flag": add_proposed_label,
    "pending flag an annotator saved": add_flag("pending", flag_annotator="ann"),
    "pending proposal an annotator edited": add_flag("pending", proposal=True, label_annotator="ann"),
}

# Pending detector output is cheap to regenerate, so it is not cleaning work.
DETECTOR_OUTPUT = {
    "pending flag": add_flag("pending"),
    "pending proposal": add_flag("pending", proposal=True),
}


@pytest.fixture
def fresh_package(shared_synthetic_zone, baseline, tmp_path):
    out = tmp_path / "zones"
    run_package(shared_synthetic_zone, baseline, out, "--zone", TRAIN_ZONE)
    return out


def test_repackaging_an_unedited_zone_rebuilds_it(fresh_package, shared_synthetic_zone, baseline):
    before = raw_id_map(fresh_package)
    run_package(shared_synthetic_zone, baseline, fresh_package, "--zone", TRAIN_ZONE)
    assert raw_id_map(fresh_package) == before


@pytest.mark.parametrize("edit", DETECTOR_OUTPUT, ids=list(DETECTOR_OUTPUT))
def test_repackaging_a_zone_with_only_pending_detector_output_rebuilds_it(
    edit, fresh_package, shared_synthetic_zone, baseline
):
    before = raw_id_map(fresh_package)
    DETECTOR_OUTPUT[edit](fresh_package)
    assert pyogrio.read_info(gpkg_path(fresh_package), layer="flags")["features"] == 1

    run_package(shared_synthetic_zone, baseline, fresh_package, "--zone", TRAIN_ZONE)
    assert pyogrio.read_info(gpkg_path(fresh_package), layer="flags")["features"] == 0
    assert set(read_layer(fresh_package, "labels")["status"]) == {"kept"}
    assert raw_id_map(fresh_package) == before


def test_a_pending_proposal_does_not_hide_other_label_work(fresh_package, shared_synthetic_zone, baseline):
    DETECTOR_OUTPUT["pending proposal"](fresh_package)
    CLEANING_WORK["label annotator"](fresh_package)
    result = run_package(shared_synthetic_zone, baseline, fresh_package, "--zone", TRAIN_ZONE, expect_ok=False)
    assert result.exit_code != 0 and "--overwrite" in result.output


def test_a_proposed_label_whose_flag_is_decided_is_label_work(fresh_package, shared_synthetic_zone, baseline):
    CLEANING_WORK["proposal with a decided flag"](fresh_package)
    result = run_package(shared_synthetic_zone, baseline, fresh_package, "--zone", TRAIN_ZONE, expect_ok=False)
    assert "1 decided flag(s)" in result.output
    assert "1 label(s) with a status other than kept" in result.output


@pytest.mark.parametrize("edit", CLEANING_WORK, ids=list(CLEANING_WORK))
def test_repackaging_an_edited_zone_refuses_and_keeps_the_edits(edit, fresh_package, shared_synthetic_zone, baseline):
    CLEANING_WORK[edit](fresh_package)
    before = gpkg_path(fresh_package).read_bytes()

    result = run_package(shared_synthetic_zone, baseline, fresh_package, "--zone", TRAIN_ZONE, expect_ok=False)
    assert result.exit_code != 0
    assert "--overwrite" in result.output and TRAIN_ZONE in result.output
    assert gpkg_path(fresh_package).read_bytes() == before


def test_overwrite_repackages_an_edited_zone_and_backs_up_the_edits(fresh_package, shared_synthetic_zone, baseline):
    CLEANING_WORK["label status"](fresh_package)
    run_package(shared_synthetic_zone, baseline, fresh_package, "--zone", TRAIN_ZONE, "--overwrite")

    assert set(read_layer(fresh_package, "labels")["status"]) == {"kept"}
    backups = list((fresh_package / TRAIN_ZONE).glob(f"{TRAIN_ZONE}.gpkg.*.bak"))
    assert len(backups) == 1
    backup = gpd.read_file(backups[0], layer="labels")
    assert backup.loc[backup["raw_id"] == 1, "status"].tolist() == ["deleted"]


def test_an_edited_zone_does_not_stop_the_other_zones(fresh_package, shared_synthetic_zone, baseline):
    CLEANING_WORK["tile reviewed"](fresh_package)
    result = run_package(shared_synthetic_zone, baseline, fresh_package, expect_ok=False)
    assert result.exit_code != 0
    assert gpkg_path(fresh_package, VAL_ZONE).is_file() and gpkg_path(fresh_package, TEST_ZONE).is_file()


def test_zone_folder_holds_the_zones_sentinel2_rgb(packaged, shared_synthetic_zone):
    rgb_path = packaged / TRAIN_ZONE / f"{TRAIN_ZONE}_s2_rgb.tif"
    with rasterio.open(shared_synthetic_zone.zone_path) as zone, rasterio.open(rgb_path) as rgb:
        assert (rgb.count, rgb.width, rgb.height) == (3, zone.width, zone.height)
        assert rgb.crs == zone.crs and rgb.transform == zone.transform
        assert rgb.dtypes == ("uint8",) * 3 and rgb.nodata == 0
        data = rgb.read()
        assert (data > 0).all()  # no no-data in the synthetic zone
        assert all(len(np.unique(band)) > 100 for band in data)  # a stretched image, not a constant
        assert rgb.overviews(1), "overviews keep the zone fast to pan in QGIS"


def test_sentinel2_rgb_marks_nan_pixels_as_nodata(synthetic_zone, tmp_path):
    with rasterio.open(synthetic_zone.zone_path, "r+", IGNORE_COG_LAYOUT_BREAK="YES") as zone:
        zone.write(np.full((1, 3, 3), np.nan, dtype="float32"), indexes=[2], window=Window(5, 7, 3, 3))
    out = tmp_path / "zones"
    run_package(synthetic_zone, run_generate(synthetic_zone, tmp_path / "ds"), out, "--zone", TRAIN_ZONE)
    with rasterio.open(out / TRAIN_ZONE / f"{TRAIN_ZONE}_s2_rgb.tif") as rgb:
        data = rgb.read()
    assert (data[:, 7:10, 5:8] == 0).all()
    assert (data[:, 0, :] > 0).all()


# --- QGIS project ---------------------------------------------------------

NGI_AERIAL_URL = "https://aerial.openstreetmap.org.za/layer/ngi-aerial/{z}/{x}/{y}.png"
LABEL_STATUSES = ["kept", "modified", "added", "deleted", "proposed"]
DEFECTS = ["gap", "stub", "misalignment", "phantom", "missing", "temporal"]
ADDED_ROAD_BUFFERS = {"Other Road": 5.0, "Main Road": 8.0, "Street": 4.0}
# Layer tree, top to bottom.
PROJECT_LAYERS = [
    "Flags (points)", "Flags (areas)", "Labels", "Raw labels", "Tiles",
    "Sentinel-2 RGB", "NGI aerial",
]


def project_path(out, zone=TRAIN_ZONE):
    return out / zone / f"{zone}.qgs"


def test_project_layers_use_paths_relative_to_the_zone_folder(packaged):
    root = ET.parse(project_path(packaged)).getroot()
    sources = {ml.findtext("layername"): ml.findtext("datasource") for ml in root.iter("maplayer")}
    assert sorted(sources) == sorted(PROJECT_LAYERS)
    for name, source in sources.items():
        if name == "NGI aerial":
            assert f"url={NGI_AERIAL_URL}" in source.replace("&amp;", "&")
        else:
            assert source.startswith("./"), (name, source)
            assert str(packaged) not in source


def _qgis_python():
    """A Python that can import qgis.core, with its PYTHONPATH, or None."""
    candidates = [os.environ.get("QGIS_PYTHON")]
    if shutil.which("qgis"):
        candidates.append(str(Path(shutil.which("qgis")).with_name("python")))
    candidates += sorted(str(p) for p in Path.home().glob("*conda*/envs/qgis*/bin/python"))
    for python in filter(None, candidates):
        prefix = Path(python).resolve().parent.parent
        env = {**os.environ, "PYTHONPATH": str(prefix / "share" / "qgis" / "python"), "QT_QPA_PLATFORM": "offscreen"}
        try:
            ok = subprocess.run([python, "-c", "import qgis.core"], env=env, capture_output=True, timeout=60).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            return python, env
    return None


@pytest.fixture(scope="module")
def qgis_report(packaged, tmp_path_factory):
    """The project as QGIS 3 loads it, from a copy of the zone folder elsewhere."""
    found = _qgis_python()
    if found is None:
        pytest.skip("QGIS not found (set QGIS_PYTHON to a Python that can import qgis.core)")
    python, env = found
    moved = tmp_path_factory.mktemp("annotator_machine") / TRAIN_ZONE
    shutil.copytree(packaged / TRAIN_ZONE, moved)
    script = Path(__file__).with_name("qgis_inspect.py")
    result = subprocess.run(
        [python, str(script), str(moved / f"{TRAIN_ZONE}.qgs")],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    report["folder"] = str(moved)
    return report


def test_project_opens_in_qgis_with_every_layer_loading_from_the_moved_folder(qgis_report):
    assert qgis_report["read"]
    assert qgis_report["order"] == PROJECT_LAYERS
    assert qgis_report["unresolved_tree_nodes"] == []
    assert qgis_report["crs"] == "EPSG:32735"
    for name, layer in qgis_report["layers"].items():
        assert layer["valid"], name
        if name != "NGI aerial":
            assert layer["source"].startswith(qgis_report["folder"]), (name, layer["source"])
    layers = qgis_report["layers"]
    assert layers["Labels"]["feature_count"] == layers["Raw labels"]["feature_count"] == 14
    assert layers["Tiles"]["feature_count"] == 9
    assert layers["Sentinel-2 RGB"]["band_count"] == 3


def test_reference_imagery_is_an_xyz_layer_of_the_ngi_aerial(qgis_report):
    aerial = qgis_report["layers"]["NGI aerial"]
    assert aerial["provider"] == "wms"
    source = urllib.parse.unquote(aerial["source"])  # QGIS stores the URL percent-encoded
    assert "type=xyz" in source and f"url={NGI_AERIAL_URL}" in source
    assert qgis_report["visible"]["NGI aerial"]


def test_raw_labels_are_read_only_and_labels_are_editable(qgis_report):
    assert qgis_report["layers"]["Raw labels"]["read_only"]
    assert not qgis_report["layers"]["Labels"]["read_only"]
    assert not qgis_report["layers"]["Tiles"]["read_only"]


def test_labels_value_lists_restrict_status_defect_and_road_class(qgis_report):
    fields = qgis_report["layers"]["Labels"]["fields"]
    assert fields["status"]["widget"] == "ValueMap"
    assert fields["status"]["values"] == LABEL_STATUSES
    assert fields["defect"]["widget"] == "ValueMap"
    assert [v for v in fields["defect"]["values"] if v is not None] == DEFECTS
    assert None in fields["defect"]["values"]  # a label revision may have no defect
    assert fields["road_class"]["widget"] == "ValueMap"
    assert fields["road_class"]["values"] == list(ADDED_ROAD_BUFFERS)
    assert not fields["raw_id"]["editable"]


def test_choosing_an_added_roads_class_sets_its_buffer(qgis_report):
    new_road = qgis_report["layers"]["Labels"]["new_road"]
    for road_class, buffer in ADDED_ROAD_BUFFERS.items():
        assert new_road[road_class]["buffer"] == buffer, road_class
    default = new_road["default"]
    assert (default["status"], default["road_class"], default["buffer"]) == ("added", "Other Road", 5.0)
    assert default["raw_id"] is None


def test_labels_are_styled_by_status(qgis_report):
    renderer = qgis_report["layers"]["Labels"]["renderer"]
    assert renderer["type"] == "categorizedSymbol" and renderer["field"] == "status"
    assert sorted(renderer["categories"]) == sorted(LABEL_STATUSES)


def test_tile_grid_outlines_kept_hatches_no_road_and_greys_out_the_rest(qgis_report):
    tiles = qgis_report["layers"]["Tiles"]
    renderer = tiles["renderer"]
    assert renderer["type"] == "categorizedSymbol" and renderer["field"] == "status"
    cats = renderer["categories"]
    assert sorted(cats) == ["dropped_nan", "dropped_no_road", "kept", "untiled"]
    assert cats["kept"]["symbol_layers"] == ["SimpleFill"] and cats["kept"]["fill_alpha"] == [0]
    assert "LinePatternFill" in cats["dropped_no_road"]["symbol_layers"]
    for status in ("dropped_nan", "untiled"):
        assert cats[status]["symbol_layers"] == ["SimpleFill"] and cats[status]["fill_alpha"][0] > 0
    fields = tiles["fields"]
    assert fields["reviewed"]["widget"] == "CheckBox"
    assert {f for f, v in fields.items() if v["editable"]} >= {"reviewed", "note", "annotator"}
    assert not any(fields[f]["editable"] for f in ("tile_id", "row", "col", "status"))


def test_flags_load_as_point_and_area_views_with_a_decision_list(qgis_report):
    layers = qgis_report["layers"]
    assert layers["Flags (points)"]["geometry_type"] in ("Point", 0)
    assert layers["Flags (areas)"]["geometry_type"] in ("Polygon", 2)
    for name in ("Flags (points)", "Flags (areas)"):
        fields = layers[name]["fields"]
        assert fields["decision"]["values"] == ["pending", "real", "not_defect", "needs_qgis"]
        assert fields["type"]["values"] == ["gap", "stub", "duplicate", "crossing", "missing?", "phantom?"]
        assert not any(fields[f]["editable"] for f in ("flag_key", "type", "detector", "raw_ids", "proposal_fid"))


def test_splitting_a_label_keeps_its_raw_id_and_attributes_on_both_pieces(qgis_report):
    split = qgis_report["layers"]["Labels"]["split"]
    assert split["result"] == 0  # Qgis.GeometryOperationResult.Success
    pieces = split["pieces"]
    assert len(pieces) == 2
    assert pieces[0] == pieces[1]
    assert (pieces[0]["raw_id"], pieces[0]["status"]) == (1, "kept")


def test_existing_roads_cannot_be_reclassified_and_added_roads_take_an_allowed_class(qgis_report):
    assert qgis_report["layers"]["Labels"]["road_class_allowed"] == {
        "existing_unchanged": True,
        "existing_reclassified": False,
        "added_allowed_class": True,
        "added_other_class": False,
    }


@pytest.mark.parametrize("extra", [(), ("--overwrite",)], ids=["plain", "overwrite"])
def test_a_zone_open_in_another_program_is_never_replaced(extra, fresh_package, shared_synthetic_zone, baseline):
    # QGIS holds an open GeoPackage in WAL mode; changes may still sit in the -wal file.
    gpkg = gpkg_path(fresh_package)
    con = sqlite3.connect(gpkg)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("SELECT count(*) FROM labels").fetchall()
        before = gpkg.read_bytes()
        result = run_package(shared_synthetic_zone, baseline, fresh_package, "--zone", TRAIN_ZONE, *extra, expect_ok=False)
        assert result.exit_code != 0
        assert "open" in result.output and TRAIN_ZONE in result.output
        assert gpkg.read_bytes() == before
        assert not list(gpkg.parent.glob("*.bak"))
    finally:
        con.close()
