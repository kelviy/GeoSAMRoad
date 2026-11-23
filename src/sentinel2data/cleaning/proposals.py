"""Accepting and rejecting proposals, and undoing either, inside the review
core's write transaction (see ``review.decide_flag``).

Accepting a gap proposal turns its ``proposed`` label row into an ``added``
road with defect ``gap`` and gives each road it ends on mid-segment a vertex
there, so the road graph, which joins lines only at shared vertices, joins
them. Rejecting removes the row. Both are logged, so changing the decision
puts the labels back.
"""

import json

import numpy as np
import shapely

from sentinel2data.cleaning import gpkg_geometry
from sentinel2data.cleaning.contract import (
    CONNECTOR_VERTICES_LAYER,
    FLAGS_LAYER,
    LABELS_LAYER,
    PROPOSAL_ROWS_LAYER,
    ROAD_STATUSES,
)

# A connector end this close to a road row lies on it.
ON_ROAD_M = 1e-6

# The review core's log tables for proposals: table -> column definitions.
TABLES = {
    # One row per proposed label row a decision accepted or removed, with the
    # whole row as it was, as JSON.
    PROPOSAL_ROWS_LAYER: "flag_fid INTEGER NOT NULL, label_fid INTEGER NOT NULL, "
    "removed INTEGER NOT NULL, before TEXT NOT NULL",
    # One row per vertex an accept added to a road row. status, defect and
    # annotator are the row's values before, set only when this vertex made a
    # kept row modified.
    CONNECTOR_VERTICES_LAYER: "flag_fid INTEGER NOT NULL, "
    "label_fid INTEGER NOT NULL, x REAL NOT NULL, y REAL NOT NULL, "
    "status TEXT, defect TEXT, annotator TEXT",
}


def accept(con, flag_fid, row, annotator):
    """Turn proposed label ``row`` into an added road with defect ``gap``, and
    give each road row a connector end lies on mid-segment a vertex there. A
    ``kept`` road so edited becomes ``modified`` with defect ``gap``. A row
    that is no longer ``proposed`` changed in QGIS and is left alone."""
    proposal = _label_row(con, row)
    if proposal is None or proposal["status"] != "proposed":
        return
    _log_row(con, flag_fid, row, proposal, removed=False)
    con.execute(
        f'UPDATE "{LABELS_LAYER}" SET status = ?, defect = ?, annotator = ? '
        "WHERE fid = ?",
        ("added", "gap", annotator, row),
    )
    geom = _geometry_column(con)
    connector = gpkg_geometry.decode(proposal[geom])
    for x, y in shapely.get_coordinates(connector)[[0, -1]]:
        for fid, road in _roads_at(con, (x, y), geom, exclude=row):
            line = _with_vertex(gpkg_geometry.decode(road[geom]), (x, y))
            if line is None:
                continue
            restyled = road["status"] == "kept"
            con.execute(
                f'INSERT INTO "{CONNECTOR_VERTICES_LAYER}" (flag_fid, label_fid, x, y, '
                "status, defect, annotator) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (flag_fid, fid, x, y)
                + (
                    (road["status"], road["defect"], road["annotator"])
                    if restyled
                    else (None,) * 3
                ),
            )
            road[geom] = gpkg_geometry.reencode(road[geom], line)
            if restyled:
                road |= {"status": "modified", "defect": "gap", "annotator": annotator}
            _write_label(con, fid, road)


def reject(con, flag_fid, row):
    """Remove proposed label ``row``, so labels holds only decided roads."""
    proposal = _label_row(con, row)
    if proposal is not None and proposal["status"] == "proposed":
        _log_row(con, flag_fid, row, proposal, removed=True)
        con.execute(f'DELETE FROM "{LABELS_LAYER}" WHERE fid = ?', (row,))


def undo(con, flag_fid, decided_by):
    """Undo what flag ``flag_fid``'s decision, made by ``decided_by``, did to
    labels, leaving whatever changed since as it is.

    * A removed row comes back, fid and all, unless a row with its fid exists.
    * An accepted row goes back to ``proposed`` while it is still ``added``
      by ``decided_by``. Its geometry stays, with any vertex another accept
      added to it.
    * Each added vertex goes, unless another accepted connector ends on it:
      then the vertex, and the edit, move to that connector's flag.
    * A row the accept made ``modified`` goes back to its old status, defect
      and annotator once no other accept's vertex remains on it, while it is
      still ``modified`` for ``gap`` by its last decider. While one remains,
      the row stays ``modified`` for that flag.
    """
    logged = con.execute(
        f'SELECT label_fid, removed, before FROM "{PROPOSAL_ROWS_LAYER}" '
        "WHERE flag_fid = ?",
        (flag_fid,),
    ).fetchall()
    for row, removed, before in logged:
        _restore_row(con, row, bool(removed), _loads(before), decided_by)
    con.execute(f'DELETE FROM "{PROPOSAL_ROWS_LAYER}" WHERE flag_fid = ?', (flag_fid,))

    vertices = con.execute(
        f"SELECT fid, label_fid, x, y, status, defect, annotator FROM "
        f'"{CONNECTOR_VERTICES_LAYER}" WHERE flag_fid = ? ORDER BY fid',
        (flag_fid,),
    ).fetchall()
    for entry, row, x, y, *was in vertices:
        _undo_vertex(con, entry, row, (x, y), was, decided_by)


def _restore_row(con, row, removed, before, decided_by):
    current = _label_row(con, row)
    if removed:
        if current is None:
            _write_label(con, row, before)
        return
    if current is not None and (
        current["status"],
        current["defect"],
        current["annotator"],
    ) == (
        "added",
        "gap",
        decided_by,
    ):
        con.execute(
            f'UPDATE "{LABELS_LAYER}" SET status = ?, defect = ?, annotator = ? '
            "WHERE fid = ?",
            (before["status"], before["defect"], before["annotator"], row),
        )


def _undo_vertex(con, entry, row, point, was, decided_by):
    road = _label_row(con, row)
    heir = _accepted_connector_ending_at(con, point)
    if heir is not None:
        con.execute(
            f'UPDATE "{CONNECTOR_VERTICES_LAYER}" SET flag_fid = ? WHERE fid = ?',
            (heir, entry),
        )
        if road is not None and was[0] is not None:
            _hand_row_to(con, row, heir)
        return
    con.execute(f'DELETE FROM "{CONNECTOR_VERTICES_LAYER}" WHERE fid = ?', (entry,))
    if road is None:
        return
    geom = _geometry_column(con)
    line = _without_vertex(gpkg_geometry.decode(road[geom]), point)
    if line is not None:
        road[geom] = gpkg_geometry.reencode(road[geom], line)
    if was[0] is not None:
        later = con.execute(
            f'SELECT fid, flag_fid FROM "{CONNECTOR_VERTICES_LAYER}" '
            "WHERE label_fid = ? ORDER BY fid LIMIT 1",
            (row,),
        ).fetchone()
        if later is not None:
            # The row stays modified for the next accept still on it.
            con.execute(
                f'UPDATE "{CONNECTOR_VERTICES_LAYER}" SET status = ?, defect = ?, '
                "annotator = ? WHERE fid = ?",
                (*was, later[0]),
            )
            road["annotator"] = _annotator_of(con, later[1])
        elif (road["status"], road["defect"], road["annotator"]) == (
            "modified",
            "gap",
            decided_by,
        ):
            road |= dict(zip(("status", "defect", "annotator"), was))
    _write_label(con, row, road)


def _hand_row_to(con, row, flag_fid):
    con.execute(
        f'UPDATE "{LABELS_LAYER}" SET annotator = ? WHERE fid = ?',
        (_annotator_of(con, flag_fid), row),
    )


def _annotator_of(con, flag_fid):
    (annotator,) = con.execute(
        f'SELECT annotator FROM "{FLAGS_LAYER}" WHERE fid = ?', (flag_fid,)
    ).fetchone()
    return annotator


def _accepted_connector_ending_at(con, point) -> int | None:
    """The fid of a flag whose accepted connector ends exactly at ``point``."""
    geom = _geometry_column(con)
    for fid, road in _roads_at(con, point, geom, exclude=None):
        if road["status"] != "added":
            continue
        ends = shapely.get_coordinates(gpkg_geometry.decode(road[geom]))[[0, -1]]
        if not (ends == np.asarray(point)).all(axis=1).any():
            continue
        flag = con.execute(
            f"SELECT fid FROM \"{FLAGS_LAYER}\" WHERE proposal_fid = ? AND decision = 'real'",
            (fid,),
        ).fetchone()
        if flag is not None:
            return flag[0]
    return None


def _roads_at(con, point, geom, exclude):
    """(fid, row) of each road row within ``ON_ROAD_M`` of ``point``, other
    than row ``exclude``, found through the layer's R-tree."""
    x, y = (float(v) for v in point)
    statuses = ", ".join("?" * len(ROAD_STATUSES))
    fids = con.execute(
        f'SELECT r.id FROM "rtree_{LABELS_LAYER}_{geom}" AS r '
        f'JOIN "{LABELS_LAYER}" AS l ON l.fid = r.id '
        "WHERE r.minx <= ? AND r.maxx >= ? AND r.miny <= ? AND r.maxy >= ? "
        f"AND l.fid IS NOT ? AND l.status IN ({statuses}) ORDER BY r.id",
        (
            x + ON_ROAD_M,
            x - ON_ROAD_M,
            y + ON_ROAD_M,
            y - ON_ROAD_M,
            exclude,
            *ROAD_STATUSES,
        ),
    ).fetchall()
    here = shapely.Point(x, y)
    rows = [(fid, _label_row(con, fid)) for (fid,) in fids]
    return [
        (fid, row)
        for fid, row in rows
        if row is not None
        and shapely.distance(gpkg_geometry.decode(row[geom]), here) <= ON_ROAD_M
    ]


def _with_vertex(line, point) -> shapely.LineString | None:
    """``line`` with ``point`` (x, y) inserted as a vertex on its nearest
    segment, Z interpolated. None when ``point`` is a vertex already."""
    coords = shapely.get_coordinates(line, include_z=shapely.has_z(line))
    xy, p = coords[:, :2], np.asarray(point, dtype=float)
    if (xy == p).all(axis=1).any():
        return None
    a, step = coords[:-1], coords[1:] - coords[:-1]
    sq = (step[:, :2] ** 2).sum(axis=1)
    t = np.divide(
        ((p - a[:, :2]) * step[:, :2]).sum(axis=1),
        sq,
        out=np.zeros_like(sq),
        where=sq > 0,
    ).clip(0, 1)
    gap = a[:, :2] + t[:, None] * step[:, :2] - p
    i = int(np.argmin(np.hypot(gap[:, 0], gap[:, 1])))
    vertex = a[i] + t[i] * step[i]
    vertex[:2] = p
    return shapely.LineString(np.insert(coords, i + 1, vertex, axis=0))


def _without_vertex(line, point) -> shapely.LineString | None:
    """``line`` without its first vertex at exactly ``point``, or None when it
    has none there or only two vertices."""
    coords = shapely.get_coordinates(line, include_z=shapely.has_z(line))
    at = np.flatnonzero((coords[:, :2] == np.asarray(point)).all(axis=1))
    if not len(at) or len(coords) <= 2:
        return None
    return shapely.LineString(np.delete(coords, at[0], axis=0))


def _geometry_column(con) -> str:
    (name,) = con.execute(
        "SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?",
        (LABELS_LAYER,),
    ).fetchone()
    return name


def _label_row(con, fid) -> dict | None:
    """Labels row ``fid`` as column -> value, or None when there is none."""
    cursor = con.execute(f'SELECT * FROM "{LABELS_LAYER}" WHERE fid = ?', (fid,))
    row = cursor.fetchone()
    return None if row is None else dict(zip((d[0] for d in cursor.description), row))


def _write_label(con, fid, row: dict):
    """Insert or update labels row ``fid`` to hold ``row``."""
    columns = [c for c in row if c != "fid"]
    names = ", ".join(f'"{c}"' for c in columns)
    con.execute(
        f'INSERT INTO "{LABELS_LAYER}" (fid, {names}) '
        f"VALUES (?, {', '.join('?' * len(columns))}) ON CONFLICT (fid) DO UPDATE SET "
        + ", ".join(f'"{c}" = excluded."{c}"' for c in columns),
        (fid, *(row[c] for c in columns)),
    )


def _log_row(con, flag_fid, row, before, removed):
    con.execute(
        f'INSERT INTO "{PROPOSAL_ROWS_LAYER}" (flag_fid, label_fid, removed, before) '
        "VALUES (?, ?, ?, ?)",
        (flag_fid, row, int(removed), _dumps(before)),
    )


def _dumps(row: dict) -> str:
    """A labels row as JSON, blobs as hex."""
    return json.dumps(
        {k: {"hex": v.hex()} if isinstance(v, bytes) else v for k, v in row.items()}
    )


def _loads(text: str) -> dict:
    return {
        k: bytes.fromhex(v["hex"]) if isinstance(v, dict) else v
        for k, v in json.loads(text).items()
    }
