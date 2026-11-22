"""GEE port band helpers: what is fetched and what is written, by name."""
import importlib

import pytest

from globalurbanmapper_gee.bands import model_bands, source_band_names, written_band_names
from geosamroad.dataset.bands import ROSA_BANDS

ENHANCED = ["B4_R_enhanced", "B3_G_enhanced", "B2_B_enhanced"]
S2 = ["B4_R", "B3_G", "B2_B", "B8_NIR", "B5", "B6", "B7", "B8A", "B11", "B12"]


@pytest.mark.parametrize(
    "module", ["globalurbanmapper_gee.fetch", "globalurbanmapper_gee.io", "geosamroad_app.grid"],
)
def test_modules_that_import_band_constants_still_load(module):
    importlib.import_module(module)


def test_written_tiles_use_the_21_band_rosa_layout():
    assert written_band_names() == S2 + [
        "VV_ascending", "VH_ascending", "elevation", "slope", "aspect",
        "esa_urban_10m", "gisa_urban_10m", "wsf_urban_10m",
    ] + ENHANCED


@pytest.mark.parametrize(
    "band_list, model, fetched",
    [
        ("rgb", ENHANCED, ["B4_R", "B3_G", "B2_B"]),
        (
            "s1s2",
            ENHANCED + ["B8_NIR", "B5", "B6", "B7", "B8A", "B11", "B12", "VV_ascending", "VH_ascending"],
            S2 + ["VV_ascending", "VH_ascending"],
        ),
    ],
)
def test_band_list_fetches_only_the_source_bands_its_model_reads(band_list, model, fetched):
    assert [ROSA_BANDS[b - 1] for b in model_bands(band_list)] == model
    assert source_band_names(band_list) == fetched
