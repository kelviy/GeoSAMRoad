"""Guards the synthetic zone itself, so each case keeps the geometry its name promises."""
import pytest
from shapely.geometry import Point, box

from sentinel2data.generator.config import WGS84
from tests.sentinel2data.synthetic_zone import (
    EMPTY_TILE,
    RESOLUTION,
    TILE_SIZE,
    TRAIN_ORIGIN,
)

CLEANING_CASES = {
    "gap", "dangling_stub", "near_duplicate", "parallel_carriageways",
    "unnoded_crossing", "track", "slipway", "zone_edge",
}


def tile_box(tile):
    r, c = (int(part[1:]) for part in tile.split("_"))
    x0, y0 = TRAIN_ORIGIN
    span = TILE_SIZE * RESOLUTION
    return box(x0 + c * span, y0 - (r + 1) * span, x0 + (c + 1) * span, y0 - r * span)


def test_every_case_is_named(shared_synthetic_zone):
    assert CLEANING_CASES <= set(shared_synthetic_zone.cases)


def test_case_road_ids_point_at_their_rows_in_roads_parquet(shared_synthetic_zone):
    roads = shared_synthetic_zone.read_roads().to_crs(shared_synthetic_zone.crs)
    for case in shared_synthetic_zone.cases.values():
        for road_id, geom in zip(case.road_ids, case.geometries, strict=True):
            assert roads.geometry.iloc[road_id].hausdorff_distance(geom) < 0.01, case.name


def test_roads_parquet_has_the_cdngi_road_layer_columns(shared_synthetic_zone):
    roads = shared_synthetic_zone.read_roads()
    assert list(roads.columns) == ["data_source", "road_class", "scale_class", "buffer", "surface", "geometry"]
    assert roads.crs.to_string() == WGS84


@pytest.mark.parametrize("name", sorted(CLEANING_CASES - {"zone_edge"}))
def test_case_lies_inside_its_tile(shared_synthetic_zone, name):
    case = shared_synthetic_zone.cases[name]
    assert case.tile != EMPTY_TILE
    for geom in case.geometries:
        assert tile_box(case.tile).contains(geom)


def test_no_case_touches_the_empty_tile(shared_synthetic_zone):
    empty = tile_box(EMPTY_TILE).buffer(20)  # 20 m covers the widest road buffer
    for case in shared_synthetic_zone.cases.values():
        assert not any(g.intersects(empty) for g in case.geometries), case.name


def test_gap_ends_are_20_m_apart(shared_synthetic_zone):
    a, b = shared_synthetic_zone.cases["gap"].geometries
    assert Point(a.coords[-1]).distance(Point(b.coords[0])) == pytest.approx(20.0)


def test_dangling_stub_is_short_and_free_at_one_end(shared_synthetic_zone):
    main, stub = shared_synthetic_zone.cases["dangling_stub"].geometries
    assert stub.length < 30.0
    assert main.distance(Point(stub.coords[0])) == pytest.approx(0.0)
    assert main.distance(Point(stub.coords[-1])) > 0.0


def test_near_duplicates_are_within_1_m(shared_synthetic_zone):
    a, b = shared_synthetic_zone.cases["near_duplicate"].geometries
    assert 0.0 < a.hausdorff_distance(b) <= 1.0


def test_parallel_carriageways_are_about_20_m_apart(shared_synthetic_zone):
    a, b = shared_synthetic_zone.cases["parallel_carriageways"].geometries
    assert a.distance(b) == pytest.approx(20.0)


def test_unnoded_crossing_crosses_without_a_shared_vertex(shared_synthetic_zone):
    a, b = shared_synthetic_zone.cases["unnoded_crossing"].geometries
    assert a.crosses(b)
    assert not set(a.coords) & set(b.coords)


def test_track_and_slipway_carry_their_road_class(shared_synthetic_zone):
    roads = shared_synthetic_zone.read_roads()
    for name, road_class in [("track", "Track"), ("slipway", "Slipway")]:
        (road_id,) = shared_synthetic_zone.cases[name].road_ids
        assert roads["road_class"].iloc[road_id] == road_class


def test_zone_edge_road_runs_past_the_zone(shared_synthetic_zone):
    (road,) = shared_synthetic_zone.cases["zone_edge"].geometries
    _, y0 = TRAIN_ORIGIN
    assert max(y for _, y in road.coords) > y0
