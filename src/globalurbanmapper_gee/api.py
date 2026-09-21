import logging

from globalurbanmapper_gee import auth, geometry, io, stack
from globalurbanmapper_gee.fetch import SCALE_M, TILE_PX
from globalurbanmapper_gee.fetch import fetch_region as fetch_region_array

log = logging.getLogger(__name__)


def build_region_image(
    lon=None,
    lat=None,
    width_km=25,
    height_km=25,
    bbox=None,
    start_date="2020-01-01",
    end_date="2021-01-01",
    band_list="s1s2",
    initialize=True,
):
    if initialize:
        auth.initialize()

    if bbox is not None:
        aoi = geometry.bbox_from_wgs84(*bbox)
    elif lon is not None and lat is not None:
        aoi = geometry.create_rectangular_bbox(lon, lat, width_km, height_km)
    else:
        raise ValueError("Provide either lon/lat or bbox")

    image, crs = stack.build_rosa_image(aoi, start_date, end_date, band_list=band_list)
    return image, crs, aoi


def fetch_region(
    lon=None,
    lat=None,
    width_km=25,
    height_km=25,
    bbox=None,
    start_date="2020-01-01",
    end_date="2021-01-01",
    band_list="s1s2",
    out_path=None,
    out_dir=None,
    scale=SCALE_M,
    tile_px=TILE_PX,
    initialize=True,
):
    """Fetch satellite imagery matching RGB/S1S2 for the region

    Output Arguments:
      - out_path: a single COG over the whole AOI.
      - out_dir: one 512 px COG per tile and metadata.csv.
      - neither: return the pixels as a (C, H, W) float32 array.

    Returns:
        dict describing the written COG, the list of tile rows, or the array.
    """
    image, crs, aoi = build_region_image(
        lon=lon, lat=lat, width_km=width_km, height_km=height_km, bbox=bbox,
        start_date=start_date, end_date=end_date, band_list=band_list,
        initialize=initialize,
    )
    bounds = geometry.planar_bounds(aoi, crs)
    log.info("AOI %s in %s", bounds, crs)

    if out_path and out_dir:
        raise ValueError("Pass out_path or out_dir, not both")
    if out_path:
        return io.write_zone_cog(
            out_path, image, crs, bounds, band_list=band_list, scale=scale,
            tile_px=tile_px,
        )
    if out_dir:
        return io.write_tiles(
            out_dir, image, crs, bounds, band_list=band_list, scale=scale,
            tile_px=tile_px,
        )
    return fetch_region_array(
        image, crs, bounds, band_list=band_list, scale=scale, tile_px=tile_px,
    )
