import logging
import math
import time

import numpy as np

from globalurbanmapper_gee.bands import (
    N_SOURCE_BANDS,
    source_band_names,
    source_bands,
    written_band_names,
)
from globalurbanmapper_gee.enhance import append_enhanced_rgb

log = logging.getLogger(__name__)

TILE_PX = 512      # tile size
SCALE_M = 10       # spatial resolution  
MAX_RETRIES = 5
BACKOFF_BASE_S = 2.0


def region_grid(bounds, scale=SCALE_M, tile_px=TILE_PX):
    min_x, min_y, max_x, max_y = bounds
    origin_x = math.floor(min_x / scale) * scale
    origin_y = math.ceil(max_y / scale) * scale  # top edge

    n_cols = int(math.ceil((max_x - origin_x) / scale))
    n_rows = int(math.ceil((origin_y - min_y) / scale))

    tiles = []
    for row in range(0, n_rows, tile_px):
        for col in range(0, n_cols, tile_px):
            width = min(tile_px, n_cols - col)
            height = min(tile_px, n_rows - row)
            tiles.append(
                (
                    col,
                    row,
                    origin_x + col * scale,
                    origin_y - row * scale,
                    width,
                    height,
                )
            )
    return origin_x, origin_y, n_cols, n_rows, tiles


def _compute_pixels(image, crs, x0, y0, width, height, scale, band_names):
    import ee

    request = {
        "expression": image,
        "fileFormat": "NUMPY_NDARRAY",
        "grid": {
            "dimensions": {"width": int(width), "height": int(height)},
            "affineTransform": {
                "scaleX": scale,
                "shearX": 0,
                "translateX": x0,
                "shearY": 0,
                "scaleY": -scale, # negative as rows ordered north to south.
                "translateY": y0,
            },
            "crsCode": crs,
        },
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            arr = ee.data.computePixels(request)
            break
        except ee.ee_exception.EEException as exc:
            last_error = exc
            wait = BACKOFF_BASE_S * (2**attempt)
            log.warning(
                "computePixels failed (attempt %d/%d), retrying in %.0fs: %s",
                attempt + 1,
                MAX_RETRIES,
                wait,
                exc,
            )
            time.sleep(wait)
    else:
        raise RuntimeError(
            f"computePixels failed after {MAX_RETRIES} attempts"
        ) from last_error

    tile = np.stack([arr[name].astype("float32") for name in band_names])
    return np.nan_to_num(tile, nan=0.0, posinf=0.0, neginf=0.0)


def fetch_tiles(image, crs, bounds, band_list="s1s2", scale=SCALE_M, tile_px=TILE_PX):
    src_idx = [b - 1 for b in source_bands(band_list)]
    src_names = source_band_names(band_list)

    _, _, _, _, tiles = region_grid(bounds, scale=scale, tile_px=tile_px)
    for i, (col, row, x0, y0, width, height) in enumerate(tiles):
        log.info("Fetching tile %d/%d at col=%d row=%d", i + 1, len(tiles), col, row)
        fetched = _compute_pixels(
            image, crs, x0, y0, width, height, scale, src_names
        )
        tile = np.zeros((N_SOURCE_BANDS, *fetched.shape[1:]), dtype="float32") # pad 0 to bands not utilised
        tile[src_idx] = fetched
        yield col, row, width, height, append_enhanced_rgb(tile)


def fetch_region(image, crs, bounds, band_list="s1s2", scale=SCALE_M, tile_px=TILE_PX):
    _, _, n_cols, n_rows, _ = region_grid(bounds, scale=scale, tile_px=tile_px)
    out = np.zeros((len(written_band_names()), n_rows, n_cols), dtype="float32")
    for col, row, width, height, tile in fetch_tiles(
        image, crs, bounds, band_list=band_list, scale=scale, tile_px=tile_px,
    ):
        out[:, row : row + height, col : col + width] = tile
    return out
