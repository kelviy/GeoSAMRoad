import math
import ee


def utm_epsg(lon, lat):
    """Local UTM EPSG code for a WGS84 lon/lat."""
    zone = math.floor((lon + 180) / 6) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def create_rectangular_bbox(lon, lat, width_km=25, height_km=25):
    proj = ee.Projection(utm_epsg(lon, lat))
    centre = ee.Geometry.Point([lon, lat]).transform(proj, 1)

    x_offset = (width_km * 1000) / 2
    y_offset = (height_km * 1000) / 2

    coords = centre.coordinates()
    cx = ee.Number(coords.get(0))
    cy = ee.Number(coords.get(1))

    return ee.Geometry.Rectangle(
        [
            cx.subtract(x_offset),
            cy.subtract(y_offset),
            cx.add(x_offset),
            cy.add(y_offset),
        ],
        proj,
        False,
    )


def bbox_from_wgs84(west, south, east, north):
    return ee.Geometry.Rectangle([west, south, east, north], "EPSG:4326", False)


def planar_bounds(geometry, crs):
    ring = (
        geometry.bounds(1)
        .transform(ee.Projection(crs), 1)
        .coordinates()
        .get(0)
        .getInfo()
    )
    xs = [pt[0] for pt in ring]
    ys = [pt[1] for pt in ring]
    return min(xs), min(ys), max(xs), max(ys)
