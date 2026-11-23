"""The review service: the JSON-lines layer the QGIS plugin drives the review
core through, on the train zone GeoPackage after ``detect``."""

import json

import pytest

from sentinel2data.cleaning import review
from sentinel2data.cleaning.review_service import handle
from tests.sentinel2data.test_detect import gpkg, run_detect
from tests.sentinel2data.test_review import (
    assert_same_rows,
    detect_with_stub_hidden,
    read,
)


@pytest.fixture
def zone(zones):
    run_detect(zones)
    return gpkg(zones)


def ask(zone, op, **args):
    """``handle``'s answer as it arrives over the wire."""
    response = json.loads(json.dumps(handle({"op": op, "gpkg": str(zone), **args})))
    assert response["ok"], response
    return response


def test_state_gives_the_current_item_its_place_and_the_progress(zone):
    first = review.build_queue(zone)[0]
    state = ask(zone, "state")["state"]
    assert state["item"]["kind"] == first.kind == "proposal"
    assert state["item"]["fid"] == first.fid
    assert state["item"]["extent"] == list(first.extent)
    assert (state["index"], state["length"]) == (0, len(review.build_queue(zone)))
    p = review.progress(zone)
    assert state["progress"] == {
        "done": 0,
        "total": p.total,
        "remaining": p.total,
        "flags_decided": 0,
        "flags_total": p.flags_total,
        "tiles_reviewed": 0,
        "tiles_kept": p.tiles_kept,
    }


def test_deciding_writes_the_decision_and_moves_on_to_the_next_item(zone):
    first, second = review.build_queue(zone)[:2]
    answer = ask(zone, "decide", fid=first.fid, decision="real", annotator="Thandi")
    flags = read(zone, "flags")
    assert (flags.loc[first.fid, "decision"], flags.loc[first.fid, "annotator"]) == (
        "real",
        "Thandi",
    )
    assert answer["deleted"] == []
    assert answer["state"]["item"]["fid"] == second.fid
    assert answer["state"]["progress"]["flags_decided"] == 1


@pytest.mark.parametrize(
    "request_args, error_type",
    [
        ({"op": "decide", "decision": "real", "annotator": " "}, "ValueError"),
        ({"op": "decide", "decision": "maybe", "annotator": "Thandi"}, "ValueError"),
        ({"op": "teleport"}, "ValueError"),
        ({"op": "state", "filter": {"kind": "road"}}, "ValueError"),
    ],
)
def test_a_refused_request_answers_with_the_error_and_writes_nothing(
    zone, request_args, error_type
):
    first = review.build_queue(zone)[0]
    before = read(zone, "flags")
    answer = handle({"gpkg": str(zone), "fid": first.fid, **request_args})
    assert answer["ok"] is False
    assert answer["error_type"] == error_type
    assert answer["error"]
    assert_same_rows(read(zone, "flags"), before)


def test_back_and_forward_step_through_the_queue_and_say_when_they_cannot(zone):
    first, second = review.build_queue(zone)[:2]
    ask(zone, "decide", fid=first.fid, decision="not_defect", annotator="Thandi")

    back = ask(zone, "back")
    assert (back["moved"], back["state"]["item"]["fid"], back["state"]["index"]) == (
        True,
        first.fid,
        0,
    )
    assert ask(zone, "back")["moved"] is False
    forward = ask(zone, "forward")
    assert (forward["moved"], forward["state"]["item"]["fid"]) == (True, second.fid)


def test_reviewing_a_tile_stores_the_note_under_the_filter_given(zone):
    tile = review.build_queue(zone, review.QueueFilter(kind="tile"))[0]
    answer = ask(
        zone,
        "review_tile",
        fid=tile.fid,
        annotator="Thandi",
        note="river crossing",
        filter={"kind": "tile"},
    )
    tiles = read(zone, "tiles")
    assert (tiles.loc[tile.fid, "note"], tiles.loc[tile.fid, "annotator"]) == (
        "river crossing",
        "Thandi",
    )
    assert answer["state"]["item"]["kind"] == "tile"
    assert answer["state"]["item"]["fid"] != tile.fid
    assert answer["state"]["progress"]["tiles_reviewed"] == 1


def test_state_lists_the_filter_choices_hidden_detectors_and_needs_qgis_count(
    zones,
):
    detect_with_stub_hidden(zones)
    zone = gpkg(zones)
    other = next(i for i in review.build_queue(zone) if i.kind == "flag")
    ask(zone, "decide", fid=other.fid, decision="needs_qgis", annotator="Thandi")

    state = ask(zone, "state")["state"]
    assert state["hidden"] == ["stub"]
    assert state["needs_qgis"] == 1
    assert state["choices"] == {
        "kind": ["proposal", "flag", "tile"],
        "flag_type": ["gap", "stub", "duplicate", "crossing", "missing?", "phantom?"],
        "source": ["labels", "overture"],
    }
