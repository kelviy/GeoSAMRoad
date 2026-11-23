"""Zone assignment: which annotator cleans which zone (KEL-5 stories 56, 57
and 59). Agreement between the shared copies (story 58) is not built yet.

Every zone gets one owner, whose copy is the one published. One test zone per
urbanisation stratum, the one nearest the stratum's median road length, is a
shared zone. Every annotator cleans their own copy of it, so agreement between
annotators can be measured.

Road length is the zone's road km inside kept tiles, summed from the baseline's
per-tile road graphs, so the assignment follows from the frozen baseline alone.
"""
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd

from sentinel2data.generator.config import DatasetPaths

STRATA = ("Urban", "PeriUrban", "Rural")
SHARED_SPLIT = "test"
COLUMNS = ["zone", "split", "urbanisation", "road_km", "annotator", "published", "shared"]
TILE_SUFFIX = re.compile(r"_r\d+_c\d+$")  # generate names tiles <zone>_r<row>_c<col>


def zone_table(dataset_dir: Path) -> pd.DataFrame:
    """One row per zone: ``zone``, ``split``, ``urbanisation``, ``road_km``."""
    meta = pd.read_parquet(DatasetPaths(dataset_dir).metadata_path)
    meta["zone"] = meta["image_path"].map(lambda p: TILE_SUFFIX.sub("", Path(p).stem))
    meta["road_km"] = [
        gpd.read_parquet(dataset_dir / p).length.sum() / 1000 for p in meta["mask_graph_path"]
    ]
    zones = meta.groupby("zone", sort=True).agg(
        split=("split_set", "first"),
        urbanisation=("urbanisation_classification", "first"),
        road_km=("road_km", "sum"),
    )
    return zones.reset_index()


def shared_zones(zones: pd.DataFrame) -> list[str]:
    """Per stratum, the test zone nearest the stratum's median road length."""
    shared = []
    for stratum in STRATA:
        in_stratum = zones.loc[zones["urbanisation"] == stratum]
        median = in_stratum["road_km"].median()
        candidates = in_stratum.loc[in_stratum["split"] == SHARED_SPLIT]
        if len(candidates) == 0:
            raise ValueError(f"no {SHARED_SPLIT} zone in stratum {stratum}")
        nearest = min(
            zip(candidates["zone"], candidates["road_km"]),
            key=lambda zone_km: (abs(zone_km[1] - median), zone_km[0]),
        )
        shared.append(nearest[0])
    return shared


def assign(zones: pd.DataFrame, annotators: list[str]) -> pd.DataFrame:
    """One row per copy an annotator cleans. Each zone has one ``published``
    copy, its owner's. A ``shared`` zone has a copy for every annotator.

    Shared zones go first, longest first, to the annotators in order. Then each
    stratum's other zones, longest first, go to whoever owns the fewest zones
    of that stratum, then the least road km of non-shared zones, then comes
    first in ``annotators``.
    """
    unknown = sorted(set(zones["urbanisation"]) - set(STRATA))
    if unknown:
        raise ValueError(f"urbanisation not in {STRATA}: {unknown}")
    km = zones.set_index("zone")["road_km"]
    shared = sorted(shared_zones(zones), key=lambda z: (-km[z], z))
    owner = {zone: annotators[i % len(annotators)] for i, zone in enumerate(shared)}
    # Everyone cleans every shared zone, so only the other zones' km differ.
    other_km = dict.fromkeys(annotators, 0.0)

    longest_first = zones.sort_values(["road_km", "zone"], ascending=[False, True])
    for stratum in STRATA:
        in_stratum = list(longest_first.loc[longest_first["urbanisation"] == stratum, "zone"])
        count = {a: sum(owner.get(z) == a for z in in_stratum) for a in annotators}
        for zone in in_stratum:
            if zone in owner:
                continue
            pick = min(annotators, key=lambda a: (count[a], other_km[a], annotators.index(a)))
            owner[zone] = pick
            count[pick] += 1
            other_km[pick] += km[zone]

    rows = []
    for zone in zones.sort_values(["urbanisation", "zone"])["zone"]:
        cleaners = annotators if zone in shared else [owner[zone]]
        for name in cleaners:
            rows.append({
                "zone": zone, "annotator": name,
                "published": name == owner[zone], "shared": zone in shared,
            })
    return pd.DataFrame(rows).merge(zones, on="zone").reindex(columns=COLUMNS)


def workload(table: pd.DataFrame) -> pd.DataFrame:
    """Per annotator: zones owned per stratum, then the copies and road km they
    clean in all, shared zones included."""
    owned = table.loc[table["published"]]
    counts = pd.crosstab(owned["annotator"], owned["urbanisation"]).reindex(
        columns=list(STRATA), fill_value=0
    )
    by_annotator = table.groupby("annotator")
    counts["copies"] = by_annotator.size()
    counts["road_km"] = by_annotator["road_km"].sum()
    return counts
