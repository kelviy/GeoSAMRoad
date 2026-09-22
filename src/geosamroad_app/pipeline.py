"""Selected tiles -> Earth Engine fetch -> inference -> road graph.

One job loads the model once and walks its tiles, so a multi-tile selection pays
the checkpoint load only on the first tile.
"""
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from geosamroad_app import grid, runner
from geosamroad_app.variants import get_variant

log = logging.getLogger(__name__)

# Roads are static, so a wide composite window just buys cloud-free pixels.
COMPOSITE_HALF_WINDOW_DAYS = 182


def composite_window(date_str):
    """``YYYY-MM-DD`` -> (start, end) spanning +/- half a year around it."""
    try:
        target = datetime.strptime((date_str or "").strip(), "%Y-%m-%d")
    except (ValueError, TypeError):
        target = datetime.utcnow()
        log.warning("Unparsed date %r; centring the composite on today", date_str)
    return (
        (target - timedelta(days=COMPOSITE_HALF_WINDOW_DAYS)).strftime("%Y-%m-%d"),
        (target + timedelta(days=COMPOSITE_HALF_WINDOW_DAYS)).strftime("%Y-%m-%d"),
    )


def fetch_tile(tile_id, band_list, start_date, end_date, out_dir):
    """Fetch one grid cell from Earth Engine -> path of the written GeoTIFF.

    Returns ``None`` when Earth Engine produced no full tile (the cell can fall
    a pixel short once reprojected into the S2 granule's CRS).
    """
    from globalurbanmapper_gee import api

    api.fetch_region(
        bbox=grid.tile_bounds_wgs84(tile_id),
        start_date=start_date,
        end_date=end_date,
        band_list=band_list,
        out_dir=str(out_dir),
        initialize=False,
    )
    written = sorted((Path(out_dir) / "imagery").glob("*.tif"))
    if not written:
        log.warning("Earth Engine returned no full tile for %s", tile_id)
        return None
    return written[0]


def _release(net):
    """Hand the model's VRAM back before the job returns.

    Torch's caching allocator holds a finished model's memory against the
    process, so without this one job leaves several GB reserved and the next
    one -- or anything else sharing the card -- fails with CUDA OOM. Costs a
    checkpoint reload per job, which is seconds against a multi-minute fetch.
    """
    if net is None:
        return
    try:
        import gc

        import torch

        del net
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:                       # never let cleanup fail a good job
        log.exception("Could not release GPU memory")


def run(tile_ids, variant_name, date, data_root, checkpoint_root, progress=None):
    """Fetch and infer every selected tile.

    Args:
        tile_ids: grid cell ids from the selector.
        variant_name: encoder variant (see ``variants.VARIANTS``).
        date: ``YYYY-MM-DD`` the composite is centred on.
        data_root: scratch dir for this job's imagery and previews.
        checkpoint_root: dir holding ``<variant>/*.ckpt``.
        progress: optional ``callable(done, total, message)``.

    Returns:
        ``dict`` with a GeoJSON FeatureCollection of road edges, per-tile
        previews, and totals.
    """
    from globalurbanmapper_gee import auth

    variant = get_variant(variant_name)
    start_date, end_date = composite_window(date)
    data_root = Path(data_root)
    previews_dir = data_root / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)

    def tick(done, message):
        log.info("[%d/%d] %s", done, len(tile_ids), message)
        if progress:
            progress(done, len(tile_ids), message)

    tick(0, "Connecting to Earth Engine")
    auth.initialize()

    net = config = bands = device = None
    features, previews, total_nodes, total_edges, failed = [], [], 0, 0, []

    for i, tile_id in enumerate(tile_ids):
        tick(i, f"Fetching imagery for {tile_id}")
        work = data_root / "fetch" / tile_id
        try:
            tif = fetch_tile(tile_id, variant["band_list"], start_date, end_date, work)
            if tif is None:
                failed.append({"tile_id": tile_id, "error": "no imagery returned"})
                continue

            if net is None:   # first tile pays the model load
                tick(i, f"Loading {variant['label']}")
                net, config, bands, device = runner.load_model(
                    checkpoint_root, variant_name, dataset_dir=str(data_root)
                )

            tick(i, f"Detecting roads in {tile_id}")
            preview = previews_dir / f"{tile_id}.png"
            out = runner.infer_tile(net, config, bands, device, tif, preview_path=preview)

            features.extend(out["features"])
            total_nodes += out["nodes"]
            total_edges += out["edges"]
            previews.append({
                "tile_id": tile_id,
                "url": f"previews/{tile_id}.png",
                "bounds": grid.tile_bounds_wgs84(tile_id),
                "corners": grid.tile_image_corners(tile_id),
            })
        except Exception as exc:                      # one bad tile must not sink the job
            log.exception("Tile %s failed", tile_id)
            failed.append({"tile_id": tile_id, "error": str(exc)})
        finally:
            shutil.rmtree(work, ignore_errors=True)   # imagery is large; previews suffice

    _release(net)
    tick(len(tile_ids), "Done")
    return {
        "roads": {"type": "FeatureCollection", "features": features},
        "previews": previews,
        "tiles_processed": len(tile_ids) - len(failed),
        "tiles_failed": failed,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "variant": variant_name,
        "composite": {"start": start_date, "end": end_date},
        "device": str(device) if device is not None else None,
    }
