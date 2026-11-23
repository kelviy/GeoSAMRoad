"""Detector runner: run every enabled detector over a zone GeoPackage and write
its flags back into it.

A run merges its flags into the ones already there by ``flag_key``: decided
flags never change, pending flags found again stay put, pending flags not found
again go, and new flags are added as pending. A proposal's ``proposed`` label
row is written and removed with its flag. Each run is recorded with its full
config in ``detector_runs``.
"""

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely

from sentinel2data.cleaning.contract import (
    ADDED_ROAD_CLASSES,
    DETECTOR_RUNS_LAYER,
    FLAG_FIELDS,
    FLAGS_LAYER,
    LABELS_LAYER,
    PENDING,
    ROAD_BUFFER,
    ROAD_SCALE,
    ROAD_STATUSES,
    TILES_LAYER,
    is_pending_detector_output,
)
from sentinel2data.cleaning.detectors import DETECTORS, DetectorConfig, Zone
from sentinel2data.cleaning.packager import ZoneIsOpen, is_open_elsewhere


class ZoneSchemaOutdated(Exception):
    """The zone GeoPackage was packaged before the current flag contract."""

    def __init__(self, gpkg):
        self.gpkg = Path(gpkg)
        super().__init__(
            f"{self.gpkg} has no detector_runs layer or an old flags schema. "
            "Repackage it with `package-zones` first."
        )


def check_schema(gpkg):
    """Raise ``ZoneSchemaOutdated`` unless ``gpkg`` has the current flag contract."""
    layers = {name for name, _ in pyogrio.list_layers(gpkg)}
    if DETECTOR_RUNS_LAYER not in layers or FLAGS_LAYER not in layers:
        raise ZoneSchemaOutdated(gpkg)
    if list(pyogrio.read_info(gpkg, layer=FLAGS_LAYER)["fields"]) != list(FLAG_FIELDS):
        raise ZoneSchemaOutdated(gpkg)


def find_packaged_zones(zones_dir) -> dict:
    """zone filename -> zone GeoPackage, for every zone folder under ``zones_dir``."""
    return {
        d.name: d / f"{d.name}.gpkg"
        for d in sorted(Path(zones_dir).iterdir())
        if (d / f"{d.name}.gpkg").is_file()
    }


def load_zone(gpkg) -> Zone:
    labels = pyogrio.read_dataframe(gpkg, layer=LABELS_LAYER, fid_as_index=True)
    tiles = pyogrio.read_dataframe(gpkg, layer=TILES_LAYER)
    roads = labels[
        labels["status"].isin(ROAD_STATUSES)
        & ~labels.geometry.is_empty
        & labels.geometry.notna()
    ]
    roads = roads.explode(index_parts=False)
    # Real zones store LineString Z; flags are 2D locations.
    roads = roads.set_geometry(shapely.force_2d(roads.geometry.values), crs=roads.crs)

    kept_area = shapely.union_all(tiles.geometry[tiles["status"] == "kept"].to_numpy())
    # Tile positions cover the whole zone, partial edge tiles included.
    boundary = shapely.box(*tiles.total_bounds).boundary
    return Zone(roads=roads, kept_area=kept_area, boundary=boundary)


def detect_zone(gpkg, config: DetectorConfig) -> Counter:
    """Run the enabled detectors on one zone GeoPackage. Returns the number of
    flags each flag type found. Refuses a zone open in QGIS (``ZoneIsOpen``) or
    packaged before the current flag contract (``ZoneSchemaOutdated``)."""
    gpkg = Path(gpkg)
    if is_open_elsewhere(gpkg):
        raise ZoneIsOpen(gpkg)
    check_schema(gpkg)
    zone = load_zone(gpkg)
    names = [name for name in DETECTORS if getattr(config, name).enabled]
    found = [DETECTORS[name](zone, config).assign(detector=name) for name in names]
    if found:
        everything = pd.concat(found, ignore_index=True)
        flags = _as_flag_rows(everything, zone.roads.crs)
        proposals = [
            p if isinstance(p, shapely.Geometry) else None
            for p in (
                everything["proposal"]
                if "proposal" in everything
                else [None] * len(everything)
            )
        ]
        _merge_flags(gpkg, flags, proposals, names)
    _record_run(gpkg, config)
    return Counter(t for flags in found for t in flags["type"])


def _as_flag_rows(found: pd.DataFrame, crs) -> gpd.GeoDataFrame:
    rows = pd.DataFrame(
        {
            name: found[name]
            if name in found
            else pd.Series([None] * len(found), dtype=dtype)
            for name, dtype in FLAG_FIELDS.items()
        }
    ).astype(FLAG_FIELDS)
    rows["decision"] = PENDING
    return gpd.GeoDataFrame(
        rows, geometry=gpd.GeoSeries(found.geometry.values, crs=crs)
    )


def _merge_flags(
    gpkg, found: gpd.GeoDataFrame, proposals: list[shapely.Geometry | None], detectors
):
    """Merge this run's ``found`` flags into the flags layer, matching by ``flag_key``.

    ``proposals`` holds each found flag's proposed label geometry, or None.

    Only pending detector output from ``detectors`` is replaceable: a pending
    flag no one saved, whose ``proposed`` row, if it has one, no one saved
    either. A replaceable flag found again with the same proposal stays as it
    is, fid included; any other replaceable flag is deleted with its
    ``proposed`` row. A found flag whose key no remaining flag has is appended
    as pending, after its proposal is written to labels. Every other flag,
    decided or saved by someone, is never touched.
    """
    existing = pyogrio.read_dataframe(
        gpkg, layer=FLAGS_LAYER, read_geometry=False, fid_as_index=True
    )
    stored_proposals = _read_proposals(gpkg, existing["proposal_fid"])
    proposal_untouched = existing["proposal_fid"].isna() | existing[
        "proposal_fid"
    ].isin(stored_proposals.index)
    replaceable = (
        existing["detector"].isin(detectors)
        & is_pending_detector_output(existing)
        & proposal_untouched
    )
    stored_keys, found_keys = existing["flag_key"], found["flag_key"]
    # A replaceable flag also goes when a kept flag already has its key.
    taken = stored_keys.isin(stored_keys[~replaceable])
    repeat = stored_keys.where(replaceable).duplicated() & replaceable
    # Of found flags sharing a key, only the first is ever written.
    found_proposal = {}
    for key, proposal in zip(found_keys, proposals):
        found_proposal.setdefault(key, proposal)
    changed = pd.Series(
        [
            key in found_proposal
            and not _same_proposal(stored_proposals.get(fid), found_proposal[key])
            for key, fid in zip(stored_keys, existing["proposal_fid"])
        ],
        index=existing.index,
        dtype=bool,
    )
    stale = replaceable & (~stored_keys.isin(found_keys) | taken | repeat | changed)
    with closing(sqlite3.connect(gpkg)) as con, con:
        con.executemany(
            f'DELETE FROM "{FLAGS_LAYER}" WHERE fid = ?',
            [(int(fid),) for fid in existing.index[stale]],
        )
        con.executemany(
            f'DELETE FROM "{LABELS_LAYER}" WHERE fid = ? '
            "AND status = 'proposed' AND annotator IS NULL",
            [(int(fid),) for fid in existing.loc[stale, "proposal_fid"].dropna()],
        )
    is_new = ~found_keys.isin(stored_keys[~stale]) & ~found_keys.duplicated()
    new = found[is_new].copy()
    if not len(new):
        return
    new_proposals = [p for p, n in zip(proposals, is_new) if n]
    with_proposal = [p is not None for p in new_proposals]
    last = _last_label_fid(gpkg)
    try:
        fids = _write_proposals(
            gpkg, [p for p in new_proposals if p is not None], found.crs, after=last
        )
        new.loc[with_proposal, "proposal_fid"] = fids
        pyogrio.write_dataframe(new, gpkg, layer=FLAGS_LAYER, append=True)
    except Exception:
        # Every labels row after ``last`` is one this run wrote.
        with closing(sqlite3.connect(gpkg)) as con, con:
            con.execute(f'DELETE FROM "{LABELS_LAYER}" WHERE fid > ?', (last,))
        raise


def _read_proposals(gpkg, proposal_fids) -> gpd.GeoSeries:
    """The 2D geometry of each ``proposed`` label row no one saved, by fid,
    among ``proposal_fids``. A fid whose row is gone is left out."""
    fids = sorted({int(f) for f in proposal_fids.dropna()})
    if not fids:
        return gpd.GeoSeries([], dtype=object)
    with closing(sqlite3.connect(gpkg)) as con:
        untouched = [
            fid
            for (fid,) in con.execute(
                f"SELECT fid FROM \"{LABELS_LAYER}\" WHERE status = 'proposed' "
                f"AND annotator IS NULL AND fid IN ({', '.join('?' * len(fids))})",
                fids,
            )
        ]
    if not untouched:
        return gpd.GeoSeries([], dtype=object)
    rows = pyogrio.read_dataframe(
        gpkg, layer=LABELS_LAYER, fids=untouched, columns=[], fid_as_index=True
    )
    return gpd.GeoSeries(shapely.force_2d(rows.geometry.values), index=rows.index)


def _same_proposal(stored, found) -> bool:
    if stored is None or found is None:
        return stored is None and found is None
    return bool(shapely.equals_exact(stored, found, tolerance=0))


def _last_label_fid(gpkg) -> int:
    with closing(sqlite3.connect(gpkg)) as con:
        (last,) = con.execute(
            f'SELECT coalesce(max(fid), 0) FROM "{LABELS_LAYER}"'
        ).fetchone()
    return int(last)


def _write_proposals(gpkg, geometries, crs, after) -> list[int]:
    """Append ``geometries`` to labels as ``proposed`` gap connectors and return
    their fids, in order. ``after`` is the largest labels fid before the write.

    A connector is a road an annotator would add, so it takes the default added
    road class. It has no ``raw_id`` and no ``annotator``.
    """
    if not geometries:
        return []
    road_class = ADDED_ROAD_CLASSES[0]
    values = {
        "status": "proposed",
        "defect": "gap",
        "road_class": road_class,
        "scale_class": ROAD_SCALE[road_class],
        "buffer": ROAD_BUFFER[road_class],
    }
    fields = pyogrio.read_info(gpkg, layer=LABELS_LAYER)["fields"]
    rows = gpd.GeoDataFrame(
        {f: [values[f]] * len(geometries) for f in fields if f in values},
        geometry=gpd.GeoSeries(geometries, crs=crs),
    )
    pyogrio.write_dataframe(rows, gpkg, layer=LABELS_LAYER, append=True)
    # GeoPackage fids only grow, so the new rows are the ones after ``after``.
    with closing(sqlite3.connect(gpkg)) as con:
        fids = [
            fid
            for (fid,) in con.execute(
                f'SELECT fid FROM "{LABELS_LAYER}" WHERE fid > ? ORDER BY fid', (after,)
            )
        ]
    if len(fids) != len(geometries):
        raise RuntimeError(
            f"{gpkg}: wrote {len(geometries)} proposals but found {len(fids)} new "
            "labels rows"
        )
    return fids


def _record_run(gpkg, config: DetectorConfig):
    run = pd.DataFrame(
        {
            "run_at": [datetime.now(UTC).isoformat(timespec="seconds")],
            "config": [json.dumps(config.as_dict(), sort_keys=True)],
        }
    )
    pyogrio.write_dataframe(run, gpkg, layer=DETECTOR_RUNS_LAYER, append=True)
