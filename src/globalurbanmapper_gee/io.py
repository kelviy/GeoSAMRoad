import csv
import logging
from pathlib import Path

import rasterio
from rasterio.enums import Resampling
from rasterio.shutil import copy as rio_copy
from rasterio.transform import Affine
from rasterio.windows import Window

from globalurbanmapper_gee.bands import written_band_names
from globalurbanmapper_gee.fetch import SCALE_M, TILE_PX, fetch_tiles, region_grid

log = logging.getLogger(__name__)


def _profile(crs, transform, count, width, height, tile_px):
    return dict(
        driver="GTiff",
        dtype="float32",
        count=count,
        width=width,
        height=height,
        crs=crs,
        transform=transform,
        tiled=True,
        blockxsize=tile_px,
        blockysize=tile_px,
        interleave="band",
        compress="deflate",
        nodata=None,
    )


def write_zone_cog(path, image, crs, bounds, band_list="s1s2", scale=SCALE_M, tile_px=TILE_PX, overviews=True):
    """Write a single large zone COG image"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    origin_x, origin_y, n_cols, n_rows, _ = region_grid(
        bounds, scale=scale, tile_px=tile_px
    )
    transform = Affine(scale, 0.0, origin_x, 0.0, -scale, origin_y)
    names = written_band_names()

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    profile = _profile(crs, transform, len(names), n_cols, n_rows, tile_px)

    with rasterio.open(tmp_path, "w", **profile) as dst:
        dst.descriptions = tuple(names)
        for col, row, width, height, tile in fetch_tiles(
            image, crs, bounds, band_list=band_list, scale=scale, tile_px=tile_px,
        ):
            dst.write(tile, window=Window(col, row, width, height))
        if overviews:
            dst.build_overviews([2, 4, 8, 16], Resampling.average)

    rio_copy(str(tmp_path), str(path), driver="COG", compress="deflate")
    tmp_path.unlink(missing_ok=True)

    log.info("Wrote %s (%d bands, %d x %d)", path, len(names), n_cols, n_rows)
    return dict(
        path=str(path), bands=names, width=n_cols, height=n_rows,
        crs=crs, transform=tuple(transform)[:6],
    )


def write_tiles(out_dir, image, crs, bounds, band_list="s1s2", scale=SCALE_M, tile_px=TILE_PX, name="region", skip_partial=True):
    """Write tiled COG images and metadata.csv"""
    out_dir = Path(out_dir)
    imagery_dir = out_dir / "imagery"
    imagery_dir.mkdir(parents=True, exist_ok=True)

    origin_x, origin_y, _, _, _ = region_grid(bounds, scale=scale, tile_px=tile_px)
    names = written_band_names()

    rows = []
    for col, row, width, height, tile in fetch_tiles(
        image, crs, bounds, band_list=band_list, scale=scale, tile_px=tile_px,
    ):
        if skip_partial and (width < tile_px or height < tile_px):
            log.info("Skipping partial tile at col=%d row=%d", col, row)
            continue

        stem = f"{name}_r{row // tile_px}_c{col // tile_px}"
        tile_path = imagery_dir / f"{stem}.tif"
        transform = Affine(
            scale, 0.0, origin_x + col * scale, 0.0, -scale, origin_y - row * scale
        )
        profile = _profile(crs, transform, len(names), width, height, tile_px)

        tmp_path = tile_path.with_suffix(".tif.tmp")
        with rasterio.open(tmp_path, "w", **profile) as dst:
            dst.descriptions = tuple(names)
            dst.write(tile)
        rio_copy(str(tmp_path), str(tile_path), driver="COG", compress="deflate")
        tmp_path.unlink(missing_ok=True)

        rows.append(
            dict(
                tile_id=stem,
                image_path=str(tile_path.relative_to(out_dir)),
                crs=crs,
                width=width,
                height=height,
                min_x=transform.c,
                max_y=transform.f,
                max_x=transform.c + width * scale,
                min_y=transform.f - height * scale,
            )
        )

    if rows:
        with open(out_dir / "metadata.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    log.info("Wrote %d tiles to %s", len(rows), imagery_dir)
    return rows
