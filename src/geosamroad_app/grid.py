"""The tile grid the region selector draws -- and the one the fetcher cuts.

The whole point of the selector is that what you hover is what gets processed,
so this is not a display convenience: cells are fetched in **EPSG:4326**, which
makes one UI cell exactly one fetched tile, north-up, with no reprojection
anywhere between the map, the imagery and the road graph.

The trade-off of sampling in degrees is that a pixel is no longer square on the
ground: ``PIXEL_DEG`` is ~10 m north-south everywhere, but east-west it is
``10 * cos(latitude)`` m, so imagery is increasingly compressed horizontally
away from the equator. The model was trained on isotropic 10 m UTM, so this is
a deliberate accuracy-for-alignment trade.

Cells are anchored on the lon/lat origin, so a patch of ground always falls in
the same cell with the same id however the map is panned.
"""
import math

from globalurbanmapper_gee.bands import TILE_PX

# Degrees per pixel. 9e-5 deg of latitude is 9.95 m -- near enough to the 10 m
# the model was trained at that the north-south scale matches training.
PIXEL_DEG = 9e-5
# Ground size of one cell. Exactly one fetched tile.
TILE_DEG = TILE_PX * PIXEL_DEG          # 0.04608 deg, ~5.1 km north-south

MAX_TILES_PER_VIEW = 600
EPSG = 4326

_M_PER_DEG_LAT = 110574.0
_M_PER_DEG_LON = 111320.0


def tile_id(col, row):
    """Stable id for a cell, from its position on the global lon/lat grid."""
    return f"{col}-{row}"


def parse_tile_id(tid):
    """``tile_id`` inverse -> ``(col, row)``."""
    try:
        col, row = tid.split("-") if tid.count("-") == 1 else _split_signed(tid)
        return int(col), int(row)
    except (AttributeError, ValueError):
        raise ValueError(f"Malformed tile id {tid!r}, expected '<col>-<row>'")


def _split_signed(tid):
    """Split ids whose row is negative (southern hemisphere), e.g. '401--737'."""
    idx = tid.index("-", 1)
    return tid[:idx], tid[idx + 1:]


def tile_bounds(col, row):
    """``(west, south, east, north)`` of a cell, in degrees."""
    return (col * TILE_DEG, row * TILE_DEG,
            (col + 1) * TILE_DEG, (row + 1) * TILE_DEG)


def tile_bounds_wgs84(tid):
    """``(west, south, east, north)`` -- already WGS84, so just the bounds."""
    return tile_bounds(*parse_tile_id(tid))


def tile_image_corners(tid):
    """Cell corners as ``[top-left, top-right, bottom-right, bottom-left]``.

    The order MapLibre's ``image`` source expects. In EPSG:4326 the cell is a
    true north-up rectangle, so these are simply the bbox corners.
    """
    west, south, east, north = tile_bounds_wgs84(tid)
    return [[west, north], [east, north], [east, south], [west, south]]


def ground_resolution(lat):
    """Pixel size in metres at a latitude, as ``(east_west, north_south)``.

    Surfaced so the UI can be honest about the horizontal compression.
    """
    return (PIXEL_DEG * _M_PER_DEG_LON * math.cos(math.radians(lat)),
            PIXEL_DEG * _M_PER_DEG_LAT)


def tile_feature(col, row):
    """One cell as a GeoJSON Feature."""
    west, south, east, north = tile_bounds(col, row)
    tid = tile_id(col, row)
    return {
        "type": "Feature",
        "id": tid,
        "properties": {"tile_id": tid, "col": col, "row": row},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[west, south], [east, south], [east, north],
                             [west, north], [west, south]]],
        },
    }


def tiles_for_viewport(west, south, east, north):
    """Cells covering a WGS84 viewport, as a GeoJSON FeatureCollection.

    ``truncated: True`` means the view is too zoomed out to enumerate, which the
    frontend turns into a "zoom in" hint.
    """
    if east <= west or north <= south:
        raise ValueError(f"Empty viewport ({west}, {south}, {east}, {north})")

    col_lo, row_lo = math.floor(west / TILE_DEG), math.floor(south / TILE_DEG)
    col_hi, row_hi = math.floor(east / TILE_DEG), math.floor(north / TILE_DEG)
    n = (col_hi - col_lo + 1) * (row_hi - row_lo + 1)
    if n > MAX_TILES_PER_VIEW:
        return {"type": "FeatureCollection", "features": [], "truncated": True,
                "tile_count": n, "epsg": EPSG}

    features = [tile_feature(col, row)
                for row in range(row_lo, row_hi + 1)
                for col in range(col_lo, col_hi + 1)]
    return {"type": "FeatureCollection", "features": features, "truncated": False,
            "tile_count": len(features), "epsg": EPSG}
