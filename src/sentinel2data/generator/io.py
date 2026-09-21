"""COG and writers"""
from pathlib import Path
import numpy as np
import rasterio
from sentinel2data.generator.helper import stretch_bands

# Loading
def load_image_rgb(path, bands=(1, 2, 3), percentile_range=(2, 98)):
    """Load an RGB image as BGR uint8 array (OpenCV convention). Support tiff, png"""
    import cv2

    # rgb image
    if not is_raster(path):
        return cv2.imread(str(path))

    # convert tiff to rgb image
    with rasterio.open(path) as src:
        image = src.read(bands)  # (3, H, W), band order R, G, B

    if image.dtype == np.uint8:
        rgb = np.transpose(image, (1, 2, 0))
    else:
        rgb = np.transpose(stretch_bands(image.astype(np.float32), percentile_range), (1, 2, 0))

    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def load_binary_mask(path, band=1):
    """Load binary road mask"""
    if is_raster(path):
        with rasterio.open(path) as src:
            image = src.read(band)
    else:
        import cv2

        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)

    if image is None:
        raise ValueError(f"Failed to load mask from {path}. Arr is None")

    return np.where(image > 0, 255, 0).astype(np.uint8)

def is_raster(path):
    return Path(path).suffix.lower() in (".tif", ".tiff")


# Writing
def _set_tiling(profile, blockxsize, blockysize, tiled=True, interleave='band'):
    """Set tiling for GeoTiff"""
    if tiled:
        profile.update(tiled=True, blockxsize=blockxsize, blockysize=blockysize)
        if interleave is not None:
            profile["interleave"] = interleave
    else:
        profile["tiled"] = False
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)


def write_image_cog(path, arr, profile, *, transform, blockxsize=None, blockysize=None, band_names=None):
    """Write image array (C, H, W) to COG (tile, band interleaved, deflate compression)"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(profile)
    profile.update(
        count=arr.shape[0],
        dtype=arr.dtype,
        height=arr.shape[1],
        width=arr.shape[2],
        transform=transform,
        compress="deflate",
        predictor=1,
    )
    _set_tiling(profile, blockxsize, blockysize)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr)
        if band_names:
            for i, name in enumerate(band_names, start=1):
                dst.set_band_description(i, name)
    return path


def write_mask_cog(path, mask, profile, *, transform=None, blockxsize=None, blockysize=None):
    """Write binary mask GeoTiff"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(profile)
    profile.update(
        count=1,
        dtype="uint8",
        nodata=0,
        height=mask.shape[0],
        width=mask.shape[1],
        compress="lzw",
    )
    if transform is not None:
        profile["transform"] = transform
    _set_tiling(profile, blockxsize, blockysize)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(mask, 1)
    return path


def write_pickle(path, obj):
    """Writes pickled object of adjacency dict"""
    import pickle

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(obj, f)
    return path


def write_roads_parquet(path, roads):
    """Writes road vector centrelines in GeoParquet"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    roads.to_parquet(path)
    return path


def write_geoparquet(gdf, path):
    """Writes GeoDataFrame metadata in GeoParquet"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(path)
    return path


def write_split_csvs(gdf, splits_dir, columns):
    """Writes metadata csv files per split"""
    splits_dir = Path(splits_dir)
    splits_dir.mkdir(parents=True, exist_ok=True)

    written = []
    if gdf.empty:
        return written
    for split_name, group in gdf.groupby("split_set"):
        out_path = splits_dir / f"{split_name}.csv"
        group[columns].to_csv(out_path, index=False)
        print(f"Wrote {len(group)} rows to {out_path}")
        written.append(out_path)
    return written
