"""The zone GeoPackage contract shared by the packager, detectors, review core
and assembler (see the KEL-5 spec and ``GLOSSARY.md``)."""
import pandas as pd

from sentinel2data.generator.config import CDNGI_ROAD_CLASSIFICATION, flatten_classification

RAW_LABELS_LAYER = "raw_labels"
LABELS_LAYER = "labels"
TILES_LAYER = "tiles"
FLAGS_LAYER = "flags"
DETECTOR_RUNS_LAYER = "detector_runs"

# labels
LABEL_STATUSES = ("kept", "modified", "added", "deleted", "proposed")
# Label statuses that are roads now; ``deleted`` and ``proposed`` rows are not.
ROAD_STATUSES = ("kept", "modified", "added")
DEFECTS = ("gap", "stub", "misalignment", "phantom", "missing", "temporal")

# flags: point or polygon locations (the QGIS project shows Point and Polygon
# rows; write single-part geometries).
FLAG_TYPES = ("gap", "stub", "duplicate", "crossing", "missing?", "phantom?")
PENDING = "pending"
FLAG_DECISIONS = (PENDING, "real", "not_defect", "needs_qgis")



def is_pending_detector_output(flags):
    """Per flag row, whether it is pending detector output: ``pending`` and not
    saved by anyone. QGIS stamps ``annotator`` whenever someone saves a row."""
    return flags["decision"].eq(PENDING).fillna(False).astype(bool) & flags["annotator"].isna()


# Field -> pandas dtype, in layer order.
FLAG_FIELDS = {
    # Set by the detector and the same for the same suspected defect on every
    # run, so a re-run can match the flag it placed before.
    "flag_key": "string",
    "type": "string",
    "detector": "string",
    "source": "string",
    "raw_ids": "string",  # comma-separated raw_id values involved
    "proposal_fid": "Int64",  # fid of the flag's ``proposed`` row in labels, if it is a proposal
    "decision": "string",
    "annotator": "string",
}


def parse_raw_ids(value) -> list[int]:
    """A flag's ``raw_ids`` field as ``raw_id`` values, empty when it has none."""
    if pd.isna(value) or not str(value).strip():
        return []
    return [int(r) for r in str(value).split(",")]


# A table with no geometry, one row per detector run.
DETECTOR_RUN_FIELDS = {
    "run_at": "string",  # ISO 8601, UTC
    "config": "string",  # the run's full detector config, as JSON
}

# Classes an annotator may give an added road; the first is the default.
ADDED_ROAD_CLASSES = ("Other Road", "Main Road", "Street")

ROAD_SCALE, ROAD_BUFFER, _ = flatten_classification(CDNGI_ROAD_CLASSIFICATION)
