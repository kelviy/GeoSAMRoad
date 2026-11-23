"""``assign-zones``: zones -> annotators, plus the three shared test zones.

The baseline is a hand-made ``metadata.parquet`` with one road graph per tile.
Each graph is a single straight road, so a zone's road length is known
exactly. ``U3`` has two tiles of 25 km to check that tiles are summed.
"""
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString
from typer.testing import CliRunner

from sentinel2data.cli import app

ANNOTATORS = ["alice", "bob", "carol"]

# zone -> (split, urbanisation, road km per tile)
ZONES = {
    "U1_-26p01_28p01_Urban": ("test", "Urban", [10]),
    "U2_-26p02_28p02_Urban": ("test", "Urban", [30]),
    "U3_-26p03_28p03_Urban": ("train", "Urban", [25, 25]),
    "U4_-26p04_28p04_Urban": ("train", "Urban", [20]),
    "P1_-30p01_20p01_PeriUrban": ("test", "PeriUrban", [4]),
    "P2_-30p02_20p02_PeriUrban": ("val", "PeriUrban", [6]),
    "P3_-30p03_20p03_PeriUrban": ("train", "PeriUrban", [8]),
    "R1_-31p01_21p01_Rural": ("test", "Rural", [1]),
    "R2_-31p02_21p02_Rural": ("test", "Rural", [3]),
    "R3_-31p03_21p03_Rural": ("train", "Rural", [2.5]),
    "R4_-31p04_21p04_Rural": ("train", "Rural", [9]),
}


def write_baseline(root, zones):
    rows = []
    for zone, (split, urban, kms) in zones.items():
        for col, km in enumerate(kms):
            tile = f"{zone}_r0_c{col}"
            graph = f"{split}/masks_graph/{tile}.parquet"
            (root / graph).parent.mkdir(parents=True, exist_ok=True)
            road = LineString([(0, 0), (km * 1000, 0)])
            gpd.GeoDataFrame(geometry=[road], crs="EPSG:32735").to_parquet(root / graph)
            rows.append({
                "image_path": f"{split}/imagery/{tile}.tif",
                "mask_graph_path": graph,
                "split_set": split,
                "urbanisation_classification": urban,
            })
    pd.DataFrame(rows).to_parquet(root / "metadata.parquet")
    return root


def run_assign(baseline, out, annotators=ANNOTATORS):
    args = ["assign-zones", "--dataset-dir", str(baseline), "--out", str(out)]
    for name in annotators:
        args += ["--annotator", name]
    return CliRunner().invoke(app, args)


@pytest.fixture
def baseline(tmp_path):
    return write_baseline(tmp_path / "baseline", ZONES)


@pytest.fixture
def assignment(baseline, tmp_path):
    out = tmp_path / "assignment.csv"
    result = run_assign(baseline, out)
    assert result.exit_code == 0, result.output + repr(result.exception)
    return pd.read_csv(out)


def shared_zones(assignment):
    return sorted(assignment.loc[assignment["shared"], "zone"].unique())


def owners(assignment):
    rows = assignment[assignment["published"]]
    return dict(zip(rows["zone"], rows["annotator"]))


def test_every_zone_has_exactly_one_published_copy(assignment):
    rows = assignment[assignment["published"]]
    assert sorted(rows["zone"]) == sorted(ZONES)


def test_shared_zones_are_the_test_zones_nearest_their_stratum_median(assignment):
    # Stratum medians over all zones: Urban 25 km, PeriUrban 6 km, Rural 2.75 km.
    assert shared_zones(assignment) == [
        "P1_-30p01_20p01_PeriUrban", "R2_-31p02_21p02_Rural", "U2_-26p02_28p02_Urban",
    ]


def test_each_shared_zone_is_cleaned_once_by_every_annotator(assignment):
    shared = assignment[assignment["shared"]]
    for rows in (g for _, g in shared.groupby("zone")):
        assert sorted(rows["annotator"]) == ANNOTATORS
        assert rows["published"].sum() == 1


def test_only_shared_zones_have_more_than_one_copy(assignment):
    copies = assignment.groupby("zone").size()
    assert sorted(copies[copies > 1].index) == shared_zones(assignment)


def test_each_annotator_owns_one_shared_zone(assignment):
    shared = set(shared_zones(assignment))
    shared_owners = [a for z, a in owners(assignment).items() if z in shared]
    assert sorted(shared_owners) == ANNOTATORS


def test_road_length_sums_the_zone_tiles(assignment):
    rows = assignment.drop_duplicates("zone").set_index("zone")
    assert rows.loc["U3_-26p03_28p03_Urban", "road_km"] == pytest.approx(50)


def test_owners_follow_the_worked_example(assignment):
    # Shared zones first, longest first, to annotators in order: U2 alice,
    # P1 bob, R2 carol. Then each stratum's other zones, longest first, go to
    # whoever has the fewest zones of that stratum, then the least road km of
    # non-shared zones (everyone cleans every shared zone).
    assert owners(assignment) == {
        "U2_-26p02_28p02_Urban": "alice",
        "P1_-30p01_20p01_PeriUrban": "bob",
        "R2_-31p02_21p02_Rural": "carol",
        "U3_-26p03_28p03_Urban": "bob",
        "U4_-26p04_28p04_Urban": "carol",
        "U1_-26p01_28p01_Urban": "alice",
        "P3_-30p03_20p03_PeriUrban": "alice",
        "P2_-30p02_20p02_PeriUrban": "carol",
        "R4_-31p04_21p04_Rural": "alice",
        "R3_-31p03_21p03_Rural": "bob",
        "R1_-31p01_21p01_Rural": "carol",
    }


def test_zones_per_stratum_differ_by_at_most_one_between_annotators(assignment):
    rows = assignment[assignment["published"]]
    counts = rows.groupby(["urbanisation", "annotator"]).size().unstack(fill_value=0)
    counts = counts.reindex(columns=ANNOTATORS, fill_value=0)
    assert ((counts.max(axis=1) - counts.min(axis=1)) <= 1).all()


def test_rerun_writes_the_same_file(baseline, tmp_path):
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    assert run_assign(baseline, first).exit_code == 0
    assert run_assign(baseline, second).exit_code == 0
    assert first.read_bytes() == second.read_bytes()


def test_refuses_a_stratum_with_no_test_zone(tmp_path):
    zones = {z: v for z, v in ZONES.items() if not (v[1] == "Rural" and v[0] == "test")}
    result = run_assign(write_baseline(tmp_path / "baseline", zones), tmp_path / "out.csv")
    assert result.exit_code != 0
    assert "Rural" in result.output


def test_refuses_an_unknown_urbanisation_class(tmp_path):
    zones = {**ZONES, "X1_-31p05_21p05_Suburban": ("train", "Suburban", [5])}
    result = run_assign(write_baseline(tmp_path / "baseline", zones), tmp_path / "out.csv")
    assert result.exit_code != 0
    assert "Suburban" in result.output
