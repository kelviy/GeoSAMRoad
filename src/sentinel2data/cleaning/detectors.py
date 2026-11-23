"""Detectors: model-agnostic checks that flag suspected defects in a zone.

Each detector takes a ``Zone`` (read from the zone GeoPackage only) and the
``DetectorConfig``, and returns the flags it places, one row per suspected
defect with ``flag_key``, ``type``, ``source``, ``raw_ids`` and a location
geometry. The runner in ``detect`` adds the rest of the flag fields. Queries
are vectorised over an STRtree: per-road loops don't finish on dense urban
zones.
"""

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import get_type_hints

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
import yaml

DEFAULT_CONFIG_PATH = Path(__file__).parents[1] / "configs" / "detectors.yaml"


@dataclass(frozen=True)
class StubConfig:
    enabled: bool
    max_length_m: float


@dataclass(frozen=True)
class DetectorConfig:
    """Every detector threshold and toggle (``configs/detectors.yaml``)."""

    connect_tolerance_m: float
    zone_edge_tolerance_m: float
    stub: StubConfig

    @classmethod
    def load(cls, path) -> "DetectorConfig":
        return _parse(cls, yaml.safe_load(Path(path).read_text()), str(path))

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _parse(cls, raw, where):
    """Build dataclass ``cls`` from ``raw``, which must hold exactly its fields."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected a mapping, got {raw!r}")  # noqa: TRY004 - a config error, like the rest
    hints = get_type_hints(cls)
    missing, unknown = set(hints) - set(raw), set(raw) - set(hints)
    if missing or unknown:
        raise ValueError(
            f"{where}: missing keys {sorted(missing)}, unknown keys {sorted(unknown)}"
        )
    values = {}
    for name, kind in hints.items():
        value, key = raw[name], f"{where}: {name}"
        if dataclasses.is_dataclass(kind):
            values[name] = _parse(kind, value, key)
        elif kind is bool and isinstance(value, bool):
            values[name] = value
        elif (
            kind is float
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            values[name] = float(value)
        else:
            raise ValueError(f"{key}: expected {kind.__name__}, got {value!r}")
    return cls(**values)


@dataclass(frozen=True)
class Zone:
    """What detectors see of a zone GeoPackage.

    ``roads`` holds the label rows that are roads now, one 2D LineString each,
    indexed by labels fid. ``kept_area`` is the union of the kept tiles, and
    ``boundary`` the zone outline.
    """

    roads: gpd.GeoDataFrame
    kept_area: shapely.Geometry
    boundary: shapely.Geometry

    def in_kept_tiles(self, geoms) -> np.ndarray:
        """Per geometry, whether it lies inside or touches a kept tile."""
        return shapely.intersects(geoms, self.kept_area)

    def on_boundary(self, points, tolerance) -> np.ndarray:
        """Per point, whether it lies within ``tolerance`` of the zone outline."""
        return shapely.distance(points, self.boundary) <= tolerance


def branches(roads):
    """Roads joined into branches wherever exactly two road ends meet.

    Returns the branch lines and, per branch, the positions in ``roads`` of the
    rows it is made of. A road stored as several rows is one branch.
    """
    geoms = np.asarray(roads)
    merged = shapely.line_merge(shapely.multilinestrings(geoms))
    lines = np.asarray(shapely.get_parts(merged))
    branch_idx, road_idx = shapely.STRtree(geoms).query(lines, predicate="covers")
    members = [road_idx[branch_idx == b] for b in range(len(lines))]
    return lines, members


def dead_ends(lines, tolerance):
    """Per line, whether its start and its end touch no other line.

    A line's own geometry doesn't count, so an end touching only its own line
    (a loop) is dead too.
    """
    geoms = np.asarray(lines)
    n = len(geoms)
    ends = np.concatenate([shapely.get_point(geoms, 0), shapely.get_point(geoms, -1)])
    owner = np.tile(np.arange(n), 2)
    end_idx, line_idx = shapely.STRtree(geoms).query(
        ends, predicate="dwithin", distance=tolerance
    )
    touched = np.zeros(2 * n, dtype=bool)
    touched[end_idx[line_idx != owner[end_idx]]] = True
    return ~touched[:n], ~touched[n:]


def stub_flags(zone: Zone, config: DetectorConfig) -> gpd.GeoDataFrame:
    """A ``stub`` flag at the dead end of each branch shorter than ``stub.max_length_m``.

    A branch with an end on the zone boundary is skipped: the clip cut it, so
    its real length is unknown. A dead end outside the kept tiles gets no flag.
    """
    lines, members = branches(zone.roads.geometry.values)
    starts, ends = shapely.get_point(lines, 0), shapely.get_point(lines, -1)
    dead_start, dead_end = dead_ends(lines, config.connect_tolerance_m)
    dead_points = np.where(dead_start, starts, ends)
    tol = config.zone_edge_tolerance_m
    edge_cut = zone.on_boundary(starts, tol) | zone.on_boundary(ends, tol)
    stub = (
        (dead_start | dead_end)
        & ~edge_cut
        & (shapely.length(lines) < config.stub.max_length_m)
        & zone.in_kept_tiles(dead_points)
    )
    raw_ids = zone.roads["raw_id"].to_numpy()
    flag_raw_ids = [_join_raw_ids(raw_ids[members[b]]) for b in np.flatnonzero(stub)]
    points = dead_points[stub]
    return gpd.GeoDataFrame(
        {
            "flag_key": [
                flag_key("stub", "stub", r, p) for r, p in zip(flag_raw_ids, points)
            ],
            "type": "stub",
            "source": "labels",
            "raw_ids": pd.array(flag_raw_ids, dtype="string"),
        },
        geometry=gpd.GeoSeries(points, crs=zone.roads.crs),
    )


def _join_raw_ids(raw_ids):
    """``raw_ids`` as the flag field: sorted, comma-separated, empty rows skipped."""
    ids = sorted({int(r) for r in raw_ids if not pd.isna(r)})
    return ",".join(map(str, ids)) or None


def flag_key(detector, flag_type, raw_ids, point) -> str:
    """The key a re-run matches flags by: ``<detector>:<type>:<raw_ids>@<x>,<y>``,
    with the location rounded to the metre.

    Two runs on the same zone with the same roads give the same key. A flag
    found again can differ from the stored one only in position, within the
    same 1 m cell, so the runner leaves the stored one as it is.
    """
    return f"{detector}:{flag_type}:{raw_ids or ''}@{point.x:.0f},{point.y:.0f}"


Detector = Callable[[Zone, DetectorConfig], gpd.GeoDataFrame]

# Detector name -> detector. The name is also its config section and the
# ``detector`` field of its flags.
DETECTORS: dict[str, Detector] = {"stub": stub_flags}
