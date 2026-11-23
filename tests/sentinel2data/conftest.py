import shutil

import pytest

from tests.sentinel2data.synthetic_zone import (
    TEST_ZONE,
    TRAIN_ZONE,
    VAL_ZONE,
    SyntheticZone,
    build_synthetic_zone,
)
from tests.sentinel2data.test_generate import run_generate
from tests.sentinel2data.test_package_zones import run_package


@pytest.fixture
def synthetic_zone(tmp_path) -> SyntheticZone:
    """A fresh synthetic zone (imagery split layout + roads parquet) per test."""
    return build_synthetic_zone(tmp_path / "zone")


@pytest.fixture(scope="module")
def shared_synthetic_zone(tmp_path_factory) -> SyntheticZone:
    """One synthetic zone shared by a module. Don't modify its files."""
    return build_synthetic_zone(tmp_path_factory.mktemp("zone"))


@pytest.fixture(scope="module")
def baseline(shared_synthetic_zone, tmp_path_factory):
    return run_generate(shared_synthetic_zone, tmp_path_factory.mktemp("baseline"))


@pytest.fixture(scope="module")
def packaged(shared_synthetic_zone, baseline, tmp_path_factory):
    """All zones packaged once. Don't modify its files."""
    out = tmp_path_factory.mktemp("packaged")
    run_package(shared_synthetic_zone, baseline, out)
    return out


@pytest.fixture
def zones(packaged, tmp_path):
    """A zones folder holding only each zone's GeoPackage."""
    out = tmp_path / "zones"
    for zone in (TRAIN_ZONE, VAL_ZONE, TEST_ZONE):
        (out / zone).mkdir(parents=True)
        shutil.copy(packaged / zone / f"{zone}.gpkg", out / zone)
    return out
