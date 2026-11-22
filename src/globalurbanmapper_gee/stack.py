import ee

from globalurbanmapper_gee import imagery
from globalurbanmapper_gee.bands import (
    S1_BANDS,
    S2_BANDS,
    S2_EE_BANDS,
    source_band_names,
    source_bands,
)


def resolve_crs(s2_coll, fallback_geometry=None):
    # Band 0 is B4
    crs = s2_coll.first().select([0]).projection().crs().getInfo()
    if crs in (None, "EPSG:4326") and fallback_geometry is not None:
        crs = fallback_geometry.projection().crs().getInfo()
    return crs


def build_rosa_image(geometry, start_date, end_date, band_list="s1s2", crs=None):
    """``crs`` forces the sampling CRS; by default the S2 granule's own is used.

    Passing ``"EPSG:4326"`` samples in degrees, which keeps the tile north-up in
    lon/lat (nothing to reproject downstream) at the cost of pixels that are no
    longer square on the ground away from the equator.
    """
    src = source_bands(band_list)
    bbox = geometry.bounds(1)
    date = [start_date, end_date]

    s2_idx = [b for b in src if b <= len(S2_BANDS)]
    s2_coll = imagery.gen_sentinel2_data(
        bbox, date, [S2_EE_BANDS[b - 1] for b in s2_idx]
    )
    crs = crs or resolve_crs(s2_coll, bbox)

    # S2 SR is scaled by 10000
    s2 = s2_coll.median().divide(10000).toFloat()
    stack = s2.rename([S2_BANDS[b - 1] for b in s2_idx])

    if any(b > len(S2_BANDS) for b in src):
        ascending, descending = imagery.gen_sentinel1_data(bbox, date, crs)
        s1 = ascending.mean().addBands(descending.mean()).toFloat()
        stack = stack.addBands(s1.rename(list(S1_BANDS)))

    return stack.select(source_band_names(band_list)).toFloat(), crs
