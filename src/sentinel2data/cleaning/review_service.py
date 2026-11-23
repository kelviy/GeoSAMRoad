"""The review service: the review core over JSON lines, for the QGIS plugin.

QGIS's Python has none of the review core's dependencies (pandas, shapely,
pyogrio), so the plugin runs this module in the repo's Python and sends one
JSON request per line on stdin. Each answer is one JSON line on stdout. Every
request names its GeoPackage and the service keeps no state between
requests, like the review core under it.

Run it with ``python -m sentinel2data.cleaning.review_service``.
"""

import dataclasses
import json
import sys

from sentinel2data.cleaning import review
from sentinel2data.cleaning.contract import FLAG_SOURCES, FLAG_TYPES

# The values each ``QueueFilter`` field takes, for the plugin's filter lists.
FILTER_CHOICES = {
    "kind": list(review.ITEM_KINDS),
    "flag_type": list(FLAG_TYPES),
    "source": list(FLAG_SOURCES),
}


def handle(request: dict) -> dict:
    """Answer one request: ``{"ok": true, "state": ...}`` with the zone's
    review state after the request, or ``{"ok": false, "error": ...,
    "error_type": ...}`` when the review core refused it or failed.

    This is the plugin's only way in, so every error comes back as an answer
    and the service keeps running."""
    try:
        return _answer(request)
    except Exception as exc:  # noqa: BLE001 (the plugin shows every failure)
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


def _answer(request: dict) -> dict:
    gpkg = request["gpkg"]
    queue_filter = review.QueueFilter(**(request.get("filter") or {}))
    answer: dict = {"ok": True}
    match request["op"]:
        case "state":
            pass
        case "decide":
            deleted = review.decide_flag(
                gpkg,
                request["fid"],
                request["decision"],
                annotator=request["annotator"],
            )
            answer["deleted"] = list(deleted)
        case "review_tile":
            review.review_tile(
                gpkg,
                request["fid"],
                annotator=request["annotator"],
                note=request.get("note"),
            )
        case "back":
            answer["moved"] = review.go_back(gpkg, queue_filter) is not None
        case "forward":
            answer["moved"] = review.go_forward(gpkg, queue_filter) is not None
        case op:
            raise ValueError(f"unknown op {op!r}")
    return answer | {"state": _state(gpkg, queue_filter)}


def _state(gpkg, queue_filter) -> dict:
    queue = review.build_queue(gpkg, queue_filter)
    item = review.current_item(gpkg, queue_filter)
    p = review.progress(gpkg)
    return {
        "item": None if item is None else _item(item),
        "index": len(queue) if item is None else queue.index(item),
        "length": len(queue),
        "progress": {
            "done": p.done,
            "total": p.total,
            "remaining": p.remaining,
            **dataclasses.asdict(p),
        },
        "hidden": sorted(review.hidden_detectors(gpkg)),
        "needs_qgis": len(review.needs_qgis_items(gpkg)),
        "choices": FILTER_CHOICES,
    }


def _item(item: review.QueueItem) -> dict:
    return {"kind": item.kind, "done": item.done, **dataclasses.asdict(item)}


def serve(stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        if line.strip():
            stdout.write(json.dumps(handle(json.loads(line))) + "\n")
            stdout.flush()


if __name__ == "__main__":
    # The plugin reads one JSON line per request, so anything else the core
    # or its libraries print goes to stderr.
    answers, sys.stdout = sys.stdout, sys.stderr
    serve(stdout=answers)
