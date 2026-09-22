"""The tile grid the region selector draws -- and the one the fetcher cuts.

The whole point of the selector is that what you hover is what gets processed,
so the grid here is not a display convenience: it is the same grid
``globalurbanmapper_gee.fetch.region_grid`` produces. One UI cell is exactly one
fetched tile.

A cell is ``TILE_PX * SCALE_M`` = 512 px x 10 m = **5.12 km** square, anchored on
the UTM origin rather than on the viewport, so a given patch of ground always
falls in the same cell with the same id no matter how the map is panned.
"""
import math

from rasterio.crs import CRS
from rasterio.warp import transform, transform_bounds

from globalurbanmapper_gee.bands import SCALE_M, TILE_PX

WGS84 = CRS.from_epsg(4326)

# Ground size of one cell, in metres. Matches one fetched tile exactly.
TILE_M = TILE_PX * SCALE_M

# A whole-world request would be millions of cells; cap what a viewport returns
# so a zoomed-out map degrades to "zoom in to pick tiles" instead of hanging.
MAX_TILES_PER_VIEW = 600


def utm_epsg(lon, lat):
    """Local UTM EPSG code for a WGS84 lon/lat (same rule as the fetcher)."""
    zone = math.floor((lon + 180) / 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _cell_indices(x, y):
    """Planar metres -> (col, row) of the containing cell."""
    return math.floor(x / TILE_M), math.floor(y / TILE_M)


def tile_id(epsg, col, row):
    """Stable id for a cell. Encodes the zone, so ids never collide."""
    return f"{epsg}-{col}-{row}"


def parse_tile_id(tid):
    """``tile_id`` inverse -> ``(epsg, col, row)``."""
    try:
        epsg, col, row = tid.split("-")
        return int(epsg), int(col), int(row)
    except (AttributeError, ValueError):
        raise ValueError(f"Malformed tile id {tid!r}, expected '<epsg>-<col>-<row>'")


def tile_bounds_utm(col, row):
    """Planar ``(min_x, min_y, max_x, max_y)`` of a cell, in its own UTM CRS.

    Always a multiple of ``TILE_M``, hence of ``SCALE_M`` -- so ``region_grid``
    over these bounds snaps to itself and yields exactly one 512 px tile.
    """
    return (col * TILE_M, row * TILE_M, (col + 1) * TILE_M, (row + 1) * TILE_M)


def tile_bounds_wgs84(tid):
    """``(west, south, east, north)`` of a cell, for handing to the fetcher."""
    epsg, col, row = parse_tile_id(tid)
    return transform_bounds(CRS.from_epsg(epsg), WGS84, *tile_bounds_utm(col, row))


def _ring_wgs84(epsg, col, row):
    """Cell outline as a WGS84 GeoJSON ring.

    The four corners are transformed individually rather than from a bbox: a UTM
    square is not a lon/lat rectangle, and drawing it as one visibly skews the
    grid away from the imagery at high latitudes.
    """
    min_x, min_y, max_x, max_y = tile_bounds_utm(col, row)
    xs = [min_x, max_x, max_x, min_x, min_x]
    ys = [min_y, min_y, max_y, max_y, min_y]
    lons, lats = transform(CRS.from_epsg(epsg), WGS84, xs, ys)
    return [[lon, lat] for lon, lat in zip(lons, lats)]


def tile_image_corners(tid):
    """Cell corners as ``[top-left, top-right, bottom-right, bottom-left]``.

    The order MapLibre's ``image`` source expects, which is *not* the order the
    GeoJSON ring uses (that starts bottom-left and runs anticlockwise). Passing
    the ring's points straight through renders the overlay rotated 180 degrees.

    Four explicit corners rather than a bbox, because a UTM square is not a
    lon/lat rectangle -- the image has to be pinned to the same quad the grid
    cell is drawn on.
    """
    epsg, col, row = parse_tile_id(tid)
    min_x, min_y, max_x, max_y = tile_bounds_utm(col, row)
    xs = [min_x, max_x, max_x, min_x]   # TL, TR, BR, BL
    ys = [max_y, max_y, min_y, min_y]
    lons, lats = transform(CRS.from_epsg(epsg), WGS84, xs, ys)
    return [[lon, lat] for lon, lat in zip(lons, lats)]


def tile_feature(epsg, col, row):
    """One cell as a GeoJSON Feature."""
    tid = tile_id(epsg, col, row)
    return {
        "type": "Feature",
        "id": tid,
        "properties": {"tile_id": tid, "epsg": epsg, "col": col, "row": row,
                       "km": round(TILE_M / 1000, 2)},
        "geometry": {"type": "Polygon", "coordinates": [_ring_wgs84(epsg, col, row)]},
    }


def tiles_for_viewport(west, south, east, north):
    """Cells covering a WGS84 viewport, as a GeoJSON FeatureCollection.

    The viewport is projected into the UTM zone of its own centre, so a view
    spanning a zone boundary is gridded in one consistent zone rather than
    stitching two. Returns ``truncated: True`` when the view is too zoomed out
    to enumerate, which the frontend turns into a "zoom in" hint.
    """
    if east <= west or north <= south:
        raise ValueError(f"Empty viewport ({west}, {south}, {east}, {north})")

    epsg = utm_epsg((west + east) / 2, (south + north) / 2)
    crs = CRS.from_epsg(epsg)
    min_x, min_y, max_x, max_y = transform_bounds(WGS84, crs, west, south, east, north)

    col_lo, row_lo = _cell_indices(min_x, min_y)
    col_hi, row_hi = _cell_indices(max_x, max_y)
    n = (col_hi - col_lo + 1) * (row_hi - row_lo + 1)
    if n > MAX_TILES_PER_VIEW:
        return {"type": "FeatureCollection", "features": [], "truncated": True,
                "tile_count": n, "epsg": epsg}

    features = [
        tile_feature(epsg, col, row)
        for row in range(row_lo, row_hi + 1)
        for col in range(col_lo, col_hi + 1)
    ]
    return {"type": "FeatureCollection", "features": features, "truncated": False,
            "tile_count": len(features), "epsg": epsg}
