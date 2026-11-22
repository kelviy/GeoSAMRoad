"""``change-report``: how the raw-label baseline differs from a previous dataset.

The baseline is ``generate`` run on the synthetic zone. The previous dataset is
a hand-written ``generation_report.yaml`` in the shape the older generator
wrote (a ``zones`` list), with the counts a legacy run of the synthetic zone
gives: the Track/Slipway-only tile was kept then.
"""
import pytest
import yaml
from typer.testing import CliRunner

from sentinel2data.cli import app
from tests.sentinel2data.synthetic_zone import TEST_ZONE, TRAIN_ZONE, VAL_ZONE
from tests.sentinel2data.test_generate import run_generate

PREVIOUS_ZONES = [
    {"zone": TRAIN_ZONE, "split": "train", "kept": 3, "dropped_no_road": 1, "dropped_nan": 0},
    {"zone": VAL_ZONE, "split": "val", "kept": 1, "dropped_no_road": 0, "dropped_nan": 0},
    {"zone": TEST_ZONE, "split": "test", "kept": 1, "dropped_no_road": 0, "dropped_nan": 0},
]


@pytest.fixture(scope="module")
def baseline(shared_synthetic_zone, tmp_path_factory):
    return run_generate(shared_synthetic_zone, tmp_path_factory.mktemp("baseline"))


@pytest.fixture(scope="module")
def previous(tmp_path_factory):
    root = tmp_path_factory.mktemp("previous")
    (root / "generation_report.yaml").write_text(yaml.safe_dump({"zones": PREVIOUS_ZONES}))
    return root


@pytest.fixture(scope="module")
def report(baseline, previous):
    result = CliRunner().invoke(
        app, ["change-report", "--dataset-dir", str(baseline), "--previous-dir", str(previous)],
    )
    assert result.exit_code == 0, result.output + repr(result.exception)
    return (baseline / "kept_dropped_report.md").read_text()


def table(markdown, heading):
    """Rows of the first table under ``heading``, keyed by their first cell."""
    lines = markdown.splitlines()
    start = lines.index(heading)
    rows = []
    for line in lines[start + 1:]:
        if line.startswith("#"):
            break
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    header, _separator, *body = rows
    return {row[0]: dict(zip(header, row)) for row in body}


def test_zone_row_compares_kept_tiles_and_counts_drops_by_reason(report):
    row = table(report, "## train")[TRAIN_ZONE]
    assert row["Kept (previous)"] == "3"
    assert row["Kept (baseline)"] == "2"
    assert row["Change"] == "-1"
    assert row["No road (previous)"] == "1"
    assert row["No road (baseline)"] == "2"
    assert row["NaN (previous)"] == "0"
    assert row["NaN (baseline)"] == "0"
    assert row["Untiled"] == "5"


def test_zone_row_counts_excluded_segments_and_length_per_class(report):
    # Track runs 800 m; Slipway 80 px east and 10 px south, about 806 m.
    train = table(report, "## train")[TRAIN_ZONE]
    assert train["Track segments"] == "1"
    assert train["Track km"] == "0.80"
    assert train["Slipway segments"] == "1"
    assert train["Slipway km"] == "0.81"
    val = table(report, "## val")[VAL_ZONE]
    assert (val["Track segments"], val["Track km"]) == ("0", "0.00")


def test_split_table_sums_zones_per_split_and_overall(report):
    splits = table(report, "## By split")
    assert splits["train"]["Zones"] == "1"
    assert splits["train"]["Kept (baseline)"] == "2"
    assert splits["test"]["Kept (baseline)"] == "1"
    total = splits["All"]
    assert total["Zones"] == "3"
    assert (total["Kept (previous)"], total["Kept (baseline)"], total["Change"]) == ("5", "4", "-1")
    assert (total["No road (previous)"], total["No road (baseline)"]) == ("1", "2")
    assert (total["Slipway segments"], total["Slipway km"]) == ("1", "0.81")
