"""Detector precision from logged decisions, on hand-written flag rows in the
packaged synthetic zones. Expected values are worked out by hand."""

import geopandas as gpd
import pyogrio
import pytest
import shapely
from typer.testing import CliRunner

from sentinel2data.cleaning import precision
from sentinel2data.cleaning.contract import FLAG_FIELDS
from sentinel2data.cli import app
from tests.sentinel2data.synthetic_zone import TEST_ZONE, TRAIN_ZONE, VAL_ZONE
from tests.sentinel2data.test_detect import gpkg


def write_flags(zone, rows):
    """Append flags given as (detector, type, decision) or
    (detector, type, decision, proposal_fid); decision None writes NULL."""
    flags = []
    for i, (detector, flag_type, decision, *proposal) in enumerate(rows):
        flag = {name: None for name in FLAG_FIELDS}
        flag |= {
            "flag_key": f"{detector}:{flag_type}:@{i}",
            "type": flag_type,
            "detector": detector,
            "source": "overture" if detector == "overture" else "labels",
            "decision": decision,
            "annotator": None if decision in (None, "pending") else "Thandi",
            "proposal_fid": proposal[0] if proposal else None,
        }
        flags.append(flag)
    crs = pyogrio.read_info(zone, layer="labels")["crs"]
    points = [shapely.Point(i, i) for i in range(len(flags))]
    frame = gpd.GeoDataFrame(flags, geometry=points, crs=crs).astype(FLAG_FIELDS)
    pyogrio.write_dataframe(frame, zone, layer="flags", append=True)


def counts(real=0, not_defect=0, needs_qgis=0, pending=0):
    return precision.DecisionCounts(real, not_defect, needs_qgis, pending)


def test_a_zones_counts_and_share_per_detector_and_flag_type(zones):
    zone = gpkg(zones)
    write_flags(
        zone,
        [
            ("stub", "stub", "real"),
            ("stub", "stub", "real"),
            ("stub", "stub", "not_defect"),
            ("stub", "stub", "needs_qgis"),
            ("stub", "stub", "pending"),
            ("stub", "stub", None),
            ("overture", "phantom?", "not_defect"),
            ("overture", "missing?", "pending"),
        ],
    )

    found = precision.zone_counts(zone)

    assert found == {
        ("stub", "stub"): counts(real=2, not_defect=1, needs_qgis=1, pending=2),
        ("overture", "phantom?"): counts(not_defect=1),
        ("overture", "missing?"): counts(pending=1),
    }
    # needs_qgis and pending are outside the share: 2 real of 3 judged.
    assert found[("stub", "stub")].precision == pytest.approx(2 / 3)
    assert found[("overture", "phantom?")].precision == 0.0
    assert found[("overture", "missing?")].precision is None


def test_accepted_and_rejected_proposals_count_as_real_and_not_defect(zones):
    """KEL-56 records accept as ``real`` and reject as ``not_defect`` on the
    proposal's flag; these rows stand in for that."""
    zone = gpkg(zones)
    write_flags(
        zone,
        [
            ("gap", "gap", "real", 101),
            ("gap", "gap", "not_defect", 102),
            ("gap", "gap", "not_defect", 103),
            ("gap", "gap", "pending", 104),
        ],
    )

    assert precision.zone_counts(zone) == {
        ("gap", "gap"): counts(real=1, not_defect=2, pending=1)
    }


def test_an_unknown_decision_is_refused(zones):
    zone = gpkg(zones)
    write_flags(zone, [("stub", "stub", "maybe")])

    with pytest.raises(ValueError, match="maybe"):
        precision.zone_counts(zone)


def run_precision(zones, *args):
    result = CliRunner().invoke(app, ["precision", "--zones-dir", str(zones), *args])
    assert result.exit_code == 0, result.output + repr(result.exception)
    return result.output.splitlines()


def test_the_command_reports_each_zone_and_pools_flags_across_zones(zones):
    write_flags(gpkg(zones, TRAIN_ZONE), [("stub", "stub", "real")])
    write_flags(
        gpkg(zones, VAL_ZONE),
        [
            ("stub", "stub", "real"),
            ("stub", "stub", "not_defect"),
            ("stub", "stub", "not_defect"),
            ("stub", "stub", "needs_qgis"),
            ("gap", "gap", "pending", 7),
        ],
    )

    lines = run_precision(zones)

    assert lines[0].startswith("Precision = real / (real + not_defect)")
    assert (
        f"{TRAIN_ZONE}: stub:stub 100.0% (1 real / 1 judged), needs_qgis 0, pending 0"
        in lines
    )
    assert (
        f"{VAL_ZONE}: stub:stub 33.3% (1 real / 3 judged), needs_qgis 1, pending 0"
        in lines
    )
    assert f"{VAL_ZONE}: gap:gap no decisions, needs_qgis 0, pending 1" in lines
    assert f"{TEST_ZONE}: no flags" in lines
    # Pooled: 2 real of 4 judged, not the 66.7% mean of the two zone shares.
    assert lines[-2:] == [
        "All zones: gap:gap no decisions, needs_qgis 0, pending 1",
        "All zones: stub:stub 50.0% (2 real / 4 judged), needs_qgis 1, pending 0",
    ]


def test_the_command_reports_one_zone(zones):
    write_flags(gpkg(zones, TRAIN_ZONE), [("stub", "stub", "real")])
    write_flags(gpkg(zones, VAL_ZONE), [("stub", "stub", "not_defect")])

    lines = run_precision(zones, "--zone", VAL_ZONE)

    assert lines[1:] == [
        f"{VAL_ZONE}: stub:stub 0.0% (0 real / 1 judged), needs_qgis 0, pending 0",
        "1 selected zone(s): stub:stub 0.0% (0 real / 1 judged), needs_qgis 0, pending 0",
    ]
