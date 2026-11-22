"""Kept/dropped report: how a raw-label baseline differs from a previous dataset.

The baseline is a dataset written by ``generate``: its ``generation_report.yaml``
counts tile statuses and excluded road segments per zone, and its
``tile_status/<zone>.parquet`` tables give each zone's split. The previous
dataset only needs a ``generation_report.yaml`` in the older shape, a
``zones`` list with ``zone``, ``kept``, ``dropped_no_road`` and ``dropped_nan``.
"""
from pathlib import Path

import pandas as pd
import yaml

from sentinel2data.generator.config import SPLIT_NAMES, DatasetPaths

ZONE_COLUMNS = [
    "Kept (previous)", "Kept (baseline)", "Change",
    "No road (previous)", "No road (baseline)",
    "NaN (previous)", "NaN (baseline)",
    "Untiled",
]


def _zone_splits(paths: DatasetPaths) -> dict:
    """Zone filename -> split, from the baseline's tile-status tables."""
    splits = {}
    for path in sorted(paths.tile_status_dir.glob("*.parquet")):
        split_set = pd.read_parquet(path, columns=["split_set"])["split_set"].unique()
        if len(split_set) != 1:
            raise ValueError(f"{path}: expected one split, got {list(split_set)}")
        splits[path.stem] = str(split_set[0])
    return splits


def _excluded_columns(classes) -> list:
    return [c for cls in classes for c in (f"{cls} segments", f"{cls} km")]


def _zone_row(baseline_counts: dict, previous: dict, excluded: dict) -> dict:
    kept = baseline_counts["kept"]
    row = {
        "Kept (previous)": previous["kept"],
        "Kept (baseline)": kept,
        "Change": kept - previous["kept"],
        "No road (previous)": previous["dropped_no_road"],
        "No road (baseline)": baseline_counts["dropped_no_road"],
        "NaN (previous)": previous["dropped_nan"],
        "NaN (baseline)": baseline_counts["dropped_nan"],
        "Untiled": baseline_counts["untiled"],
    }
    for cls, v in excluded.items():
        row[f"{cls} segments"] = v["segments"]
        row[f"{cls} km"] = v["length_m"] / 1000
    return row


def _sum_rows(rows, columns) -> dict:
    total = {c: sum(row[c] for row in rows) for c in columns}
    total["Zones"] = len(rows)
    return total


def _cell(value) -> str:
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def _markdown_table(first_column: str, columns: list, rows: dict) -> list:
    lines = [
        "| " + " | ".join([first_column, *columns]) + " |",
        "|" + "|".join(["---"] + ["---:"] * len(columns)) + "|",
    ]
    for name, row in rows.items():
        lines.append("| " + " | ".join([name, *(_cell(row[c]) for c in columns)]) + " |")
    return lines


def build_change_report(dataset_dir, previous_dir) -> str:
    """The report as markdown."""
    paths = DatasetPaths(dataset_dir)
    baseline = yaml.safe_load(paths.report_path.read_text())
    previous_report = yaml.safe_load(DatasetPaths(previous_dir).report_path.read_text())
    previous = {z["zone"]: z for z in previous_report["zones"]}
    splits = _zone_splits(paths)
    moved = [z for z, s in splits.items() if previous[z]["split"] != s]
    if moved:
        raise ValueError(f"Zones in a different split than in the previous dataset: {', '.join(moved)}")
    classes = baseline["label_changes"]["excluded_road_classes"]
    excluded = baseline["label_changes"]["excluded_by_zone"]
    columns = ZONE_COLUMNS + _excluded_columns(classes)

    by_split = {}
    for split in SPLIT_NAMES:
        zones = sorted(z for z, s in splits.items() if s == split)
        by_split[split] = {
            z: _zone_row(baseline["tile_status"][z], previous[z], excluded[z]) for z in zones
        }

    split_rows = {split: _sum_rows(list(rows.values()), columns) for split, rows in by_split.items()}
    every_zone = [row for rows in by_split.values() for row in rows.values()]
    split_rows["All"] = _sum_rows(every_zone, columns)

    lines = [
        "# Kept/dropped report", "",
        f"Baseline: `{Path(dataset_dir).resolve()}`. Previous: `{Path(previous_dir).resolve()}`.", "",
        ("Tile counts per zone. *No road* and *NaN* are the two drop reasons; *Untiled* counts the "
        "partial grid positions at the right and bottom edge, which are never tiles. The last "
        "columns are the road segments (clipped to the zone) and their length removed by the "
        f"baseline's road-class exclusion ({', '.join(classes) or 'none'})."), "",
        "## By split", "", *_markdown_table("Split", ["Zones", *columns], split_rows), "",
    ]
    for split, rows in by_split.items():
        lines += [f"## {split}", "", *_markdown_table("Zone", columns, rows), ""]
    return "\n".join(lines)


def write_change_report(dataset_dir, previous_dir, out=None) -> Path:
    out = Path(out) if out else Path(dataset_dir) / "kept_dropped_report.md"
    out.write_text(build_change_report(dataset_dir, previous_dir))
    return out
