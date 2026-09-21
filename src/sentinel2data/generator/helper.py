"""Shared raster / vector geometry helpers.
  - window bounds calculation
  - percentile contrast stretch
  - imagery directory scanning 
"""
from pathlib import Path
import numpy as np
from rasterio.warp import transform_bounds
from rasterio.windows import transform as window_transform
from shapely.geometry import box
from sentinel2data.generator.config import RGBEnhanceConfig

# image directory scanning
def scan_rasters(directory, skip_mask_suffix="_mask.tif"):
    """Return sorted source rasters in the given directory. 
    Skip macOS `._` dotfiles and existing `*_mask.tif`
    """
    
    exts = (".tif", ".tiff")
    
    directory = Path(directory)
    paths = []
    for ext in exts:
        paths.extend(directory.glob(f"*{ext}"))

    out = []
    for p in sorted(paths):
        if p.name.startswith("._"):
            continue
        if skip_mask_suffix and p.name.endswith(skip_mask_suffix):
            continue
        out.append(p)
    return out


# window helpers
def window_bounds(window, transform):
    """Map a rasterio Window to (minx, miny, maxx, maxy) in CRS units."""
    ptf = window_transform(window, transform)
    minx = ptf.c
    maxy = ptf.f
    maxx = minx + window.width * ptf.a
    miny = maxy + window.height * ptf.e
    return minx, miny, maxx, maxy


def window_box(window, transform):
    """Shapely box region of a window"""
    return box(*window_bounds(window, transform))


def reproject_bounds(bounds, src_crs, dst_crs):
    """Reproject (minx, miny, maxx, maxy) to (west, south, east, north)."""
    minx, miny, maxx, maxy = bounds
    return transform_bounds(src_crs, dst_crs, minx, miny, maxx, maxy)


# visualisation helper
def cummulative_stretch_to_rgb(band, percentile_range=(2, 98)):
    """Percentile contrast stretch of a single float band to 0-255. Pixels <= 0 are excluded from the percentiles"""
    valid = band[band > 0]
    if valid.size == 0:
        return np.zeros_like(band, dtype=np.uint8)

    lo, hi = np.percentile(valid, percentile_range)
    if hi <= lo:
        return np.zeros_like(band, dtype=np.uint8)

    stretched = np.clip(band, lo, hi)
    stretched = (stretched - lo) / (hi - lo)
    return (stretched * 255).astype(np.uint8)


def stretch_bands(img, percentile_range=(2, 98)):
    """Stretch (3, H, W) tiff to (3, H, W) rgb"""
    return np.stack(
        [cummulative_stretch_to_rgb(img[i], percentile_range) for i in range(img.shape[0])]
    )


# RGB enhancement (appended extra imagery bands)
def enhance_rgb(rgb, config=None):
    """CLAHE + gamma enhancement of rgb tiff image per band."""
    from skimage.exposure import adjust_gamma, equalize_adapthist, rescale_intensity

    if config is None:
        config = RGBEnhanceConfig()

    out = []
    for i in range(rgb.shape[0]):
        band = rgb[i].astype("float32")
        finite = np.isfinite(band)
        if not finite.any():  # skip
            out.append(np.zeros_like(band, dtype="float32"))
            continue
        low = float(band[finite].min())
        high = float(band[finite].max())
        if high <= low:  # skip
            out.append(np.zeros_like(band, dtype="float32"))
            continue
        # set nodata (NaN/inf) to low
        band = np.where(finite, band, low)
        norm = rescale_intensity(band, in_range=(low, high), out_range=(0.0, 1.0))
        gamma_enhance = adjust_gamma(norm, gamma=config.gamma)
        clahe_enhance = equalize_adapthist(
            gamma_enhance,
            kernel_size=config.clahe_kernel_size,
            clip_limit=config.clahe_clip_limit,
        )
        out.append(clahe_enhance.astype("float32"))
    return np.stack(out)