"""Review core: the review queue over one zone GeoPackage, with no QGIS import.

Every call reads the GeoPackage afresh and every decision is written straight
into it, so there is no state outside the file. The QGIS plugin and the CLI
are thin layers over these functions.
"""

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pyogrio
import shapely

from sentinel2data.cleaning import gpkg_geometry, proposals
from sentinel2data.cleaning.contract import (
    DETECTOR_RUNS_LAYER,
    FLAG_DECISIONS,
    FLAG_DELETIONS_LAYER,
    FLAG_SOURCES,
    FLAG_TYPES,
    FLAGS_LAYER,
    LABELS_LAYER,
    PENDING,
    REVIEW_POSITION_LAYER,
    ROAD_STATUSES,
    TILES_LAYER,
    parse_raw_ids,
)

# Flag type -> the defect a ``real`` decision deletes its label rows for.
DELETING_FLAG_TYPES = {"stub": "stub", "phantom?": "phantom"}

# A flag's map extent is its bounds grown by this much on every side.
FLAG_MARGIN_M = 50.0

Extent = tuple[float, float, float, float]  # minx, miny, maxx, maxy


class FlagChangedSinceRead(Exception):
    """Someone else changed the flag's decision between the read and the write."""

    def __init__(self, fid):
        self.fid = fid
        super().__init__(f"Flag {fid} was decided by someone else since it was read.")


class NothingToDelete(Exception):
    """``real`` on a deleting flag whose label rows can't be deleted from the queue."""

    def __init__(self, fid, raw_ids):
        self.fid, self.raw_ids = fid, raw_ids
        super().__init__(
            f"Flag {fid} names road rows (raw_id {raw_ids}) that run outside it, so "
            "deleting them would delete roads it does not cover. Mark it needs_qgis "
            "and fix it in QGIS."
        )


# The kinds of queue item. A proposal is a flag with a ``proposed`` label row.
ITEM_KINDS = ("proposal", "flag", "tile")


@dataclass(frozen=True)
class FlagItem:
    fid: int
    type: str
    detector: str
    source: str
    is_proposal: bool
    decision: str
    location: tuple[float, float]  # a point on the flag
    extent: Extent

    @property
    def kind(self) -> str:
        return "proposal" if self.is_proposal else "flag"

    @property
    def done(self) -> bool:
        return self.decision != PENDING


@dataclass(frozen=True)
class TileItem:
    fid: int
    tile_id: str
    reviewed: bool
    extent: Extent

    kind = "tile"

    @property
    def done(self) -> bool:
        return self.reviewed


QueueItem = FlagItem | TileItem


@dataclass(frozen=True)
class QueueFilter:
    """Which items the queue keeps. Every field given must match, so a flag
    type or source keeps no tiles. None keeps everything."""

    kind: str | None = None
    flag_type: str | None = None
    source: str | None = None

    def __post_init__(self):
        if self.kind is not None and self.kind not in ITEM_KINDS:
            raise ValueError(f"kind must be one of {ITEM_KINDS}, got {self.kind!r}")
        if self.flag_type is not None and self.flag_type not in FLAG_TYPES:
            raise ValueError(
                f"flag type must be one of {FLAG_TYPES}, got {self.flag_type!r}"
            )
        if self.source is not None and self.source not in FLAG_SOURCES:
            raise ValueError(
                f"source must be one of {FLAG_SOURCES}, got {self.source!r}"
            )

    def keeps(self, item: QueueItem) -> bool:
        if self.kind is not None and item.kind != self.kind:
            return False
        if self.flag_type is None and self.source is None:
            return True
        return (
            isinstance(item, FlagItem)
            and self.flag_type in (None, item.type)
            and self.source in (None, item.source)
        )


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


def build_queue(gpkg, queue_filter: QueueFilter | None = None) -> list[QueueItem]:
    """Every proposal, then every other flag, then every kept tile, each in
    spatial order, keeping only the items ``queue_filter`` keeps.

    Tiles go in row then column order, north-west first. A flag goes with the
    tile nearest its representative point (the first such tile in that order
    when it lies on a shared edge), and inside a tile flags go north to south,
    then west to east. Ties go by fid.
    """
    hidden = hidden_detectors(gpkg)
    return [
        i
        for i in _all_items(gpkg)
        if not (isinstance(i, FlagItem) and i.detector in hidden)
        and (queue_filter is None or queue_filter.keeps(i))
    ]


def hidden_detectors(gpkg) -> set[str]:
    """Detectors whose flags the queue leaves out: those marked ``hidden`` in
    the config of the zone's latest ``detect`` run. None before any run."""
    runs = pyogrio.read_dataframe(gpkg, layer=DETECTOR_RUNS_LAYER, fid_as_index=True)
    if runs.empty:
        return set()
    config = json.loads(runs.sort_index()["config"].iloc[-1])
    return {
        name
        for name, section in config.items()
        if isinstance(section, dict) and section.get("hidden")
    }


def _all_items(gpkg) -> list[QueueItem]:
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
            flags["proposal_fid"].isna().to_numpy(),  # proposals first
        )
    )
    flag_items = [
        FlagItem(
            fid=int(flags.index[i]),
            type=str(flags["type"].iloc[i]),
            detector=str(flags["detector"].iloc[i]),
            source=str(flags["source"].iloc[i]),
            is_proposal=bool(pd.notna(flags["proposal_fid"].iloc[i])),
            decision=_decision(flags["decision"].iloc[i]),
            location=(float(shapely.get_x(points[i])), float(shapely.get_y(points[i]))),
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


def needs_qgis_items(gpkg) -> list[FlagItem]:
    """Every flag marked ``needs_qgis``, in queue order, hidden detectors'
    flags included: a hidden flag marked so still needs fixing in QGIS."""
    return [
        i
        for i in _all_items(gpkg)
        if isinstance(i, FlagItem) and i.decision == "needs_qgis"
    ]


def current_item(gpkg, queue_filter: QueueFilter | None = None) -> QueueItem | None:
    """The item the annotator is on: the stored position, when it is in the
    queue, or else the first item that is not done. None when all are done.

    The position is stored by ``go_back`` and ``go_forward`` and cleared by
    every decision, so reopening the zone resumes where the annotator was."""
    return _current(gpkg, build_queue(gpkg, queue_filter))


def go_back(gpkg, queue_filter: QueueFilter | None = None) -> QueueItem | None:
    """Step to the item before the current one and store it as the position.
    When every item is done, the last item. None, and no move, at the first."""
    queue = build_queue(gpkg, queue_filter)
    current = _current(gpkg, queue)
    at = len(queue) if current is None else queue.index(current)
    if at == 0:
        return None
    _store_position(gpkg, queue[at - 1])
    return queue[at - 1]


def go_forward(gpkg, queue_filter: QueueFilter | None = None) -> QueueItem | None:
    """Step to the item after the current one and store it as the position.
    None, and no move, at the last item or when every item is done."""
    queue = build_queue(gpkg, queue_filter)
    current = _current(gpkg, queue)
    if current is None or current == queue[-1]:
        return None
    following = queue[queue.index(current) + 1]
    _store_position(gpkg, following)
    return following


def _current(gpkg, queue: list[QueueItem]) -> QueueItem | None:
    position = _read_position(gpkg)
    stored = next((i for i in queue if _item_key(i) == position), None)
    return stored or next((i for i in queue if not i.done), None)


def _item_key(item: QueueItem) -> tuple[str, int]:
    """The layer and fid that name a queue item."""
    return (TILES_LAYER if isinstance(item, TileItem) else FLAGS_LAYER, item.fid)


def _read_position(gpkg) -> tuple[str, int] | None:
    with closing(sqlite3.connect(gpkg)) as con:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (REVIEW_POSITION_LAYER,),
        ).fetchone()
        row = (
            exists
            and con.execute(
                f'SELECT layer, item_fid FROM "{REVIEW_POSITION_LAYER}"'
            ).fetchone()
        )
    return (row[0], int(row[1])) if row else None


def _store_position(gpkg, item: QueueItem):
    with _writing(gpkg) as con:
        _clear_position(con)
        con.execute(
            f'INSERT INTO "{REVIEW_POSITION_LAYER}" (layer, item_fid) VALUES (?, ?)',
            _item_key(item),
        )


def decide_flag(gpkg, fid: int, decision: str, *, annotator: str) -> tuple[int, ...]:
    """Record ``decision`` on flag ``fid``, with the annotator. Returns the
    labels fids the decision deleted.

    ``real`` on a flag type in ``DELETING_FLAG_TYPES`` also sets label rows
    to ``status = deleted``, with that type's defect and the annotator. Rows
    are never removed.

    On a proposal, ``real`` accepts it and ``not_defect`` rejects it (see
    ``proposals``). Any other decision changes no label.

    * ``stub``: every road row the flag's ``raw_ids`` name. They make up the
      short branch.
    * ``phantom?``: the named road rows that lie wholly inside the flag area.
      A named row that runs outside it stays, because part of it matched the
      second source. If named road rows exist but none lies inside, nothing
      is written and ``NothingToDelete`` is raised.

    A decided flag can be decided again. The old decision's deletions are
    undone first: each row goes back to the status, defect and annotator it
    had, unless it changed since. The same decision again changes nothing.
    Refuses a flag whose decision changed since it was read
    (``FlagChangedSinceRead``).
    """
    if decision not in FLAG_DECISIONS or decision == PENDING:
        raise ValueError(
            f"decision must be one of {[d for d in FLAG_DECISIONS if d != PENDING]}, "
            f"got {decision!r}"
        )
    annotator = _checked_annotator(annotator)
    flag = _read_flag(gpkg, fid)
    old = _decision(flag["decision"])
    if old == decision:
        return ()
    defect = DELETING_FLAG_TYPES.get(flag["type"]) if decision == "real" else None
    deleted = _rows_to_delete(gpkg, flag) if defect else []
    with _writing(gpkg) as con:
        # Unchanged here, so a decision written since the read is kept.
        written = con.execute(
            f'UPDATE "{FLAGS_LAYER}" SET decision = ?, annotator = ? '
            "WHERE fid = ? AND coalesce(decision, ?) = ?",
            (decision, annotator, fid, PENDING, old),
        ).rowcount
        if not written:
            raise FlagChangedSinceRead(fid)
        _clear_position(con)
        _undo_deletions(
            con, fid, DELETING_FLAG_TYPES.get(flag["type"]), flag["annotator"]
        )
        deleted = [
            row for row in deleted if _delete_row(con, fid, row, defect, annotator)
        ]
        if pd.notna(flag["proposal_fid"]):
            proposals.undo(con, fid, _or_none(flag["annotator"]))
            if decision == "real":
                proposals.accept(con, fid, int(flag["proposal_fid"]), annotator)
            elif decision == "not_defect":
                proposals.reject(con, fid, int(flag["proposal_fid"]))
    return tuple(deleted)


def _delete_row(con, flag_fid, row, defect, annotator) -> bool:
    """Delete label ``row`` for ``flag_fid`` and log what it was, if it is
    still a road here; a row changed in QGIS since the read is kept."""
    still_roads = ", ".join("?" * len(ROAD_STATUSES))
    was = con.execute(
        f'SELECT status, defect, annotator FROM "{LABELS_LAYER}" '
        f"WHERE fid = ? AND status IN ({still_roads})",
        (row, *ROAD_STATUSES),
    ).fetchone()
    if was is None:
        return False
    con.execute(
        f'INSERT INTO "{FLAG_DELETIONS_LAYER}" '
        "(flag_fid, label_fid, status, defect, annotator) VALUES (?, ?, ?, ?, ?)",
        (flag_fid, row, *was),
    )
    con.execute(
        f'UPDATE "{LABELS_LAYER}" SET status = ?, defect = ?, annotator = ? '
        "WHERE fid = ?",
        ("deleted", defect, annotator, row),
    )
    return True


def _undo_deletions(con, flag_fid, defect, deleted_by):
    """Put back each row ``flag_fid``'s decision deleted, as the log has it,
    if the row is still deleted as that decision left it.

    A row that another ``real`` deleting flag also names stays deleted, now
    for that flag: its log entry, defect and annotator move to it, so
    changing that flag later puts the row back."""
    logged = con.execute(
        f"SELECT l.fid, l.label_fid, l.status, l.defect, l.annotator, r.raw_id "
        f'FROM "{FLAG_DELETIONS_LAYER}" AS l JOIN "{LABELS_LAYER}" AS r '
        "ON r.fid = l.label_fid WHERE l.flag_fid = ? AND r.status = 'deleted' "
        "AND r.defect IS ? AND r.annotator IS ?",
        (flag_fid, defect, _or_none(deleted_by)),
    ).fetchall()
    others = _other_real_flags(con, flag_fid)
    for log_fid, row, status, was_defect, was_annotator, raw_id in logged:
        holder = next((o for o in others if raw_id in o[1]), None)
        if holder is None:
            con.execute(
                f'UPDATE "{LABELS_LAYER}" SET status = ?, defect = ?, annotator = ? '
                "WHERE fid = ?",
                (status, was_defect, was_annotator, row),
            )
            continue
        holder_fid, _, holder_defect, holder_annotator = holder
        con.execute(
            f'UPDATE "{FLAG_DELETIONS_LAYER}" SET flag_fid = ? WHERE fid = ?',
            (holder_fid, log_fid),
        )
        con.execute(
            f'UPDATE "{LABELS_LAYER}" SET defect = ?, annotator = ? WHERE fid = ?',
            (holder_defect, holder_annotator, row),
        )
    con.execute(f'DELETE FROM "{FLAG_DELETIONS_LAYER}" WHERE flag_fid = ?', (flag_fid,))


def _other_real_flags(con, flag_fid):
    """(fid, raw_ids, defect, annotator) of every other flag decided ``real``
    whose type deletes rows."""
    types = list(DELETING_FLAG_TYPES)
    rows = con.execute(
        f'SELECT fid, type, raw_ids, annotator FROM "{FLAGS_LAYER}" '
        f"WHERE decision = 'real' AND fid != ? AND type IN ({', '.join('?' * len(types))}) "
        "ORDER BY fid",
        (flag_fid, *types),
    ).fetchall()
    return [
        (fid, set(parse_raw_ids(raw_ids)), DELETING_FLAG_TYPES[kind], annotator)
        for fid, kind, raw_ids, annotator in rows
    ]


def _clear_position(con):
    _ensure_review_tables(con)
    con.execute(f'DELETE FROM "{REVIEW_POSITION_LAYER}"')


def _ensure_review_tables(con):
    """Create the review core's own tables, registered as GeoPackage
    attribute tables so GDAL and QGIS list them, if they are missing."""
    tables = {
        # One row per label row a ``real`` decision deleted, with what it was.
        FLAG_DELETIONS_LAYER: "flag_fid INTEGER NOT NULL, label_fid INTEGER NOT NULL, "
        "status TEXT, defect TEXT, annotator TEXT",
        # At most one row: the layer and fid of the item the annotator moved to.
        REVIEW_POSITION_LAYER: "layer TEXT NOT NULL, item_fid INTEGER NOT NULL",
    }
    for name, columns in (tables | proposals.TABLES).items():
        con.execute(
            f'CREATE TABLE IF NOT EXISTS "{name}" '
            f"(fid INTEGER PRIMARY KEY AUTOINCREMENT, {columns})"
        )
        con.execute(
            "INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier) "
            "VALUES (?, 'attributes', ?)",
            (name, name),
        )


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
        _clear_position(con)
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


@contextmanager
def _writing(gpkg):
    """A plain SQLite connection to ``gpkg`` in one transaction, committed on
    success and rolled back on error."""
    with closing(sqlite3.connect(gpkg)) as con, con:
        gpkg_geometry.register_rtree_functions(con)
        yield con


def _or_none(value):
    return None if pd.isna(value) else value


def _decision(value) -> str:
    """A flag's decision, with an empty one read as pending."""
    return PENDING if pd.isna(value) else str(value)


def _grown(bounds, margin) -> Extent:
    minx, miny, maxx, maxy = bounds
    return (minx - margin, miny - margin, maxx + margin, maxy + margin)
