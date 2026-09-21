from dataclasses import dataclass
from pathlib import Path

## GENERAL CONSTANTS ##

WGS84 = "EPSG:4326"          # common lat/lon CRS. Google maps view of a map
# SCAFFOLD_DATES = ["2023-01-01"]   # Change to range/composite dates per tile
HARD_CODE_DATE_RANGE = ["2020-01-01", "2021-01-01"]

## ROAD CLASSIFICATION ##

# classification -> road classification -> buffer (half-meter)
# buffer 5.0m = 1 pixel wide in 10m (5*2 = 10m)
OVERTURE_ROAD_CLASSIFICATION = {
    "large": {
        "motorway": 8.0, 
        "trunk": 8.0, 
        "primary": 8.0, 
        "secondary": 8.0
    },
    "medium": {
        "tertiary": 6.0, 
        "unclassified": 5.0, 
        "residential": 5.0
    },
    "links": { # links (not certain about the buffers)
        "motorway_link": 8.0, 
        "trunk_link": 8.0, 
        "primary_link": 8.0, 
        "secondary_link": 6.0,
        "tertiary_link": 5.0,   
    },
    "small": {
        "living_street": 4.0, 
        "track": 3.0
    },
    "ignored": {
        "pedestrian": -1, 
        "path": -1, 
        "footway": -1, 
        "service": -1, 
        "unknown": -1, 
        "steps": -1, 
        "bridleway": -1, 
        "cycleway": -1
    }
}

CDNGI_ROAD_CLASSIFICATION = {
    "large": {
        "National Freeway": 8.0, 
        "National Road": 8.0, 
        "Arterial Road": 8.0, 
        "Main Road": 8.0
    },
    "medium": {
        "On/OffRamp": 6.0, 
        "Secondary Road": 5.0, 
        "Other Road": 5.0
    },
    "small": {
        "Street": 4.0, 
        "Track": 3.0, 
        "Slipway": 3.0,
    },
    "ignored": {
        "Footpath": -1
    }
}

def flatten_classification(road_classification_config: dict, exclude_class: tuple = ("ignored",)) -> tuple[dict, dict, list]:
    """Flatten config into mappings of road class to scale and buffer. 
    The ignored category is dropped.

    Args:
        road_classification_config (dict): The road classification configuration.
        exclude_class (list): List of scale classes to exclude.
    """
    scale_map, buffer_map, road_classifications = {}, {}, []
    for scale_class, classes in road_classification_config.items():
        if scale_class in exclude_class:
            continue
        for road_class, buffer in classes.items():
            scale_map[road_class] = scale_class
            buffer_map[road_class] = buffer
            road_classifications.append(road_class)
    return scale_map, buffer_map, road_classifications


## METADATA CONSTANTS ##

# Classification labels - Jenks road-density classes (road_density_classification).
CLASS_LABELS = ["Low", "Medium", "High"]
EMPTY_LABEL = "Empty"

NAN_EXEMPT_BANDS = (13, 14)  # VV_descending, VH_descending

ROSA_METADATA_COLUMNS = [
    # indexing
    "image_id",
    "zone_name",
    "image_path",
    "mask_path",          # raster mask
    "mask_graph_path",
    "mask_adj_graph_path",
    "mask_keypoint_path",

    # tile metadata
    "road_pixels",
    "road_density",                 # road percentage of tile area
    "road_density_classification",  # Jenks class from road_density, over the whole dataset
    "urbanisation_classification",  # from google earth engine (ESA + WSF), parsed from the filename
    "biome",
    "satellite_image_dates",
    "split_set",

    # image metadata
    "crs",
    "tile_bounding_geometry",   # for visualisation of dataset tiles
    "tile_size",                # tile edge in px. Constant (512px x 512px)
    "band_count",               # source bands + 3 appended enhanced-RGB bands. Constant (23)
    "spatial_resolution",       # spatial resolution. Constant (10)
]

ROSA_SPLIT_CSV_COLUMNS = [
    "image_id",
    "zone_name",
    "split_set",
    "image_path",
    "mask_path",
    "mask_graph_path",
    "mask_adj_graph_path",
    "mask_keypoint_path",
    "road_density",
    "biome",
    "urbanisation_classification",
]

## Dataset Processing Configs ##
@dataclass(frozen=True)
class TileSpec:
    tile_size: int = 512        # tiling grid size from large zone imagery
    patch_size: int = 256       # internal COG patch size

@dataclass(frozen=True)
class RGBEnhanceConfig:
    """CLAHE + gamma enhanced-RGB bands (scikit-image) configs."""
    gamma: float = 0.4
    clahe_kernel_size: tuple = (32, 32)
    clahe_clip_limit: float = 0.01   
    rgb_bands: tuple = (1, 2, 3)

@dataclass(frozen=True)
class CatalogueSchema:
    """Describes a catalogue variant's row schema"""
    columns: list
    geometry_col: str
    split_csv_columns: list
    reindex_tile_id: bool = False


ROSA_SCHEMA = CatalogueSchema(
    columns=ROSA_METADATA_COLUMNS,
    geometry_col="tile_bounding_geometry",
    split_csv_columns=ROSA_SPLIT_CSV_COLUMNS,
    reindex_tile_id=False,
)


# Splits and per-split output subdirectories
SPLIT_NAMES = ("train", "val", "test")
TILE_SUBDIRS = (
    "imagery",
    "masks_raster",
    "masks_graph",
    "mask_adj_graphs",
    "mask_keypoints",
)

@dataclass(frozen=True)
class DatasetPaths:
    """Directory layout of the dataset from its root directory."""
    root: Path

    def __post_init__(self):
        # Normalise to Path
        object.__setattr__(self, "root", Path(self.root))

    @property
    def splits_dir(self) -> Path:
        return self.root / "splits"

    @property
    def metadata_path(self) -> Path:
        return self.root / "metadata.parquet"

    def split_dirs(self, split) -> dict:
        base = self.root / split
        return {name: base / name for name in TILE_SUBDIRS}
