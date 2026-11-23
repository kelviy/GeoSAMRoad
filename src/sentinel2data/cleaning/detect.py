"""Detector runner: run every enabled detector over a zone GeoPackage and write
its flags back into it.

A run replaces the pending flags the run's detectors placed before, keeps every
other flag, and records itself and its full config in ``detector_runs``.
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
    DETECTOR_RUNS_LAYER,
    FLAG_FIELDS,
    FLAGS_LAYER,
    LABELS_LAYER,
    PENDING,
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
        flags = _as_flag_rows(pd.concat(found, ignore_index=True), zone.roads.crs)
        _replace_pending_flags(gpkg, flags, names)
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


def _replace_pending_flags(gpkg, flags: gpd.GeoDataFrame, detectors):
    """Swap the pending flags ``detectors`` placed before for ``flags``.

    A flag someone decided on or saved (it has an ``annotator``) is never
    touched, and a new flag with the same key as such a flag is dropped. So
    is a second new flag with the same key.
    """
    existing = pyogrio.read_dataframe(
        gpkg, layer=FLAGS_LAYER, read_geometry=False, fid_as_index=True
    )
    replace = existing["detector"].isin(detectors) & is_pending_detector_output(
        existing
    )
    with closing(sqlite3.connect(gpkg)) as con, con:
        con.executemany(
            f'DELETE FROM "{FLAGS_LAYER}" WHERE fid = ?',
            [(int(fid),) for fid in existing.index[replace]],
        )
    kept_keys = existing.loc[~replace, "flag_key"].dropna().tolist()
    keys = flags["flag_key"]
    new = flags[~keys.isin(kept_keys) & ~keys.duplicated()]
    if len(new):
        pyogrio.write_dataframe(new, gpkg, layer=FLAGS_LAYER, append=True)


def _record_run(gpkg, config: DetectorConfig):
    run = pd.DataFrame(
        {
            "run_at": [datetime.now(UTC).isoformat(timespec="seconds")],
            "config": [json.dumps(config.as_dict(), sort_keys=True)],
        }
    )
    pyogrio.write_dataframe(run, gpkg, layer=DETECTOR_RUNS_LAYER, append=True)
