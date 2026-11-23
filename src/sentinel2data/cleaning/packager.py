"""Zone packager: one zone -> the folder an annotator opens to clean it.

``<output_dir>/<zone>/`` holds:

* ``<zone>.gpkg``: the zone GeoPackage (raw labels, labels, tiles, flags,
  detector runs).
* ``<zone>.qgs``: the QGIS project an annotator opens (see ``qgis_project``).
* ``<zone>_s2_rgbnir.tif``: the zone's Sentinel-2 red, green, blue and NIR,
  each enhanced the way the generator's enhanced-RGB bands are (gamma +
  CLAHE), as 8-bit with 0 = no data.
* ``<zone>_overture.gpkg``: the second-source roads clipped to the zone, when
  packaged with one. A separate file, so no second-source geometry ever sits
  in the zone GeoPackage that becomes the published labels.
"""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
from rasterio.enums import Resampling
import shapely
import yaml

from sentinel2data.cleaning.contract import (
    DETECTOR_RUN_FIELDS,
    DETECTOR_RUNS_LAYER,
    FLAG_FIELDS,
    FLAGS_LAYER,
    LABELS_LAYER,
    OVERTURE_LAYER,
    PENDING,
    is_pending_detector_output,
    RAW_LABELS_LAYER,
    TILES_LAYER,
)
from sentinel2data.cleaning.qgis_project import write_zone_project
from sentinel2data.generator.config import EXCLUDED_ROAD_CLASSES, DatasetPaths, RGBEnhanceConfig
from sentinel2data.generator.helper import enhance_rgb
from sentinel2data.generator.labels import exclude_road_classes, load_zone_roads
from sentinel2data.generator.naming import assign_zone_metadata
from sentinel2data.generator.pipeline import PreSplitImagerySource


class ZoneIsOpen(Exception):
    """The zone GeoPackage is open in another program."""

    def __init__(self, gpkg):
        self.gpkg = Path(gpkg)
        super().__init__(
            f"{self.gpkg} is open in another program (it has a -wal file). Close it in QGIS "
            "and re-run. After a QGIS crash, open and close the project once first."
        )


class ZoneHasCleaningWork(Exception):
    """The zone GeoPackage already holds cleaning work."""

    def __init__(self, gpkg, work):
        self.gpkg, self.work = Path(gpkg), list(work)
        super().__init__(
            f"{self.gpkg} already holds cleaning work ({'; '.join(self.work)}). "
            "Re-run with --overwrite to repackage it (the old file is kept as a .bak)."
        )


@dataclass(frozen=True)
class ZoneInputs:
    zone_filename: str
    split: str
    cog_path: Path


@dataclass(frozen=True)
class ZoneFolder:
    """Paths of one packaged zone."""
    root: Path
    zone_filename: str

    @property
    def gpkg(self) -> Path:
        return self.root / f"{self.zone_filename}.gpkg"

    @property
    def project(self) -> Path:
        return self.root / f"{self.zone_filename}.qgs"

    @property
    def s2_imagery(self) -> Path:
        return self.root / f"{self.zone_filename}_s2_rgbnir.tif"

    @property
    def overture(self) -> Path:
        return self.root / f"{self.zone_filename}_overture.gpkg"


def find_zones(imagery_dir) -> dict:
    """zone filename -> ZoneInputs, for every zone COG under the pre-split root."""
    flat = [(split, p) for split, paths in PreSplitImagerySource(imagery_dir).enumerate() for p in paths]
    meta = assign_zone_metadata(flat)
    return {
        meta[(split, p)].zone_filename: ZoneInputs(meta[(split, p)].zone_filename, split, Path(p))
        for split, p in flat
    }


def baseline_excluded_classes(dataset_dir) -> tuple:
    """Road classes the baseline excluded, read from its generation report."""
    report = DatasetPaths(dataset_dir).report_path
    if not report.exists():
        return EXCLUDED_ROAD_CLASSES
    labels = (yaml.safe_load(report.read_text()) or {}).get("label_changes", {})
    return tuple(labels.get("excluded_road_classes", EXCLUDED_ROAD_CLASSES))


def raw_labels(cog_path, roads_path, excluded_classes) -> gpd.GeoDataFrame:
    """The zone's baseline roads (as the generator labels them), with ``raw_id``.

    Roads are clipped to the zone. A road the zone edge cuts into several
    pieces becomes one row per piece. ``raw_id`` numbers the rows from 1 in an
    order set by their geometry and class alone, so it does not depend on the
    row order of the road parquet.
    """
    zone, _ = exclude_road_classes(load_zone_roads(cog_path, roads_path), excluded_classes)
    roads = zone.roads
    geom = roads.geometry.name
    attrs = [c for c in roads.columns if c != geom and _is_scalar(roads[c])]
    roads = roads[attrs + [geom]].copy()
    # Clipping can split a road at its own vertices; rejoin before exploding.
    roads[geom] = shapely.line_merge(roads.geometry.values, directed=True)
    roads = roads.explode(index_parts=False)
    roads = roads[(roads.geom_type == "LineString") & (roads.length > 0)]
    if geom != "geometry":
        roads = roads.rename_geometry("geometry")

    key = pd.DataFrame({
        "wkb": shapely.to_wkb(roads.geometry.values, hex=True),
        "cls": roads["road_class"].astype(str).to_numpy() if "road_class" in roads else "",
    })
    order = key.sort_values(["wkb", "cls"], kind="stable").index
    roads = roads.iloc[order].reset_index(drop=True)
    roads.insert(0, "raw_id", pd.array(range(1, len(roads) + 1), dtype="int64"))
    return gpd.GeoDataFrame(roads, geometry="geometry", crs=zone.crs)


def _is_scalar(col) -> bool:
    sample = col.dropna()
    return sample.empty or not isinstance(sample.iloc[0], (dict, list, tuple))


def editable_labels(raw: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    labels = raw.copy()
    labels.insert(1, "status", "kept")
    labels.insert(2, "defect", pd.array([None] * len(raw), dtype="string"))
    labels.insert(3, "annotator", pd.array([None] * len(raw), dtype="string"))
    return labels


def tile_grid(tile_status_path) -> gpd.GeoDataFrame:
    table = gpd.read_parquet(tile_status_path)
    n = len(table)
    return gpd.GeoDataFrame(
        {
            "tile_id": table["tile_id"].to_numpy(),
            "row": table["row"].astype("int64").to_numpy(),
            "col": table["col"].astype("int64").to_numpy(),
            "status": table["status"].to_numpy(),
            "reviewed": [False] * n,
            "note": pd.array([None] * n, dtype="string"),
            "annotator": pd.array([None] * n, dtype="string"),
        },
        geometry=table.geometry.to_numpy(),
        crs=table.crs,
    )


def _empty_table(fields: dict) -> pd.DataFrame:
    return pd.DataFrame({name: pd.Series([], dtype=dtype) for name, dtype in fields.items()})


def empty_flags(crs) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(_empty_table(FLAG_FIELDS), geometry=gpd.GeoSeries([], crs=crs))


def write_zone_gpkg(path, raw, tiles):
    """Write the contract layers to a fresh GeoPackage at ``path``."""
    path = Path(path)
    if path.exists():
        path.unlink()
    raw.to_file(path, layer=RAW_LABELS_LAYER, driver="GPKG", geometry_type="LineString")
    editable_labels(raw).to_file(path, layer=LABELS_LAYER, driver="GPKG", geometry_type="LineString")
    tiles.to_file(path, layer=TILES_LAYER, driver="GPKG", geometry_type="Polygon")
    # Flags hold points or areas, so the layer's geometry type is generic.
    empty_flags(raw.crs).to_file(path, layer=FLAGS_LAYER, driver="GPKG", geometry_type="Unknown")
    # A plain DataFrame, so pyogrio writes a table with no geometry column.
    pyogrio.write_dataframe(_empty_table(DETECTOR_RUN_FIELDS), path, layer=DETECTOR_RUNS_LAYER, driver="GPKG")


# Zone COG bands written to the imagery file, in order, with their names.
S2_IMAGERY_BANDS = {1: "red", 2: "green", 3: "blue", 4: "nir"}  # B4, B3, B2, B8


def write_s2_imagery(cog_path, out_path):
    """The zone's enhanced Sentinel-2 red, green, blue and NIR as a 4-band
    uint8 GeoTIFF with overviews. A pixel with no data in any band is 0 in all."""
    with rasterio.open(cog_path) as src:
        bands = src.read(list(S2_IMAGERY_BANDS)).astype("float32")
        profile = {
            "driver": "GTiff", "width": src.width, "height": src.height, "count": len(S2_IMAGERY_BANDS),
            "dtype": "uint8", "nodata": 0, "crs": src.crs, "transform": src.transform,
            "tiled": True, "blockxsize": 256, "blockysize": 256, "compress": "deflate",
            # Not RGB: GDAL would then read a 4th band as alpha.
            "photometric": "MINISBLACK",
        }
    valid = np.isfinite(bands).all(axis=0)
    # 1..255 for data, so 0 stays free for no data.
    out = (1 + np.round(enhance_rgb(bands, RGBEnhanceConfig()) * 254)).astype("uint8")
    out[:, ~valid] = 0
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(out)
        dst.descriptions = tuple(S2_IMAGERY_BANDS.values())
        dst.build_overviews([2, 4, 8, 16], Resampling.average)
    return Path(out_path)



def write_overture_roads(cog_path, overture_path, out_path):
    """The second-source roads clipped to the zone, one 2D LineString per row
    with its ``road_class``, as a fresh GeoPackage at ``out_path``."""
    zone = load_zone_roads(cog_path, overture_path)
    roads = zone.roads
    if "road_class" not in roads:  # no roads in the zone
        roads = roads.assign(road_class=pd.Series([], dtype="string"))
    roads = roads[["road_class", roads.geometry.name]].explode(index_parts=False)
    roads = roads.set_geometry(shapely.force_2d(roads.geometry.values), crs=zone.crs)
    roads = roads[(roads.geom_type == "LineString") & (roads.length > 0)]
    out_path = Path(out_path)
    out_path.unlink(missing_ok=True)
    roads.to_file(out_path, layer=OVERTURE_LAYER, driver="GPKG", geometry_type="LineString")
    return out_path


def zone_cleaning_work(gpkg) -> list:
    """What cleaning work a zone GeoPackage holds, as short descriptions.

    Empty when the GeoPackage is as packaged, apart from pending detector
    output, which a detector re-run regenerates: ``pending`` flags and the
    ``proposed`` label rows they point at. A row with an ``annotator`` is not
    detector output, because QGIS stamps the annotator when someone saves the
    row. A file that is not a readable zone GeoPackage counts as cleaning
    work, so it is never replaced silently.
    """
    try:
        raw = gpd.read_file(gpkg, layer=RAW_LABELS_LAYER)
        labels = gpd.read_file(gpkg, layer=LABELS_LAYER, fid_as_index=True)
        tiles = gpd.read_file(gpkg, layer=TILES_LAYER)
        flags = pyogrio.read_dataframe(gpkg, layer=FLAGS_LAYER, read_geometry=False)
        pending = flags["decision"] == PENDING
        untouched = flags["annotator"].isna()
    except Exception as exc:  # noqa: BLE001 - any failure means "don't touch it"
        return [f"not a readable zone GeoPackage: {exc}"]

    work = []
    if not pending.all():
        work.append(f"{int((~pending).sum())} decided flag(s)")
    if not untouched.all():
        work.append(f"{int((~untouched).sum())} flag(s) an annotator saved")
    detector_flags = is_pending_detector_output(flags)
    pending_proposals = set(flags.loc[detector_flags, "proposal_fid"].dropna().astype(int))
    proposed = set(labels.index[(labels["status"] == "proposed") & labels["annotator"].isna()])
    labels = labels.drop(index=list(proposed & pending_proposals))
    if len(labels) != len(raw) or set(labels["raw_id"].dropna()) != set(raw["raw_id"]):
        work.append(f"labels has {len(labels)} rows for {len(raw)} raw rows")
    if (labels["status"] != "kept").any():
        work.append(f"{int((labels['status'] != 'kept').sum())} label(s) with a status other than kept")
    for col in ("defect", "annotator"):
        if labels[col].notna().any():
            work.append(f"labels with a {col}")
    if not work:
        raw_geom = dict(zip(raw["raw_id"], raw.geometry))
        moved = sum(not g.equals_exact(raw_geom[i], 0) for i, g in zip(labels["raw_id"], labels.geometry))
        if moved:
            work.append(f"{moved} label geometr{'y' if moved == 1 else 'ies'} changed")
    if tiles["reviewed"].astype(bool).any():
        work.append(f"{int(tiles['reviewed'].astype(bool).sum())} reviewed tile(s)")
    for col in ("note", "annotator"):
        if tiles[col].notna().any():
            work.append(f"tiles with a {col}")
    return work


def _back_up(gpkg) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = gpkg.with_name(f"{gpkg.name}.{stamp}.bak")
    gpkg.rename(backup)
    return backup


def is_open_elsewhere(gpkg) -> bool:
    """Whether another program (e.g. QGIS) has the GeoPackage open.

    QGIS opens a GeoPackage in WAL mode and the ``-wal`` file goes when it
    closes, so one present means the file is open (or QGIS crashed), possibly
    with changes not yet written into the GeoPackage itself.
    """
    return gpkg.with_name(gpkg.name + "-wal").exists()


def package_zone(
    zone: ZoneInputs, *, roads_path, dataset_dir, output_dir, overwrite=False, overture_path=None,
) -> ZoneFolder:
    """Write one zone's folder. Refuses (``ZoneHasCleaningWork``) to replace a zone
    GeoPackage that holds cleaning work unless ``overwrite``, which first moves
    the old file aside as ``<zone>.gpkg.<timestamp>.bak``. Never replaces one
    that another program has open (``ZoneIsOpen``)."""
    folder = ZoneFolder(Path(output_dir) / zone.zone_filename, zone.zone_filename)
    tile_status = DatasetPaths(dataset_dir).tile_status_path(zone.zone_filename)
    if not tile_status.exists():
        raise FileNotFoundError(
            f"No tile-status table for {zone.zone_filename} at {tile_status}; run `generate` first."
        )
    if folder.gpkg.exists():
        # Check before reading: GDAL removes the WAL files on close, even when
        # another program still uses them.
        if is_open_elsewhere(folder.gpkg):
            raise ZoneIsOpen(folder.gpkg)
        work = zone_cleaning_work(folder.gpkg)
        if work and not overwrite:
            raise ZoneHasCleaningWork(folder.gpkg, work)
        if work:
            print(f"Backed up {folder.gpkg.name} to {_back_up(folder.gpkg).name}")

    folder.root.mkdir(parents=True, exist_ok=True)
    raw = raw_labels(zone.cog_path, roads_path, baseline_excluded_classes(dataset_dir))
    # Build next to the target, then swap in, so a failed run leaves no half file.
    tmp = folder.gpkg.with_name(f".{folder.gpkg.name}.tmp")
    write_zone_gpkg(tmp, raw, tile_grid(tile_status))
    tmp.replace(folder.gpkg)
    write_s2_imagery(zone.cog_path, folder.s2_imagery)
    if overture_path is None:
        folder.overture.unlink(missing_ok=True)
    else:
        write_overture_roads(zone.cog_path, overture_path, folder.overture)
    with rasterio.open(zone.cog_path) as src:
        crs, extent = src.crs.to_string(), tuple(src.bounds)
    write_zone_project(
        folder.project, gpkg=folder.gpkg, s2_imagery=folder.s2_imagery,
        overture=folder.overture if overture_path is not None else None, crs=crs, extent=extent,
        road_columns=[c for c in raw.columns if c not in ("raw_id", "geometry")],
    )
    return folder
