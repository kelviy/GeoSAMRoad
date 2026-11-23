# Zone package

The **zone package** is the folder an annotator opens to clean one zone. It is built from the frozen baseline by `package-zones`:

```sh
python -m sentinel2data.cli package-zones \
  --roads cdngi_dense.parquet \
  --imagery-dir ROSASampling60zones2020_images \
  --dataset-dir <baseline dataset written by generate> \
  --output-dir <zones folder> \
  [--zone <zone filename stem> ...] [--overwrite]
```

Use the same `--roads` and `--imagery-dir` that `generate` was run with. Road classes the baseline excluded (Track, Slipway) are read from its `generation_report.yaml`.

Each `<zone>/` folder holds:

| File | What it is |
| --- | --- |
| `<zone>.gpkg` | The zone GeoPackage: `raw_labels`, `labels`, `tiles`, `flags`, `detector_runs` |
| `<zone>.qgs` | The QGIS project. Open this. |
| `<zone>_s2_rgb.tif` | The zone's Sentinel-2 RGB, enhanced (gamma + CLAHE) |

All paths in the project are relative, so the folder can be copied to another machine as a whole.

## Re-packaging

Re-packaging rebuilds a zone whose GeoPackage is unedited. It **refuses** a zone with any cleaning work: changed label status, defect, annotator or geometry, added rows, reviewed tiles, tile notes, or decided flags. `--overwrite` repackages such a zone anyway, after moving the old file aside as `<zone>.gpkg.<timestamp>.bak`. A zone open in QGIS is never repackaged: close it first.

Pending detector output is not cleaning work. That is every `pending` flag and every `proposed` road a pending flag points at, as long as no one has saved a change to it in QGIS. Repackaging drops it, and re-running the detectors brings it back.

`raw_id` depends only on each road's geometry and class, so it is the same every time a zone is packaged from the same baseline. A road that leaves and re-enters the zone is cut by the zone edge into pieces, and each piece is its own raw row.

## In QGIS

Layers, top to bottom:

* **Flags (points)** / **Flags (areas)**: two views of the `flags` table, filled by the detectors.
* **Labels**: the layer you edit, coloured by `status`.
* **Raw labels**: the frozen baseline, read-only, drawn as a yellow casing under the labels.
* **Tiles**: kept tiles outlined in cyan, `dropped_no_road` hatched, `dropped_nan` and `untiled` greyed out. Only edit inside kept tiles.
* **Sentinel-2 RGB**: hidden at start. Switch it on to check a temporal mismatch.
* **NGI aerial**: the reference imagery (CDNGI aerial via `aerial.openstreetmap.org.za`).

Field behaviour in the labels layer:

* `status`, `defect` and `road_class` are drop-down lists of the allowed values.
* A newly drawn road starts as `status = added`, `road_class = Other Road`. `buffer` follows `road_class` (Other Road 5 m, Main Road 8 m, Street 4 m) and can't be typed.
* `raw_id` can't be edited. Splitting a road copies its `raw_id` to both pieces.
* `annotator` is filled with your QGIS user name (`@user_full_name`) whenever you save a change to a row. The same applies to tiles and flags.
* `road_class` of an existing road can't be changed (an enforced constraint): existing roads are never reclassified.
* **Never delete a row.** QGIS can't block the delete tool, so set `status = deleted` instead. The packager treats a missing row as cleaning work.
* `status` is **not** set for you when you move a raw road: set it to `modified` yourself.
