# ROSA

ROSA is a road-extraction dataset of Sentinel-2 imagery over South Africa, with vector road labels. This context covers how the dataset is built, cleaned and used to train and evaluate road models.

## Dataset structure

**Zone**:
One of the 60 sampled areas of South Africa. Each zone is a single scene that is cut into tiles, and every zone belongs to exactly one split.
_Avoid_: Region, area, sample

**Tile**:
A 512×512 px (10 m) square cut from a zone, together with its labels. It is the unit the dataset ships in.
_Avoid_: Image, patch, chip

**Split**:
The train, val or test partition. Splits are assigned per zone, so all tiles of a zone share a split.

## Labels

**Road**:
A path that a car can drive along, paved or unpaved. Hiking trails and footpaths are not roads, and neither are faint vehicle tracks (two wheel ruts with no cleared surface).
_Avoid_: Track, trail, path

**Raw label**:
The road label as generated automatically from the source road layer (with the excluded classes, track and slipway, removed), before any human edit. It is frozen as the baseline for cleaning and is never modified.
_Avoid_: Original label, ground truth

**Cleaned label**:
The road label after the raw label has been reviewed and corrected under the cleaning spec.
_Avoid_: Fixed label, ground truth

**Label revision**:
One attributable change from a raw label towards a cleaned label. It records who made it, when, and which defect it corrects.
_Avoid_: Edit, fix, correction

## Cleaning

**Defect**:
A disagreement between a label and the reference imagery that the cleaning spec says to correct. Classes: gap, dangling stub, misalignment, phantom road, missing road.
_Avoid_: Error, issue, problem

**Reference imagery**:
The high-resolution aerial basemap used to decide whether a road exists and where it lies. It is used for review only and is not part of the dataset.
_Avoid_: Basemap, high-res, ground truth

**Temporal mismatch**:
A road that clearly exists in one of the 2020 Sentinel-2 imagery and the reference imagery and is clearly absent in the other, because the two were captured at different times. The 2020 Sentinel-2 imagery decides whether the road exists.
_Avoid_: Date conflict, outdated label

**Detector**:
An automated, model-agnostic check that flags a suspected defect and proposes a label revision for a human to accept, reject or adjust.
_Avoid_: Checker, rule, triage

**Flag**:
A marker a detector places at a suspected defect location, possibly involving several roads or none (a missing road). An annotator decides whether it is a real defect. A flag never changes a label by itself.
_Avoid_: Issue, warning, annotation

**Proposal**:
A flag that also carries a suggested label revision (e.g. a connector line closing a gap), which an annotator accepts or rejects.
_Avoid_: Suggestion, auto-fix

**Second source**:
An independent road layer (e.g. Overture, OpenStreetMap) used to point annotators at suspected defects. Its geometry is not copied into labels.
_Avoid_: Reference layer, alternative labels

**Review queue**:
The ordered list of detector proposals, flags and kept tiles that an annotator works through one at a time, deciding on each. Anything that needs drawing is passed on to QGIS.
_Avoid_: Triage, worklist

**Cleaning spec**:
The written rules annotators follow when deciding what counts as a defect and how to correct it. It is also the methodology reported in the paper.
_Avoid_: Guidelines, rules, SOP
