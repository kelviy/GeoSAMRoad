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
from typing import get_args, get_origin, get_type_hints

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
import yaml
from scipy.sparse import coo_array
from scipy.sparse.csgraph import connected_components

from sentinel2data.generator.config import WGS84

DEFAULT_CONFIG_PATH = Path(__file__).parents[1] / "configs" / "detectors.yaml"


@dataclass(frozen=True)
class GapConfig:
    enabled: bool
    # Its flags are left out of the review queue (still written).
    hidden: bool
    max_distance_m: float
    snap_to_end_m: float


@dataclass(frozen=True)
class StubConfig:
    enabled: bool
    hidden: bool
    max_length_m: float


@dataclass(frozen=True)
class OvertureConfig:
    enabled: bool
    hidden: bool
    path: str
    match_tolerance_m: float
    sample_spacing_m: float
    missing_min_length_m: float
    phantom_min_length_m: float
    # Overture road_class -> whether its roads produce ``missing?`` flags.
    missing_classes: dict[str, bool]

    @property
    def roads_path(self) -> Path:
        return Path(self.path).expanduser()


@dataclass(frozen=True)
class DuplicateConfig:
    enabled: bool
    hidden: bool
    max_distance_m: float
    min_overlap_share: float
    min_length_m: float


@dataclass(frozen=True)
class CrossingConfig:
    enabled: bool
    hidden: bool


@dataclass(frozen=True)
class DetectorConfig:
    """Every detector threshold and toggle (``configs/detectors.yaml``)."""

    connect_tolerance_m: float
    zone_edge_tolerance_m: float
    gap: GapConfig
    stub: StubConfig
    duplicate: DuplicateConfig
    crossing: CrossingConfig
    overture: OvertureConfig

    @classmethod
    def load(cls, path) -> "DetectorConfig":
        config = _parse(cls, yaml.safe_load(Path(path).read_text()), str(path))
        overture = config.overture
        if overture.enabled and not overture.roads_path.is_file():
            raise ValueError(f"{path}: overture.path: no file at {overture.roads_path}")
        return config

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
        elif kind in (bool, str) and type(value) is kind:
            values[name] = value
        elif get_origin(kind) is dict:
            values[name] = _parse_mapping(get_args(kind), value, key)
        elif (
            kind is float
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            values[name] = float(value)
        else:
            raise ValueError(f"{key}: expected {kind.__name__}, got {value!r}")
    return cls(**values)


def _parse_mapping(kinds, raw, where):
    key_kind, value_kind = kinds
    if not isinstance(raw, dict) or not all(
        isinstance(k, key_kind) and isinstance(v, value_kind) for k, v in raw.items()
    ):
        raise ValueError(
            f"{where}: expected a mapping of {key_kind.__name__} to "
            f"{value_kind.__name__}, got {raw!r}"
        )
    return dict(raw)


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


def gap_flags(zone: Zone, config: DetectorConfig) -> gpd.GeoDataFrame:
    """A ``gap`` flag at each dangling road end within ``gap.max_distance_m`` of
    another road, proposing a connector to that road.

    A dangling end touches no other row. Ends on the zone boundary and ends
    outside the kept tiles are skipped. The end's own branch and every branch
    touching it don't count as another road, so a stub never gets a connector
    back onto the road it hangs off. The connector runs from the end to the
    nearest point of the nearest road row, or to that row's end when the end
    lies within ``gap.snap_to_end_m`` of the nearest point. Two dangling ends
    within the distance of each other whose connectors run to each other's
    roads close one gap, and only one of them gets a flag.

    The flags carry their connector in a ``proposal`` column.
    """
    cfg = config.gap
    geoms = np.asarray(zone.roads.geometry.values)
    n = len(geoms)
    tree = shapely.STRtree(geoms)
    branch_of = _branch_of_rows(geoms)
    ra, rb = tree.query(geoms, predicate="dwithin", distance=config.connect_tolerance_m)
    m = n + 1
    touching = np.unique(branch_of[ra] * m + branch_of[rb])

    starts, ends = shapely.get_point(geoms, 0), shapely.get_point(geoms, -1)
    points = np.concatenate([starts, ends])
    owner = np.tile(np.arange(n), 2)
    dead = np.concatenate(dead_ends(geoms, config.connect_tolerance_m))
    dangling = np.flatnonzero(
        dead
        & ~zone.on_boundary(points, config.zone_edge_tolerance_m)
        & zone.in_kept_tiles(points)
    )

    near, target = tree.query(
        points[dangling], predicate="dwithin", distance=cfg.max_distance_m
    )
    end = dangling[near]
    other_road = ~np.isin(branch_of[owner[end]] * m + branch_of[target], touching)
    end, target = end[other_road], target[other_road]
    # The nearest row per end; ties go to the row stored first.
    distance = shapely.distance(points[end], geoms[target])
    order = np.lexsort((target, distance, end))
    _, first = np.unique(end[order], return_index=True)
    end, target = end[order[first]], target[order[first]]

    origin = points[end]
    nearest = shapely.get_point(shapely.shortest_line(origin, geoms[target]), -1)
    to_start = shapely.distance(nearest, starts[target])
    to_end = shapely.distance(nearest, ends[target])
    snap_end = np.where(to_start <= to_end, target, target + n)
    snapped = np.minimum(to_start, to_end) <= cfg.snap_to_end_m
    tip = np.where(snapped, points[snap_end], nearest)

    keep = _one_flag_per_gap(
        branch_of[owner[end]], branch_of[target], origin, snapped, cfg.max_distance_m
    )
    end, target, origin, tip = end[keep], target[keep], origin[keep], tip[keep]
    connectors = [
        shapely.LineString([a.coords[0], b.coords[0]]) for a, b in zip(origin, tip)
    ]
    raw_ids = zone.roads["raw_id"].to_numpy()
    flags = _flags_from_labels(
        "gap",
        origin,
        [raw_ids[[owner[e], t]] for e, t in zip(end, target)],
        zone.roads.crs,
    )
    flags["proposal"] = pd.Series(connectors, index=flags.index, dtype=object)
    return flags


def _branch_of_rows(geoms):
    """Per row, the branch it belongs to. A row no branch covers is a branch of
    its own."""
    _, members = branches(geoms)
    branch_of = np.full(len(geoms), -1)
    for b, rows in enumerate(members):
        branch_of[rows] = b
    alone = branch_of == -1
    branch_of[alone] = len(members) + np.arange(alone.sum())
    return branch_of


def _one_flag_per_gap(road, target_road, origin, snapped, max_distance):
    """Per candidate connector, whether its dangling end gets a flag.

    Candidate ``i`` runs from ``origin[i]`` on branch ``road[i]`` to branch
    ``target_road[i]``. Two candidates close one gap when each runs to the
    other's branch and their dangling ends are within ``max_distance`` of each
    other. Of such a pair, a connector that ends on a row end wins, then the
    one starting at the lower x, then y.
    """
    by_roads: dict[tuple[int, int], list[int]] = {}
    for i, pair in enumerate(zip(road, target_road)):
        by_roads.setdefault((int(pair[0]), int(pair[1])), []).append(i)
    rank = [(not snapped[i], *origin[i].coords[0]) for i in range(len(origin))]
    keep = np.ones(len(origin), dtype=bool)
    for i in range(len(origin)):
        facing = by_roads.get((int(target_road[i]), int(road[i])), [])
        for j in facing:
            close = origin[i].distance(origin[j]) <= max_distance
            if close and rank[j] < rank[i]:
                keep[i] = False
    return keep


def _flags_from_labels(flag_type, points, raw_id_groups, crs) -> gpd.GeoDataFrame:
    """Flags of ``flag_type`` from the labels, one per point. The detector has
    the same name as the flag type."""
    return _flag_rows(flag_type, flag_type, "labels", points, raw_id_groups, crs)


def _flag_rows(
    detector, flag_type, source, geometries, raw_id_groups, crs
) -> gpd.GeoDataFrame:
    """One flag per geometry, keyed at its point or, for an area, its centroid."""
    flag_raw_ids = [_join_raw_ids(ids) for ids in raw_id_groups]
    anchors = [shapely.centroid(g) for g in geometries]
    return gpd.GeoDataFrame(
        {
            "flag_key": [
                flag_key(detector, flag_type, r, p)
                for r, p in zip(flag_raw_ids, anchors)
            ],
            "type": flag_type,
            "source": source,
            "raw_ids": pd.array(flag_raw_ids, dtype="string"),
        },
        geometry=gpd.GeoSeries(list(geometries), crs=crs),
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


def overture_flags(zone: Zone, config: DetectorConfig) -> gpd.GeoDataFrame:
    """``missing?`` and ``phantom?`` flags where the labels and Overture disagree.

    Both line sets are sampled every ``sample_spacing_m`` at most. A sample is
    matched when the other set has a line within ``match_tolerance_m``.
    Unmatched samples inside the kept tiles are grouped into stretches, and a
    stretch at least its type's minimum length long gets one flag:
    ``missing?`` for Overture roads of a class switched on in
    ``missing_classes``, ``phantom?`` for labels, matched against Overture
    roads of every class.

    A flag is an area: the stretch's bounding box grown by the tolerance and
    rounded outwards to the metre. It never holds Overture geometry, because
    Overture is ODbL and copying it would make ROSA a derivative database.
    """
    settings = config.overture
    tolerance = settings.match_tolerance_m
    overture = _read_overture(settings.roads_path, zone, tolerance)
    labels = zone.roads.geometry.to_numpy()
    overture_lines = overture.geometry.to_numpy()
    switched_on = [c for c, on in settings.missing_classes.items() if on]
    missing_lines = overture_lines[
        np.isin(np.asarray(overture["road_class"]), switched_on)
    ]
    # Per line, the raw_id a flag on it names. Overture lines name none.
    label_raw_ids = zone.roads["raw_id"].to_numpy()
    no_raw_ids = np.full(len(missing_lines), None)

    found = []
    for flag_type, lines, line_raw_ids, others, min_length in (
        ("missing?", missing_lines, no_raw_ids, labels, settings.missing_min_length_m),
        (
            "phantom?",
            labels,
            label_raw_ids,
            overture_lines,
            settings.phantom_min_length_m,
        ),
    ):
        stretches = list(
            _unmatched_stretches(
                zone, lines, others, settings.sample_spacing_m, tolerance, min_length
            )
        )
        found.append(
            _flag_rows(
                "overture",
                flag_type,
                "overture",
                [_flag_area(samples, tolerance) for samples, _ in stretches],
                [line_raw_ids[rows] for _, rows in stretches],
                zone.roads.crs,
            )
        )
    return gpd.GeoDataFrame(pd.concat(found, ignore_index=True), crs=zone.roads.crs)


def _read_overture(path, zone: Zone, margin):
    """The Overture roads around the zone, in the zone CRS, one 2D LineString each.

    The layer is a WGS84 GeoParquet with a bbox covering column, like every
    road layer the ``roads`` command writes.
    """
    crs = zone.roads.crs
    assert crs is not None, "zone labels always have a CRS"
    around = gpd.GeoSeries([shapely.envelope(zone.boundary).buffer(margin)], crs=crs)
    bbox = tuple(around.to_crs(WGS84).total_bounds)
    roads = gpd.read_parquet(path, bbox=bbox).to_crs(crs).explode(index_parts=False)
    lines = shapely.force_2d(roads.geometry.to_numpy())
    return roads.set_geometry(lines, crs=crs)[~shapely.is_empty(lines)]


def _unmatched_stretches(zone, lines, others, spacing, tolerance, min_length):
    """Per stretch of ``lines`` with no line of ``others`` within the tolerance,
    its sample points and the positions in ``lines`` they lie on.

    A sample stands for the line around it, half the way to each neighbour, so
    a stretch's length is its samples' share summed. Unmatched samples within
    1.5 spacings of each other are one stretch, across line ends and junctions.
    """
    owner, points, share = _sample(lines, spacing)
    near, _ = shapely.STRtree(others).query(
        points, predicate="dwithin", distance=tolerance
    )
    unmatched = np.ones(len(points), dtype=bool)
    unmatched[near] = False
    unmatched &= zone.in_kept_tiles(points)
    points, owner, share = points[unmatched], owner[unmatched], share[unmatched]

    a, b = shapely.STRtree(points).query(
        points, predicate="dwithin", distance=1.5 * spacing
    )
    n = len(points)
    graph = coo_array((np.ones(len(a)), (a, b)), shape=(n, n))
    _, stretch = connected_components(graph, directed=False)
    length = np.bincount(stretch, weights=share, minlength=n)
    for s in np.flatnonzero(length >= min_length):
        members = stretch == s
        yield points[members], owner[members]


def _sample(lines, spacing):
    """Points along each line, evenly spaced at most ``spacing`` apart, ends
    included. Returns each point's line position, the points, and the length of
    line each point stands for."""
    lines = np.asarray(lines)
    lengths = shapely.length(lines)
    counts = np.ceil(lengths / spacing).astype(int) + 1
    counts[lengths == 0] = 1
    owner = np.repeat(np.arange(len(lines)), counts)
    first = np.repeat(np.cumsum(counts) - counts, counts)
    k = np.arange(counts.sum()) - first
    step = np.divide(lengths, counts - 1, out=np.zeros_like(lengths), where=counts > 1)
    points = shapely.line_interpolate_point(lines[owner], k * step[owner])
    end = (k == 0) | (k == counts[owner] - 1)
    share = np.where(end, step[owner] / 2, step[owner])
    return owner, points, share


def _flag_area(points, margin):
    """The bounding box of ``points``, grown by ``margin`` and rounded outwards
    to the metre."""
    minx, miny, maxx, maxy = shapely.total_bounds(points)
    return shapely.box(
        np.floor(minx - margin),
        np.floor(miny - margin),
        np.ceil(maxx + margin),
        np.ceil(maxy + margin),
    )


Detector = Callable[[Zone, DetectorConfig], gpd.GeoDataFrame]

# Detector name -> detector. The name is also its config section and the
# ``detector`` field of its flags.
DETECTORS: dict[str, Detector] = {
    "gap": gap_flags,
    "stub": stub_flags,
    "duplicate": duplicate_flags,
    "crossing": crossing_flags,
    "overture": overture_flags,
}
