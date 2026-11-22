"""The zone GeoPackage contract shared by the packager, detectors, review core
and assembler (see the KEL-5 spec and ``GLOSSARY.md``)."""
from sentinel2data.generator.config import CDNGI_ROAD_CLASSIFICATION, flatten_classification

RAW_LABELS_LAYER = "raw_labels"
LABELS_LAYER = "labels"
TILES_LAYER = "tiles"
FLAGS_LAYER = "flags"

# labels
LABEL_STATUSES = ("kept", "modified", "added", "deleted", "proposed")
DEFECTS = ("gap", "stub", "misalignment", "phantom", "missing", "temporal")

# flags: point or polygon locations (the QGIS project shows Point and Polygon
# rows; write single-part geometries).
FLAG_TYPES = ("gap", "stub", "duplicate", "crossing", "missing?", "phantom?")
FLAG_DECISIONS = ("pending", "real", "not_defect", "needs_qgis")
# Flag fields: type, detector, source, raw_ids (comma-separated raw_id values
# involved), proposal_fid (fid of the flag's ``proposed`` row in labels, when
# the flag is a proposal), decision, annotator.

# Classes an annotator may give an added road; the first is the default.
ADDED_ROAD_CLASSES = ("Other Road", "Main Road", "Street")

ROAD_SCALE, ROAD_BUFFER, _ = flatten_classification(CDNGI_ROAD_CLASSIFICATION)
