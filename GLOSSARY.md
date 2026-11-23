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

**Baseline**:
The dataset generated once from the source road layer, with the excluded classes removed, and frozen. Every raw versus cleaned comparison is made against it.
_Avoid_: Original dataset, v1

**Road class**:
The source layer's own category for a road, such as Main Road or Street. It sets how wide the road is drawn in the road mask.
_Avoid_: Road type, category

**Buffer**:
The half-width, in metres, that a road line is widened by before it becomes road pixels. It depends on the road class.
_Avoid_: Road width, thickness

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

**Zone package**:
The folder an annotator opens to clean one zone: the zone's GeoPackage, its QGIS project and its Sentinel-2 RGB.
_Avoid_: Zone bundle, workspace

**Cleaning work**:
Any change an annotator or detector has made to a zone package since it was packaged. Cleaning work is never overwritten by accident.
_Avoid_: Edits, progress

## Imagery

**Composite**:
One image made by combining many satellite scenes over a date window, pixel by pixel, to remove clouds and gaps.
_Avoid_: Mosaic, scene

**Band layout**:
The fixed order and names of the bands in a tile. Models find each band by its name in the layout.
_Avoid_: Band order, channels

**Enhanced RGB**:
The red, green and blue bands contrast-stretched for viewing and for RGB-only models, stored in every tile beside the raw bands.
_Avoid_: True colour, visual bands

## Road graphs

**Road mask**:
A per-pixel image of where roads are, either burned from road labels or predicted by a model as a probability.
_Avoid_: Segmentation, label image

**Keypoint**:
A point on the road network where a road ends or meets other roads.
_Avoid_: Junction, node, intersection

**Road graph**:
The roads of a tile as points joined by straight edges, the form in which connectivity is measured.
_Avoid_: Network, topology, vector

**Label graph**:
The road graph built from a tile's labels, which a predicted graph is scored against.
_Avoid_: Ground truth graph, GT graph

**Predicted graph**:
The road graph a model produces for one tile.
_Avoid_: Output graph, proposal

**Road density**:
The fraction of a tile's pixels that are road in its labels.
_Avoid_: Coverage, road ratio

## Models and evaluation

**Crop**:
A square window cut from a tile, the unit a model sees in one sample.
_Avoid_: Patch, tile

**Variant**:
One road model, defined by its backbone and input bands, such as SAM with RGB or TerraMind with multispectral input.
_Avoid_: Model type, flavour

**Final run**:
A training run on the train and val splits together, producing the model whose test scores are reported.
_Avoid_: Production run, full run

**TOPO**:
A road graph metric that matches points sampled along nearby roads in the predicted and label graphs, giving precision and recall of local connectivity.

**APLS**:
A road graph metric that compares shortest-path lengths between matched point pairs in the predicted and label graphs, scoring route connectivity.

## Web app

**Cell**:
One square of the web app's global selection grid, the area of one image fetched on demand. A cell is not a tile.
_Avoid_: Tile, square
