import pytest

from tests.sentinel2data.synthetic_zone import SyntheticZone, build_synthetic_zone


@pytest.fixture
def synthetic_zone(tmp_path) -> SyntheticZone:
    """A fresh synthetic zone (imagery split layout + roads parquet) per test."""
    return build_synthetic_zone(tmp_path / "zone")


@pytest.fixture(scope="module")
def shared_synthetic_zone(tmp_path_factory) -> SyntheticZone:
    """One synthetic zone shared by a module. Don't modify its files."""
    return build_synthetic_zone(tmp_path_factory.mktemp("zone"))
