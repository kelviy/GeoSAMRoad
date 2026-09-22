"""Run GeoSAMRoad over a fetched tile and return the road graph in WGS84.

Reuses the tested core (``inferencer.infer_one_tile``) rather than the CLI
``inferencer.main``, which is split-CSV oriented.

The output is GeoJSON, not a raster overlay: the model already produces a graph,
so handing the map real LineStrings keeps the roads crisp at every zoom, lets
the frontend style them, and avoids reprojecting imagery. A true-colour PNG of
the tile is written alongside so the UI can show what the model actually saw.
"""
import glob
import logging
import os

import numpy as np
import rasterio
import torch
from rasterio.crs import CRS
from rasterio.warp import transform
from rasterio.windows import Window

from geosamroad.dataset.bands import RGB, resolve_input
from geosamroad.dataset.helper import read_upsampled_window, read_window
from geosamroad.inferencer import NATIVE_TILE_PX, infer_one_tile, patch_offsets
from geosamroad.models.model import GeoSAMRoad
from geosamroad.utils import finalize_config, load_config
from geosamroad_app.variants import get_variant

log = logging.getLogger(__name__)

WGS84 = CRS.from_epsg(4326)

# Inference geometry, pinned here so an upstream config change cannot silently
# alter the tiling. up_size = 512 * 2 = 1024; PATCH_SIZE = CROP_SIZE * UPSCALE.
UPSCALE = 2
INFER_PATCHES_PER_EDGE = 16

# Patches per forward pass. The shipped configs use 16, which is tuned for the
# training cluster: sgcn's decoder concatenates skip connections and needs well
# over 6 GB at that batch size. 4 keeps the heaviest variant inside a 6 GB card
# for a negligible speed cost, since the fetch dominates wall-clock anyway.
# Override with GEOSAMROAD_INFER_BATCH on a larger GPU.
INFER_BATCH_SIZE = int(os.getenv("GEOSAMROAD_INFER_BATCH", "4"))

# infer_one_tile caches the encoder features of every patch for its second
# pass. On the GPU that is ~5 GB for a 16x16 grid and dwarfs the batch itself,
# which is why shrinking INFER_BATCH_SIZE alone does not stop an OOM. Parking
# them in host RAM is what makes a 6 GB card viable.
CACHE_FEATURES_ON_CPU = os.getenv("GEOSAMROAD_CACHE_FEATURES_CPU", "1") == "1"

# True-colour preview from raw S2 reflectance: a FIXED stretch, so adjacent
# tiles stay colour-consistent instead of each normalising to its own range.
TRUE_COLOR_MAX_REFL = 0.30
TRUE_COLOR_GAMMA = 0.8


def pick_device(name="auto"):
    """Resolve the torch device (auto -> cuda when present)."""
    if name != "auto":
        return torch.device(name)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _find_checkpoint(directory):
    ckpts = sorted(glob.glob(os.path.join(directory, "*.ckpt")))
    if not ckpts:
        raise FileNotFoundError(f"No GeoSAMRoad checkpoint (*.ckpt) in {directory}")
    if len(ckpts) > 1:
        log.warning("Multiple checkpoints in %s; using %s", directory, ckpts[0])
    return ckpts[0]


def load_model(checkpoint_root, variant_name, dataset_dir=".", device=None):
    """Build the model for a variant and load its weights.

    Returns:
        ``(net, config, bands, device)`` -- ``bands`` are the 1-based GeoTIFF
        band indices this encoder reads.
    """
    device = device or pick_device()
    variant = get_variant(variant_name)
    config = load_config(str(variant["config_path"]))

    config.DATASET_DIR = dataset_dir      # finalize_config demands a real dir
    config.PRETRAINED = False             # the ckpt carries every weight
    config.UPSCALE = UPSCALE
    config.INFER_PATCHES_PER_EDGE = INFER_PATCHES_PER_EDGE
    config.INFER_BATCH_SIZE = INFER_BATCH_SIZE
    config.INFER_CACHE_FEATURES_CPU = CACHE_FEATURES_ON_CPU
    finalize_config(config)
    patch_offsets(NATIVE_TILE_PX * UPSCALE, int(config.PATCH_SIZE),
                  INFER_PATCHES_PER_EDGE)   # fail now, not mid-tile

    bands = list(resolve_input(str(config.ENCODER_MODEL), bool(config.RGB_INPUT),
                               config.get("RGB_SOURCE") or "enhanced"))

    checkpoint = _find_checkpoint(os.path.join(checkpoint_root, variant_name))
    log.info("Loading %s (encoder=%s, bands=%s)", checkpoint, config.ENCODER_MODEL, bands)
    net = GeoSAMRoad(config)
    state = torch.load(checkpoint, map_location="cpu", weights_only=False)
    # strict=False tolerates the mask_criterion.* loss buffers the checkpoints
    # carry, but every model parameter must still be present.
    result = net.load_state_dict(state["state_dict"], strict=False)
    if result.missing_keys:
        raise RuntimeError(
            f"{checkpoint} is missing {len(result.missing_keys)} model weights, "
            f"first few: {result.missing_keys[:5]}"
        )
    net.eval().to(device)
    return net, config, bands, device


def _pixels_to_wgs84(nodes_rc, up_size, src_crs, bounds):
    """(row, col) at ``up_size`` -> (lon, lat), via the tile's own georeferencing.

    Using the GeoTIFF's real CRS and bounds rather than the planning grid means
    the graph lands exactly on the imagery even when the S2 granule's CRS
    differs from the UTM zone the selector drew.
    """
    if not len(nodes_rc):
        return np.zeros((0, 2))
    left, bottom, right, top = bounds
    xs = left + (nodes_rc[:, 1] / up_size) * (right - left)
    ys = top - (nodes_rc[:, 0] / up_size) * (top - bottom)
    lons, lats = transform(src_crs, WGS84, list(xs), list(ys))
    return np.column_stack([lons, lats])


def _true_colour(raw):
    """(3, H, W) raw reflectance -> (H, W, 3) uint8 RGB."""
    stretched = np.clip(raw / TRUE_COLOR_MAX_REFL, 0.0, 1.0) ** TRUE_COLOR_GAMMA
    return (stretched.transpose(1, 2, 0) * 255).astype(np.uint8)


def infer_tile(net, config, bands, device, tile_path, preview_path=None):
    """Infer one tile -> ``{"features": [...], "nodes": n, "edges": m}``.

    Features are WGS84 LineStrings, one per predicted road edge.
    """
    upscale = int(config.UPSCALE)
    up_size = NATIVE_TILE_PX * upscale
    win = Window(0, 0, NATIVE_TILE_PX, NATIVE_TILE_PX)

    with rasterio.open(tile_path) as src:
        if upscale > 1:
            img = read_upsampled_window(src, list(bands), win, up_size)
            raw = read_upsampled_window(src, list(RGB), win, up_size)
        else:
            img = read_window(src, list(bands), win)
            raw = read_window(src, list(RGB), win)
        src_crs, bounds = src.crs, src.bounds

    nodes_rc, edges, _itsc, _road = infer_one_tile(
        net, torch.from_numpy(np.ascontiguousarray(img)), config, device
    )

    if preview_path:
        from PIL import Image
        Image.fromarray(_true_colour(raw)).save(preview_path, optimize=True)

    lonlat = _pixels_to_wgs84(nodes_rc, up_size, src_crs, bounds)
    features = [
        {
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [float(lonlat[a][0]), float(lonlat[a][1])],
                    [float(lonlat[b][0]), float(lonlat[b][1])],
                ],
            },
        }
        for a, b in edges
    ]
    return {"features": features, "nodes": int(len(nodes_rc)), "edges": int(len(edges))}
