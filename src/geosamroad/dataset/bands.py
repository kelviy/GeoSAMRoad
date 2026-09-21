from collections import OrderedDict

## Dataset Band information
S2_BANDS = (
    "B4_R", "B3_G", "B2_B", "B8_NIR", "B5", "B6", "B7", "B8A", "B11", "B12",
    "VV_ascending", "VH_ascending", "VV_descending", "VH_descending",
    "elevation", "slope", "aspect",
    "esa_urban_10m", "gisa_urban_10m", "wsf_urban_10m",
)
ENHANCED_RGB_BANDS = ("B4_R_enhanced", "B3_G_enhanced", "B2_B_enhanced")
S2_V2_BANDS = S2_BANDS + ENHANCED_RGB_BANDS

# Common index groups maping into the 23-band V2 imagery.
RGB = (1, 2, 3)                 # B4_R, B3_G, B2_B
S2_10M = (1, 2, 3, 4)           # RBG + B8_NIR
S2_20M = (5, 6, 7, 8, 9, 10)    # B5, B6, B7, B8A, B11, B12
S2_SAR = (11, 12, 13, 14)       # VV/VH ascending/descending
ENHANCED_RGB = (21, 22, 23)     # appended CLAHE+gamma enhanced RGB


def written_band_names(*, version=2):
    if version == 2:
        return list(S2_V2_BANDS)
    elif version == 1:
        return list(S2_BANDS)
    raise ValueError(f"Unknown version {version}, expected 1 or 2")

## Model band information

# Band index mapping
S2L2A_BANDS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
S1GRD_BANDS = (11, 12)

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

ROSA_SEG_BANDS = ENHANCED_RGB + (4, 5, 6, 7, 8, 9, 10, 11, 12)   # 12 channels
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
        return OrderedDict(image=[S2_V2_BANDS[b - 1] for b in idx])
    raise ValueError(f"No band-name mapping for encoder: {encoder}")