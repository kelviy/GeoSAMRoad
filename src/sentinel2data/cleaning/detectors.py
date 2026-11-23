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
class DuplicateConfig:
    enabled: bool
    max_distance_m: float
    min_overlap_share: float
    min_length_m: float


@dataclass(frozen=True)
class CrossingConfig:
    enabled: bool


@dataclass(frozen=True)
class DetectorConfig:
    """Every detector threshold and toggle (``configs/detectors.yaml``)."""

    connect_tolerance_m: float
    zone_edge_tolerance_m: float
    stub: StubConfig
    duplicate: DuplicateConfig
    crossing: CrossingConfig

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
    return _flags_from_labels(
        "stub",
        dead_points[stub],
        [raw_ids[members[b]] for b in np.flatnonzero(stub)],
        zone.roads.crs,
    )


def duplicate_flags(zone: Zone, config: DetectorConfig) -> gpd.GeoDataFrame:
    """A ``duplicate`` flag for each row lying within ``duplicate.max_distance_m``
    of one other row over at least ``duplicate.min_overlap_share`` of its length.

    Flag only: parallel carriageways look the same, so nothing is proposed. The
    flag is a point at the middle of the covered row, which must lie in or
    touch a kept tile, and names both rows. A pair where each row covers the
    other gets one flag, on the shorter row that qualifies. Rows shorter than
    ``duplicate.min_length_m`` are never the covered row.
    """
    cfg = config.duplicate
    geoms = np.asarray(zone.roads.geometry.values)
    lengths = shapely.length(geoms)
    buffers = shapely.buffer(geoms, cfg.max_distance_m)
    other, row = shapely.STRtree(geoms).query(buffers, predicate="intersects")
    keep = (other != row) & (lengths[row] >= cfg.min_length_m)
    other, row = other[keep], row[keep]
    covered = shapely.length(shapely.intersection(geoms[row], buffers[other]))
    is_dup = covered >= cfg.min_overlap_share * lengths[row]
    row, other = row[is_dup], other[is_dup]
    points = shapely.line_interpolate_point(geoms[row], 0.5, normalized=True)
    scoped = zone.in_kept_tiles(points)
    row, other, points = row[scoped], other[scoped], points[scoped]
    # One flag per pair, on its shorter row.
    pairs = np.sort(np.stack([row, other], axis=1), axis=1)
    by_length = np.lexsort((row, lengths[row]))
    _, first = np.unique(pairs[by_length], axis=0, return_index=True)
    pick = by_length[first]
    raw_ids = zone.roads["raw_id"].to_numpy()
    return _flags_from_labels(
        "duplicate",
        points[pick],
        [raw_ids[pair] for pair in pairs[pick]],
        zone.roads.crs,
    )


def crossing_flags(zone: Zone, config: DetectorConfig) -> gpd.GeoDataFrame:
    """A ``crossing`` flag at each point where two roads cross and the point is
    not a vertex of both.

    Flag only: a bridge or underpass crosses without a junction too. A point
    where one road ends on the other is a touch, not a crossing.
    """
    del config  # no thresholds
    geoms = np.asarray(zone.roads.geometry.values)
    a, b = shapely.STRtree(geoms).query(geoms, predicate="crosses")
    a, b = a[a < b], b[a < b]
    points, pair = shapely.get_parts(
        shapely.intersection(geoms[a], geoms[b]), return_index=True
    )
    a, b = a[pair], b[pair]
    vertices = np.asarray(
        shapely.multipoints(
            shapely.get_coordinates(geoms),
            indices=np.repeat(
                np.arange(len(geoms)), shapely.get_num_coordinates(geoms)
            ),
        )
    )
    noded = shapely.intersects(points, vertices[a]) & shapely.intersects(
        points, vertices[b]
    )
    at_an_end = shapely.intersects(
        points, shapely.boundary(geoms[a])
    ) | shapely.intersects(points, shapely.boundary(geoms[b]))
    flag = ~noded & ~at_an_end & zone.in_kept_tiles(points)
    raw_ids = zone.roads["raw_id"].to_numpy()
    return _flags_from_labels(
        "crossing",
        points[flag],
        [raw_ids[[i, j]] for i, j in zip(a[flag], b[flag])],
        zone.roads.crs,
    )


def _flags_from_labels(flag_type, points, raw_id_groups, crs) -> gpd.GeoDataFrame:
    """Flags of ``flag_type`` from the labels, one per point. The detector has
    the same name as the flag type."""
    flag_raw_ids = [_join_raw_ids(ids) for ids in raw_id_groups]
    return gpd.GeoDataFrame(
        {
            "flag_key": [
                flag_key(flag_type, flag_type, r, p)
                for r, p in zip(flag_raw_ids, points)
            ],
            "type": flag_type,
            "source": "labels",
            "raw_ids": pd.array(flag_raw_ids, dtype="string"),
        },
        geometry=gpd.GeoSeries(points, crs=crs),
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
DETECTORS: dict[str, Detector] = {
    "stub": stub_flags,
    "duplicate": duplicate_flags,
    "crossing": crossing_flags,
}
