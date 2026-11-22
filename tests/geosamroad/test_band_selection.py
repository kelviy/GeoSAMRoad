"""Training band selection by name.

The written tile layout dropped the S1 descending bands (23 -> 21 bands), which
moved band indices. These tests pin each encoder to the physical bands, and
normalisation stats, it used before that change. Expected values are literals
from the old 23-band layout, not recomputed from the code.
"""
import pytest

from geosamroad.dataset.bands import ROSA_BANDS, encoder_modalities, resolve_input
from geosamroad.models.normalization import rosa_norm

ENHANCED = ["B4_R_enhanced", "B3_G_enhanced", "B2_B_enhanced"]
RAW_RGB = ["B4_R", "B3_G", "B2_B"]
S2 = ["B4_R", "B3_G", "B2_B", "B8_NIR", "B5", "B6", "B7", "B8A", "B11", "B12"]
S1_ASC = ["VV_ascending", "VH_ascending"]
SEG = ENHANCED + ["B8_NIR", "B5", "B6", "B7", "B8A", "B11", "B12"] + S1_ASC

# Old ROSA_MEANS / ROSA_STDS entries, keyed by band name.
OLD_STATS = {
    "B4_R_enhanced": (0.551126, 0.177911),
    "B3_G_enhanced": (0.522076, 0.147157),
    "B2_B_enhanced": (0.48006, 0.147469),
    "B8_NIR": (0.23897, 0.0688163),
    "B5": (0.152557, 0.07177),
    "B6": (0.207308, 0.0614309),
    "B7": (0.228655, 0.0655398),
    "B8A": (0.246422, 0.0677955),
    "B11": (0.267869, 0.103342),
    "B12": (0.204559, 0.105481),
    "VV_ascending": (-12.6379, 3.53385),
    "VH_ascending": (-19.5885, 3.96946),
}


def names(indices):
    return [ROSA_BANDS[i - 1] for i in indices]


@pytest.mark.parametrize(
    "encoder, rgb, rgb_source, expected",
    [
        ("sam", True, "enhanced", ENHANCED),
        ("sam", True, "raw", RAW_RGB),
        ("terramind", True, "enhanced", ENHANCED),
        ("terramind", False, "enhanced", S2 + S1_ASC),
        ("unet", True, "enhanced", ENHANCED),
        ("unet", False, "enhanced", SEG),
        ("sgcn", True, "raw", RAW_RGB),
        ("sgcn", False, "enhanced", SEG),
    ],
)
def test_each_encoder_reads_the_same_physical_bands(encoder, rgb, rgb_source, expected):
    assert names(resolve_input(encoder, rgb, rgb_source)) == expected


@pytest.mark.parametrize("encoder", ["unet", "sgcn"])
@pytest.mark.parametrize("rgb, expected", [(True, ENHANCED), (False, SEG)])
def test_segmentation_modality_names_match_the_bands_read(encoder, rgb, expected):
    assert encoder_modalities(encoder, rgb)["image"] == expected


@pytest.mark.parametrize("rgb, expected", [(True, ENHANCED), (False, SEG)])
def test_rosa_norm_stats_follow_the_band_by_name(rgb, expected):
    mean, std, scale = rosa_norm(resolve_input("unet", rgb))
    assert mean == pytest.approx([OLD_STATS[n][0] for n in expected])
    assert std == pytest.approx([OLD_STATS[n][1] for n in expected])
    assert scale == 1.0


def test_written_layout_has_no_s1_descending_bands():
    assert len(ROSA_BANDS) == 21
    assert not {"VV_descending", "VH_descending"} & set(ROSA_BANDS)
