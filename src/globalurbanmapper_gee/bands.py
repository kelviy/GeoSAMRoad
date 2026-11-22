from collections import OrderedDict

from geosamroad.dataset.bands import (
    ENHANCED_RGB,
    GEE_GLOBALURBANMAPPER_BANDS,
    RGB,
    ROSA_BANDS,
    ROSA_SEG_BANDS,
    ROSA_SOURCE_BANDS,
    S1_ASCENDING_BANDS,
)

S2_EE_BANDS = ("B4", "B3", "B2", "B8", "B5", "B6", "B7", "B8A", "B11", "B12")

# Tile geometry
TILE_PX = 512      # tile size
SCALE_M = 10       # spatial resolution

S2_BANDS = ROSA_BANDS[:10]      # 1-10
# Names given to the fetched S1 means: ascending then descending. Only the
# ascending pair is in the written ROSA layout; descending is selected away.
S1_BANDS = GEE_GLOBALURBANMAPPER_BANDS[10:14]

N_SOURCE_BANDS = len(ROSA_SOURCE_BANDS)                                # 18 source bands
N_FETCHABLE_BANDS = ROSA_BANDS.index(S1_ASCENDING_BANDS[-1]) + 1      # 12 fetched from gee (S2 + S1 asc)

BAND_LIST: "OrderedDict[str, dict]" = OrderedDict(
    rgb=dict(
        description="RGB-only encoders: CLAHE-enhanced RGB (19-21)",
        bands=ENHANCED_RGB,
    ),
    s1s2=dict(
        description=(
            "multi-band encoders: enhanced RGB (19-21) "
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
