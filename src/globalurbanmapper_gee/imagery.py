import ee

SENTINEL_1 = "COPERNICUS/S1_GRD"
SENTINEL_2_SR = "COPERNICUS/S2_SR_HARMONIZED"
S2_CLOUD_PROBABILITY = "COPERNICUS/S2_CLOUD_PROBABILITY"

# s2cloudless thresholds
CLOUD_FILTER = 90       # max scene CLOUDY_PIXEL_PERCENTAGE
CLD_PRB_THRESH = 50     # s2cloudless probability
NIR_DRK_THRESH = 0.15   # shadow threshold from NIR 
CLD_PRJ_DIST = 1        # shadow projection distance, in units of 10 m
BUFFER = 50             # cloud-shadow mask dilation, m


def _get_s2_sr_cld_col(aoi, start_date, end_date, cloud_filter=CLOUD_FILTER):
    """S2 SR scenes with the matching s2cloudless probability image joined on."""
    s2_sr_col = (
        ee.ImageCollection(SENTINEL_2_SR)
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", cloud_filter))
    )
    s2_cloudless_col = (
        ee.ImageCollection(S2_CLOUD_PROBABILITY)
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
    )
    return ee.ImageCollection(
        ee.Join.saveFirst("s2cloudless").apply(
            primary=s2_sr_col,
            secondary=s2_cloudless_col,
            condition=ee.Filter.equals(
                leftField="system:index", rightField="system:index"
            ),
        )
    )


def _add_cloud_bands(img):
    cld_prb = ee.Image(img.get("s2cloudless")).select("probability")
    is_cloud = cld_prb.gt(CLD_PRB_THRESH).rename("clouds")
    return img.addBands(ee.Image([cld_prb, is_cloud]))


def _add_shadow_bands(img):
    # SCL class 6 is water; only non-water dark pixels can be cloud shadow.
    not_water = img.select("SCL").neq(6)
    sr_band_scale = 1e4
    dark_pixels = (
        img.select("B8")
        .lt(NIR_DRK_THRESH * sr_band_scale)
        .multiply(not_water)
        .rename("dark_pixels")
    )
    # Shadows fall opposite the sun; assumes a UTM (north-up) projection.
    shadow_azimuth = ee.Number(90).subtract(
        ee.Number(img.get("MEAN_SOLAR_AZIMUTH_ANGLE"))
    )
    cld_proj = (
        img.select("clouds")
        .directionalDistanceTransform(shadow_azimuth, CLD_PRJ_DIST * 10)
        .reproject(crs=img.select(0).projection(), scale=100)
        .select("distance")
        .mask()
        .rename("cloud_transform")
    )
    shadows = cld_proj.multiply(dark_pixels).rename("shadows")
    return img.addBands(ee.Image([dark_pixels, cld_proj, shadows]))


def _add_cloud_shadow_mask(img):
    img_cloud_shadow = _add_shadow_bands(_add_cloud_bands(img))
    is_cld_shdw = (
        img_cloud_shadow.select("clouds").add(img_cloud_shadow.select("shadows")).gt(0)
    )
    is_cld_shdw = (
        is_cld_shdw.focalMin(2)
        .focalMax(BUFFER * 2 / 20)
        .reproject(crs=img.select([0]).projection(), scale=20)
        .rename("cloudmask")
    )
    return img_cloud_shadow.addBands(is_cld_shdw)


def _apply_cloud_shadow_mask(img):
    not_cld_shdw = img.select("cloudmask").Not()
    return img.select("B.*").updateMask(not_cld_shdw)


def gen_sentinel2_data(geo_bounds, date, bands):
    bands = list(bands)
    s2_sr_cld_col = _get_s2_sr_cld_col(geo_bounds, date[0], date[1])
    s2_sr_col = (
        s2_sr_cld_col.map(_add_cloud_shadow_mask).map(_apply_cloud_shadow_mask).select(bands)
    )
    fallback = ee.ImageCollection(
        [ee.Image.constant([0] * len(bands)).rename(bands)]
    )
    return ee.ImageCollection(
        ee.Algorithms.If(s2_sr_col.size(), s2_sr_col, fallback)
    )


def _handle_empty_collection(collection, ascending):
    suffix = "_ascending" if ascending else "_descending"
    names = ["VV" + suffix, "VH" + suffix]
    empty = ee.Image.constant([0, 0]).rename(names)
    return ee.ImageCollection(
        ee.Algorithms.If(
            collection.size(),
            collection.map(lambda img: img.rename(names)),
            ee.ImageCollection([empty]),
        )
    )


def gen_sentinel1_data(geo_bounds, date, crs):
    filtered = (
        ee.ImageCollection(SENTINEL_1)
        .filterBounds(geo_bounds)
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .filterDate(date[0], date[1])
        .select(["VV", "VH"])
    )
    iw = filtered.filter(ee.Filter.eq("instrumentMode", "IW")).filter(
        ee.Filter.eq("resolution_meters", 10)
    )
    ew = (
        filtered.filter(ee.Filter.eq("instrumentMode", "EW"))
        .filter(ee.Filter.eq("resolution_meters", 40))
        .map(lambda img: img.resample("bilinear").reproject(crs=crs, scale=10))
    )
    final = ee.ImageCollection(
        ee.Algorithms.If(
            filtered.size(),
            ee.Algorithms.If(iw.size(), iw, ew),
            ee.ImageCollection([ee.Image.constant([0, 0]).rename(["VV", "VH"])]),
        )
    )
    ascending = _handle_empty_collection(
        final.filter(ee.Filter.eq("orbitProperties_pass", "ASCENDING")), True
    )
    descending = _handle_empty_collection(
        final.filter(ee.Filter.eq("orbitProperties_pass", "DESCENDING")), False
    )
    return [ascending, descending]
