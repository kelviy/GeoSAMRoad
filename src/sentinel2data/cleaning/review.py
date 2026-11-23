"""Review core: the review queue over one zone GeoPackage, with no QGIS import.

Every call reads the GeoPackage afresh and every decision is written straight
into it, so there is no state outside the file. The QGIS plugin and the CLI
are thin layers over these functions.
"""

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyogrio
import shapely

from sentinel2data.cleaning.contract import (
    FLAG_DECISIONS,
    FLAGS_LAYER,
    LABELS_LAYER,
    PENDING,
    ROAD_STATUSES,
    TILES_LAYER,
    parse_raw_ids,
)

# Flag type -> the defect a ``real`` decision deletes its label rows for.
DELETING_FLAG_TYPES = {"stub": "stub", "phantom?": "phantom"}

# A flag's map extent is its bounds grown by this much on every side.
FLAG_MARGIN_M = 50.0

Extent = tuple[float, float, float, float]  # minx, miny, maxx, maxy


class FlagAlreadyDecided(Exception):
    """A decision on a flag that already has one."""

    def __init__(self, fid, decision=None):
        self.fid, self.decision = fid, decision
        made = f" ({decision})" if decision else " since it was read"
        super().__init__(f"Flag {fid} is already decided{made}.")


class NothingToDelete(Exception):
    """``real`` on a deleting flag whose label rows can't be deleted from the queue."""

    def __init__(self, fid, raw_ids):
        self.fid, self.raw_ids = fid, raw_ids
        super().__init__(
            f"Flag {fid} names road rows (raw_id {raw_ids}) that run outside it, so "
            "deleting them would delete roads it does not cover. Mark it needs_qgis "
            "and fix it in QGIS."
        )


@dataclass(frozen=True)
class FlagItem:
    fid: int
    type: str
    decision: str
    extent: Extent

    @property
    def done(self) -> bool:
        return self.decision != PENDING


@dataclass(frozen=True)
class TileItem:
    fid: int
    tile_id: str
    reviewed: bool
    extent: Extent

    @property
    def done(self) -> bool:
        return self.reviewed


QueueItem = FlagItem | TileItem


@dataclass(frozen=True)
class Progress:
    """How far through its queue a zone is. A flag with any decision, including
    ``needs_qgis``, is done."""

    flags_decided: int
    flags_total: int
    tiles_reviewed: int
    tiles_kept: int

    @property
    def done(self) -> int:
        return self.flags_decided + self.tiles_reviewed

    @property
    def total(self) -> int:
        return self.flags_total + self.tiles_kept

    @property
    def remaining(self) -> int:
        return self.total - self.done


def build_queue(gpkg) -> list[QueueItem]:
    """Every flag, then every kept tile, each in spatial order.

    Tiles go in row then column order, north-west first. A flag goes with the
    tile nearest its representative point (the first such tile in that order
    when it lies on a shared edge), and inside a tile flags go north to south,
    then west to east. Ties go by fid.
    """
    tiles = pyogrio.read_dataframe(gpkg, layer=TILES_LAYER, fid_as_index=True)
    flags = pyogrio.read_dataframe(gpkg, layer=FLAGS_LAYER, fid_as_index=True)
    tiles = tiles.sort_values(["row", "col"], kind="stable")

    points = shapely.point_on_surface(flags.geometry.to_numpy())
    near_point, near_tile = shapely.STRtree(tiles.geometry.to_numpy()).query_nearest(
        points, all_matches=True
    )
    # query_nearest gives every tied tile; tiles are sorted, so the lowest
    # position is the first in row-column order.
    tile_pos = np.full(len(points), len(tiles))
    np.minimum.at(tile_pos, near_point, near_tile)
    order = np.lexsort(
        (
            flags.index.to_numpy(),
            shapely.get_x(points),
            -shapely.get_y(points),
            tile_pos,
        )
    )
    flag_items = [
        FlagItem(
            fid=int(flags.index[i]),
            type=str(flags["type"].iloc[i]),
            decision=_decision(flags["decision"].iloc[i]),
            extent=_grown(flags.geometry.iloc[i].bounds, FLAG_MARGIN_M),
        )
        for i in order
    ]

    kept = tiles[tiles["status"] == "kept"]
    tile_items = [
        TileItem(
            fid=int(fid),
            tile_id=str(tile["tile_id"]),
            reviewed=bool(pd.notna(tile["reviewed"]) and tile["reviewed"]),
            extent=tuple(tile.geometry.bounds),
        )
        for fid, tile in kept.iterrows()
    ]
    return [*flag_items, *tile_items]


def current_item(gpkg) -> QueueItem | None:
    """The first item in the queue that is not done, or None when all are."""
    return next((item for item in build_queue(gpkg) if not item.done), None)


def decide_flag(gpkg, fid: int, decision: str, *, annotator: str) -> tuple[int, ...]:
    """Record ``decision`` on pending flag ``fid``, with the annotator. Returns
    the labels fids the decision deleted. Refuses a decided flag
    (``FlagAlreadyDecided``).

    ``real`` on a flag type in ``DELETING_FLAG_TYPES`` also sets label rows
    to ``status = deleted``, with that type's defect and the annotator. Rows
    are never removed. Any other decision changes no label.

    * ``stub``: every road row the flag's ``raw_ids`` name. They make up the
      short branch.
    * ``phantom?``: the named road rows that lie wholly inside the flag area.
      A named row that runs outside it stays, because part of it matched the
      second source. If named road rows exist but none lies inside, nothing
      is written and ``NothingToDelete`` is raised.
    """
    if decision not in FLAG_DECISIONS or decision == PENDING:
        raise ValueError(
            f"decision must be one of {[d for d in FLAG_DECISIONS if d != PENDING]}, "
            f"got {decision!r}"
        )
    annotator = _checked_annotator(annotator)
    flag = _read_flag(gpkg, fid)
    if _decision(flag["decision"]) != PENDING:
        raise FlagAlreadyDecided(fid, flag["decision"])
    defect = DELETING_FLAG_TYPES.get(flag["type"]) if decision == "real" else None
    deleted = _rows_to_delete(gpkg, flag) if defect else []
    with _writing(gpkg) as con:
        # Pending again here, so a decision written since the read is kept.
        written = con.execute(
            f'UPDATE "{FLAGS_LAYER}" SET decision = ?, annotator = ? '
            "WHERE fid = ? AND coalesce(decision, ?) = ?",
            (decision, annotator, fid, PENDING, PENDING),
        ).rowcount
        if not written:
            raise FlagAlreadyDecided(fid)
        # Still a road here, so a row changed in QGIS since the read is kept.
        still_roads = ", ".join("?" * len(ROAD_STATUSES))
        deleted = [
            row
            for row in deleted
            if con.execute(
                f'UPDATE "{LABELS_LAYER}" SET status = ?, defect = ?, annotator = ? '
                f"WHERE fid = ? AND status IN ({still_roads})",
                ("deleted", defect, annotator, row, *ROAD_STATUSES),
            ).rowcount
        ]
    return tuple(deleted)


def _read_flag(gpkg, fid):
    flags = pyogrio.read_dataframe(
        gpkg, layer=FLAGS_LAYER, fid_as_index=True, where=f"fid = {int(fid)}"
    )
    if flags.empty:
        raise ValueError(f"{gpkg} has no flag with fid {fid}")
    return flags.iloc[0]


def _rows_to_delete(gpkg, flag) -> list[int]:
    """Labels fids of the rows ``real`` on ``flag`` deletes (see ``decide_flag``)."""
    raw_ids = parse_raw_ids(flag["raw_ids"])
    labels = pyogrio.read_dataframe(gpkg, layer=LABELS_LAYER, fid_as_index=True)
    named = labels[
        labels["raw_id"].isin(raw_ids) & labels["status"].isin(ROAD_STATUSES)
    ]
    if flag["type"] == "phantom?":
        inside = named[shapely.covers(flag.geometry, named.geometry.to_numpy())]
        if len(named) and inside.empty:
            raise NothingToDelete(int(flag.name), raw_ids)
        named = inside
    return [int(f) for f in named.index]


def review_tile(gpkg, fid: int, *, annotator: str, note: str | None = None):
    """Mark kept tile ``fid`` reviewed, with the annotator and an optional note.
    Reviewing it again replaces the annotator, and the note only when one is
    given."""
    annotator = _checked_annotator(annotator)
    tiles = pyogrio.read_dataframe(
        gpkg, layer=TILES_LAYER, read_geometry=False, where=f"fid = {int(fid)}"
    )
    if tiles.empty or tiles["status"].iloc[0] != "kept":
        raise ValueError(f"{gpkg}: tile {fid} is not a kept tile")
    with _writing(gpkg) as con:
        con.execute(
            f'UPDATE "{TILES_LAYER}" SET reviewed = 1, note = coalesce(?, note), '
            "annotator = ? WHERE fid = ?",
            (note, annotator, fid),
        )


def progress(gpkg) -> Progress:
    """Flags decided and kept tiles reviewed, out of the zone's queue."""
    queue = build_queue(gpkg)
    flags = [i for i in queue if isinstance(i, FlagItem)]
    tiles = [i for i in queue if isinstance(i, TileItem)]
    return Progress(
        flags_decided=sum(i.done for i in flags),
        flags_total=len(flags),
        tiles_reviewed=sum(i.done for i in tiles),
        tiles_kept=len(tiles),
    )


def _checked_annotator(annotator) -> str:
    if not isinstance(annotator, str) or not annotator.strip():
        raise ValueError(f"annotator must be a non-empty name, got {annotator!r}")
    return annotator.strip()


# GDAL's R-tree triggers call these. An UPDATE compiles every trigger on its
# table, so they must exist, but the review core never changes a geometry or
# a fid, so no trigger that calls them fires.
_GDAL_TRIGGER_FUNCTIONS = ("ST_IsEmpty", "ST_MinX", "ST_MaxX", "ST_MinY", "ST_MaxY")


def _never_called(*_):
    raise RuntimeError("the review core must not change a geometry or a fid")


@contextmanager
def _writing(gpkg):
    """A plain SQLite connection to ``gpkg`` in one transaction, committed on
    success and rolled back on error."""
    with closing(sqlite3.connect(gpkg)) as con, con:
        for name in _GDAL_TRIGGER_FUNCTIONS:
            con.create_function(name, 1, _never_called)
        yield con


def _decision(value) -> str:
    """A flag's decision, with an empty one read as pending."""
    return PENDING if pd.isna(value) else str(value)


def _grown(bounds, margin) -> Extent:
    minx, miny, maxx, maxy = bounds
    return (minx - margin, miny - margin, maxx + margin, maxy + margin)
