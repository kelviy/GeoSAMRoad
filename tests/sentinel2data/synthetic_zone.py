"""A tiny synthetic zone for testing the ROSA generator and label cleaning.

``build_synthetic_zone(root)`` writes, under ``root``:

* ``imagery/{train,val,test}/<zone>.tif``: 20-band float32 COGs in UTM 35S
  (EPSG:32735) at 10 m. This is the pre-split layout ``generate`` expects.
* ``roads.parquet``: a WGS84 GeoParquet with the same columns as the cleaned
  CDNGI road layer that the ``roads`` command writes.

The train zone is ``TILE_SIZE`` px tiles in a 2x2 grid, plus a partial
``EDGE_STRIP`` px strip on the east and south edges that the tiler drops. In
pixel coordinates (col, row), with r/c the tile row and column:

    +---------------+---------------+-----+
    | r0_c0         | r0_c1         |strip|
    |  gap          |  near_dup     | road|
    |  dangling_stub|  unnoded_x    |     |
    |  zone_edge ^  |               |     |
    +---------------+---------------+     |
    | r1_c0         | r1_c1         |     |
    |  carriageways |  (no roads:   |     |
    |  track        |   dropped)    |     |
    |  slipway      |               |     |
    +---------------+---------------+-----+
    |                strip                |
    +-------------------------------------+

The val and test zones are one tile each, with one plain road, and sit well
away from the train zone so their roads never overlap.

Adding a synthetic case
-----------------------
1. Pick a tile in the train zone that has room. Don't use ``EMPTY_TILE``: it
   must stay road-free. Keep roads at least about 2 px (20 m) inside tile
   edges unless the case is about edges, because masks are buffered and
   rasterised with ``all_touched``.
2. Add a ``CaseSpec`` to ``CASES`` with a unique ``name``, the tile, a
   one-line description, and its roads as ``(CDNGI FEAT_TYPE, [(col, row),
   ...])`` in zone pixel coordinates. One pixel is 10 m.
3. Tests look the case up with ``zone.cases["<name>"]`` to get its tile, its
   road row ids in ``roads.parquet``, and its roads in zone CRS metres.
4. If the case adds or removes a kept tile, update ``EXPECTED_TILES`` in
   ``test_generate.py``.
"""
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString

from sentinel2data.generator.config import CDNGI_ROAD_CLASSIFICATION, WGS84, flatten_classification

CRS = "EPSG:32735"  # UTM 35S, covers the Eastern Cape
RESOLUTION = 10.0  # metres per pixel
BAND_COUNT = 20  # source bands; the generator appends 3 enhanced-RGB bands
TILE_SIZE = 128  # small tiles keep the suite fast
PATCH_SIZE = 64  # internal COG block size; must divide TILE_SIZE
EDGE_STRIP = 40  # px; narrower than a tile, so never tiled
EMPTY_TILE = "r1_c1"

TRAIN_ZONE = "Synthetic_AlbanyThicket_-33p90_25p60_Rural"
VAL_ZONE = "SyntheticVal_Fynbos_-33p95_25p65_Urban"
TEST_ZONE = "SyntheticTest_Savanna_-33p85_25p55_PeriUrban"

# Top-left corners (UTM metres). Val and test sit 5 km and 10 km east.
TRAIN_ORIGIN = (370_000.0, 6_250_000.0)
VAL_ORIGIN = (375_000.0, 6_250_000.0)
TEST_ORIGIN = (380_000.0, 6_250_000.0)

_SCALE, _BUFFER, _ = flatten_classification(CDNGI_ROAD_CLASSIFICATION)


@dataclass(frozen=True)
class CaseSpec:
    name: str
    tile: str
    description: str
    roads: list  # [(road_class, [(col, row), ...]), ...] in zone pixels


CASES = [
    CaseSpec(
        "gap", "r0_c0", "Two Street ends 20 m apart on one line",
        [("Street", [(10, 30), (60, 30)]), ("Street", [(62, 30), (110, 30)])],
    ),
    CaseSpec(
        "dangling_stub", "r0_c0", "A 25 m Street stub hanging off an Other Road vertex",
        [("Other Road", [(10, 80), (60, 80), (110, 80)]), ("Street", [(60, 80), (60, 82.5)])],
    ),
    CaseSpec(
        "zone_edge", "r0_c0", "A Secondary Road running 200 m past the north zone edge",
        [("Secondary Road", [(120, 50), (120, -20)])],
    ),
    CaseSpec(
        "near_duplicate", "r0_c1", "Two Streets tracing the same line within 1 m",
        [("Street", [(140, 30), (240, 30)]), ("Street", [(140, 30.1), (240, 30.05)])],
    ),
    CaseSpec(
        "unnoded_crossing", "r0_c1", "Two Other Roads crossing with no shared vertex",
        [("Other Road", [(150, 60), (230, 110)]), ("Other Road", [(150, 110), (230, 60)])],
    ),
    CaseSpec(
        "parallel_carriageways", "r1_c0", "Two National Freeway carriageways 20 m apart, opposite directions",
        [("National Freeway", [(10, 150), (118, 150)]), ("National Freeway", [(118, 152), (10, 152)])],
    ),
    CaseSpec("track", "r1_c0", "A Track segment", [("Track", [(20, 190), (100, 190)])]),
    CaseSpec("slipway", "r1_c0", "A Slipway segment", [("Slipway", [(20, 220), (100, 230)])]),
    CaseSpec(
        "edge_strip", "strip", "A Street inside the partial east edge strip",
        [("Street", [(276, 20), (276, 100)])],
    ),
]

# One plain road per single-tile val/test zone, in that zone's pixels.
_SINGLE_TILE_ROAD = ("Street", [(10, 10), (118, 118)])


@dataclass(frozen=True)
class SyntheticCase:
    name: str
    tile: str  # e.g. "r0_c0", or "strip" for the dropped edge strip
    description: str
    road_ids: tuple  # row positions in roads.parquet
    geometries: tuple  # LineStrings in zone CRS metres


@dataclass(frozen=True)
class SyntheticZone:
    root: Path
    imagery_dir: Path
    roads_path: Path
    zone_stem: str  # train zone filename stem; tile stems are f"{zone_stem}_r{r}_c{c}"
    zone_path: Path
    crs: str
    tile_size: int
    patch_size: int
    cases: dict = field(default_factory=dict)  # name -> SyntheticCase

    def tile_stem(self, tile: str) -> str:
        return f"{self.zone_stem}_{tile}"

    def read_roads(self) -> gpd.GeoDataFrame:
        return gpd.read_parquet(self.roads_path)


def _to_metres(origin, pixels):
    x0, y0 = origin
    return LineString([(x0 + c * RESOLUTION, y0 - r * RESOLUTION) for c, r in pixels])


def _write_zone_cog(path, origin, size_px, seed):
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    data = rng.uniform(0.0, 1.0, size=(BAND_COUNT, size_px, size_px)).astype("float32")
    profile = dict(
        driver="COG",
        width=size_px,
        height=size_px,
        count=BAND_COUNT,
        dtype="float32",
        crs=CRS,
        transform=from_origin(*origin, RESOLUTION, RESOLUTION),
        blocksize=PATCH_SIZE,
        compress="deflate",
    )
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
    return path


def _road_row(road_class, geometry):
    return {
        "data_source": "cdngi",
        "road_class": road_class,
        "scale_class": _SCALE[road_class],
        "buffer": _BUFFER[road_class],
        "surface": "unknown",
        "geometry": geometry,
    }


def build_synthetic_zone(root) -> SyntheticZone:
    """Write the synthetic imagery and roads under ``root`` and describe them."""
    root = Path(root)
    imagery_dir = root / "imagery"
    train_size = 2 * TILE_SIZE + EDGE_STRIP

    zone_path = _write_zone_cog(imagery_dir / "train" / f"{TRAIN_ZONE}.tif", TRAIN_ORIGIN, train_size, seed=0)
    _write_zone_cog(imagery_dir / "val" / f"{VAL_ZONE}.tif", VAL_ORIGIN, TILE_SIZE, seed=1)
    _write_zone_cog(imagery_dir / "test" / f"{TEST_ZONE}.tif", TEST_ORIGIN, TILE_SIZE, seed=2)

    rows, cases = [], {}
    for spec in CASES:
        geoms = tuple(_to_metres(TRAIN_ORIGIN, pixels) for _, pixels in spec.roads)
        ids = tuple(range(len(rows), len(rows) + len(geoms)))
        rows.extend(_road_row(cls, g) for (cls, _), g in zip(spec.roads, geoms))
        cases[spec.name] = SyntheticCase(spec.name, spec.tile, spec.description, ids, geoms)

    road_class, pixels = _SINGLE_TILE_ROAD
    for origin in (VAL_ORIGIN, TEST_ORIGIN):
        rows.append(_road_row(road_class, _to_metres(origin, pixels)))

    roads = gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS).to_crs(WGS84)
    roads_path = root / "roads.parquet"
    roads.to_parquet(roads_path, write_covering_bbox=True)

    return SyntheticZone(
        root=root,
        imagery_dir=imagery_dir,
        roads_path=roads_path,
        zone_stem=TRAIN_ZONE,
        zone_path=zone_path,
        crs=CRS,
        tile_size=TILE_SIZE,
        patch_size=PATCH_SIZE,
        cases=cases,
    )
