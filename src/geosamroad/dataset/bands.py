from collections import OrderedDict

## Dataset Band information
# Zone imagery as fetched from GEE (20 source bands, 1-based order).
GEE_GLOBALURBANMAPPER_BANDS = (
    "B4_R", "B3_G", "B2_B", "B8_NIR", "B5", "B6", "B7", "B8A", "B11", "B12",
    "VV_ascending", "VH_ascending", "VV_descending", "VH_descending",
    "elevation", "slope", "aspect",
    "esa_urban_10m", "gisa_urban_10m", "wsf_urban_10m",
)
ENHANCED_RGB_BANDS = ("B4_R_enhanced", "B3_G_enhanced", "B2_B_enhanced")

# The generator drops these from the written tiles (band layout version 3).
S1_DESCENDING_BANDS = ("VV_descending", "VH_descending")

# Written ROSA tile layout (version 3): source bands minus S1 descending, then
# the appended enhanced RGB. 21 bands.
ROSA_SOURCE_BANDS = tuple(b for b in GEE_GLOBALURBANMAPPER_BANDS if b not in S1_DESCENDING_BANDS)
ROSA_BANDS = ROSA_SOURCE_BANDS + ENHANCED_RGB_BANDS

# Legacy written layout (version 2): all 20 source bands + enhanced RGB. 23 bands.
ROSA_BANDS_V2 = GEE_GLOBALURBANMAPPER_BANDS + ENHANCED_RGB_BANDS


def band_indices(names, layout=ROSA_BANDS) -> tuple:
    """1-based indices of ``names`` in ``layout``, in the order given."""
    missing = [n for n in names if n not in layout]
    if missing:
        raise ValueError(f"Bands {missing} are not in the layout {list(layout)}")
    return tuple(layout.index(n) + 1 for n in names)


# Common index groups, 1-based into ROSA_BANDS. Derived from names so a
# layout change cannot silently point them at other physical bands.
S2_BAND_NAMES = GEE_GLOBALURBANMAPPER_BANDS[:10]
S1_ASCENDING_BANDS = ("VV_ascending", "VH_ascending")

RGB = band_indices(("B4_R", "B3_G", "B2_B"))
S2_10M = band_indices(("B4_R", "B3_G", "B2_B", "B8_NIR"))
S2_20M = band_indices(("B5", "B6", "B7", "B8A", "B11", "B12"))
S2_SAR = band_indices(S1_ASCENDING_BANDS)     # VV/VH ascending (descending dropped)
ENHANCED_RGB = band_indices(ENHANCED_RGB_BANDS)  # appended CLAHE+gamma enhanced RGB


def written_band_names(*, version=3):
    if version == 3:
        return list(ROSA_BANDS)
    elif version == 2:
        return list(ROSA_BANDS_V2)
    elif version == 1:
        return list(GEE_GLOBALURBANMAPPER_BANDS)
    raise ValueError(f"Unknown version {version}, expected 1, 2 or 3")

## Model band information

# Band index mapping
S2L2A_BANDS = band_indices(S2_BAND_NAMES)
S1GRD_BANDS = band_indices(S1_ASCENDING_BANDS)

# TerraMind S2L2A: RED, GREEN, BLUE, NIR_BROAD, RED_EDGE_1/2/3, NIR_NARROW, SWIR_1/2
# TerraMind S1GRD: VV, VH (ascending orbit)
TERRAMIND_MODALITIES: "OrderedDict[str, tuple[int, ...]]" = OrderedDict(
    S2L2A=S2L2A_BANDS,
    S1GRD=S1GRD_BANDS,
)

# Semantic names
RGB_NAMES = ["RED", "GREEN", "BLUE"]
TM_S2L2A_NAMES = ["RED", "GREEN", "BLUE", "NIR_BROAD", "RED_EDGE_1",
                  "RED_EDGE_2", "RED_EDGE_3", "NIR_NARROW", "SWIR_1", "SWIR_2"]
TM_S1GRD_NAMES = ["VV", "VH"]

ROSA_SEG_BANDS = ENHANCED_RGB + band_indices(   # 12 channels
    ("B8_NIR", "B5", "B6", "B7", "B8A", "B11", "B12") + S1_ASCENDING_BANDS
)
COMPLETE_SEG_ENCODERS = ("unet", "sgcn")


def resolve_input(encoder: str, rgb: bool, rgb_source: str = "enhanced"):
    """Dataset input specification"""
    if rgb_source not in ("raw", "enhanced"):
        raise ValueError(f"rgb_source must be 'raw' or 'enhanced', got {rgb_source!r}")
    rgb_idx = ENHANCED_RGB if rgb_source == "enhanced" else RGB

    if encoder == "sam":
        return rgb_idx
    if encoder == "terramind":
        if rgb:
            return rgb_idx
        return S2L2A_BANDS + S1GRD_BANDS
    if encoder in COMPLETE_SEG_ENCODERS:
        return (rgb_idx if rgb else ROSA_SEG_BANDS)
    raise ValueError(f"Unsupported encoder: {encoder}")


def encoder_modalities(encoder: str, rgb: bool) -> "OrderedDict[str, list[str]]":
    """Semantic band names for models"""
    if encoder == "sam":
        return OrderedDict(image=list(RGB_NAMES))
    if encoder == "terramind":
        if rgb:
            return OrderedDict(RGB=list(RGB_NAMES))
        return OrderedDict(S2L2A=list(TM_S2L2A_NAMES), S1GRD=list(TM_S1GRD_NAMES))
    if encoder in COMPLETE_SEG_ENCODERS:
        idx = ENHANCED_RGB if rgb else ROSA_SEG_BANDS
        return OrderedDict(image=[ROSA_BANDS[b - 1] for b in idx])
    raise ValueError(f"No band-name mapping for encoder: {encoder}")