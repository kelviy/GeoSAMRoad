from collections import OrderedDict

from geosamroad.dataset.bands import (
    ENHANCED_RGB,
    ENHANCED_RGB_BANDS,
    RGB,
    ROSA_BANDS,
    ROSA_SEG_BANDS,
)

S2_EE_BANDS = ("B4", "B3", "B2", "B8", "B5", "B6", "B7", "B8A", "B11", "B12")

# Tile geometry
TILE_PX = 512      # tile size
SCALE_M = 10       # spatial resolution

S2_BANDS = ROSA_BANDS[:10]      # 1-10
S1_BANDS = ROSA_BANDS[10:14]    # 11-14, VV/VH ascending then descending

N_SOURCE_BANDS = len(ROSA_BANDS) - len(ENHANCED_RGB_BANDS)  # equals 20 bands
N_FETCHABLE_BANDS = len(S2_BANDS) + len(S1_BANDS)           # equals 14 bands to fetch from gee

BAND_LIST: "OrderedDict[str, dict]" = OrderedDict(
    rgb=dict(
        description="RGB-only encoders: CLAHE-enhanced RGB (21-23)",
        bands=ENHANCED_RGB,
    ),
    s1s2=dict(
        description=(
            "multi-band encoders: enhanced RGB (21-23) "
            "+ S2 (4-10) + S1 VV/VH ascending (11-12)"
        ),
        bands=ROSA_SEG_BANDS,
    ),
)


def model_bands(band_list: str) -> tuple:
    if band_list not in BAND_LIST:
        raise ValueError(
            f"Unknown band_list {band_list!r}, expected one of {list(BAND_LIST)}"
        )
    return tuple(BAND_LIST[band_list]["bands"])


def source_bands(band_list: str) -> tuple:
    requested = set(model_bands(band_list))
    src = {b for b in requested if b <= N_SOURCE_BANDS}
    if requested & set(ENHANCED_RGB):
        src |= set(RGB)

    unfetchable = sorted(b for b in src if b > N_FETCHABLE_BANDS)
    if unfetchable:
        raise ValueError(
            f"band_list {band_list!r} asks for bands {unfetchable}"
        )
    return tuple(sorted(src))


def source_band_names(band_list: str) -> list:
    return [ROSA_BANDS[b - 1] for b in source_bands(band_list)]


def written_band_names() -> list:
    return list(ROSA_BANDS)
