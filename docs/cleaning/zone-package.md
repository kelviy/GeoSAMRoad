# Zone package

The **zone package** is the folder an annotator opens to clean one zone. It is built from the frozen baseline by `package-zones`:

```sh
python -m sentinel2data.cli package-zones \
  --roads cdngi_dense.parquet \
  --imagery-dir ROSASampling60zones2020_images \
  --dataset-dir <baseline dataset written by generate> \
  --output-dir <zones folder> \
  [--overture overture_dense.parquet] \
  [--zone <zone filename stem> ...] [--overwrite]
```

Use the same `--roads` and `--imagery-dir` that `generate` was run with. Road classes the baseline excluded (Track, Slipway) are read from its `generation_report.yaml`. Give `--overture` the same file as `overture.path` in the detector config, so the Overture layer in QGIS shows the roads that `missing?` and `phantom?` flags were measured against. Without it the zone has no Overture layer.

Each `<zone>/` folder holds:

| File | What it is |
| --- | --- |
| `<zone>.gpkg` | The zone GeoPackage: `raw_labels`, `labels`, `tiles`, `flags`, `detector_runs`, and `flag_deletions`, `review_position`, `proposal_rows` and `connector_vertices` once the review core first writes |
| `<zone>.qgs` | The QGIS project. Open this. |
| `<zone>_s2_rgbnir.tif` | The zone's Sentinel-2 red, green, blue and NIR bands, each enhanced (gamma + CLAHE) |
| `<zone>_overture.gpkg` | The Overture roads clipped to the zone, with `road_class`. Only with `--overture`. |

All paths in the project are relative, so the folder can be copied to another machine as a whole.

## Re-packaging

Re-packaging rebuilds a zone whose GeoPackage is unedited. It **refuses** a zone with any cleaning work: changed label status, defect, annotator or geometry, added rows, reviewed tiles, tile notes, or decided flags. `--overwrite` repackages such a zone anyway, after moving the old file aside as `<zone>.gpkg.<timestamp>.bak`. A zone open in QGIS is never repackaged: close it first.

Pending detector output is not cleaning work. That is every `pending` flag and every `proposed` road a pending flag points at, as long as no one has saved a change to it in QGIS. Repackaging drops it, and re-running the detectors brings it back.

`raw_id` depends only on each road's geometry and class, so it is the same every time a zone is packaged from the same baseline. A road that leaves and re-enters the zone is cut by the zone edge into pieces, and each piece is its own raw row.

## In QGIS

Layers, top to bottom:

* **Flags (points)** / **Flags (areas)**: two views of the `flags` table, filled by the detectors. Each type has its own colour and marker shape, and a label with its type. A flag decided `real` or `not_defect` turns grey. A `needs_qgis` flag keeps its colour.
* **Labels**: the layer you edit, coloured by `status`.
* **Raw labels**: the frozen baseline, read-only, drawn as a yellow casing under the labels.
* **Overture roads**: the second source, read-only, drawn as a line of white circles. Use it to find where to look, then draw from the aerial imagery. Snapping never uses this layer, and a road is never copied from it, because Overture is ODbL.
* **Tiles**: kept tiles outlined in cyan, `dropped_no_road` hatched, `dropped_nan` and `untiled` greyed out. Only edit inside kept tiles.
* **Sentinel-2 RGB** and **Sentinel-2 false colour**: hidden at start. Switch one on to check a temporal mismatch. False colour shows NIR as red, so vegetation is red and roads, bare ground and buildings are not.
* **NGI aerial**: the reference imagery (CDNGI aerial via `aerial.openstreetmap.org.za`).

Field behaviour in the labels layer:

* `status`, `defect` and `road_class` are drop-down lists of the allowed values.
* A newly drawn road starts as `status = added`, `road_class = Other Road`. `buffer` follows `road_class` (Other Road 5 m, Main Road 8 m, Street 4 m) and can't be typed.
* `raw_id` can't be edited. Splitting a road copies its `raw_id` to both pieces.
* `annotator` is filled with your QGIS user name (`@user_full_name`) whenever you save a change to a row. The same applies to tiles and flags.
* `road_class` of an existing road can't be changed (an enforced constraint): existing roads are never reclassified.
* **Never delete a row.** QGIS can't block the delete tool, so set `status = deleted` instead. The packager treats a missing row as cleaning work.
* `status` is **not** set for you when you move a raw road: set it to `modified` yourself.

## Review queue panel

The review queue plugin steps you through the zone's proposals, flags and kept tiles in a docked panel.

Install it once per machine, from the repo, with the Python that has the repo installed:

```sh
python -m sentinel2data.cli install-qgis-plugin [--profile-dir <QGIS profile folder>]
```

This copies the plugin into the QGIS `default` profile, next to the other plugins, and records that Python for the plugin to use. Run it again after updating the repo. Then, in QGIS:

1. Open **Plugins > Manage and Install Plugins**, select **Installed**, and enable **ROSA review queue**.
2. Check that your computer account has a full name. QGIS reads it from the operating system, and every decision records it. If it is missing, the panel says so.
3. Open the zone's `<zone>.qgs`. The panel docks on the right and goes to the first item not done, or to where you stopped. To show it again, use **Plugins > ROSA review queue > Review queue panel**.
4. Click the panel so it has focus, then press one key per item. The keys work only while the panel has focus:

| Item | Keys |
| --- | --- |
| Proposal | `A` accept, `X` reject, `Q` needs QGIS |
| Flag | `R` real, `N` not a defect, `Q` needs QGIS |
| Kept tile | type an optional note, then `T` or Enter for reviewed |
| Any | `B` back, `F` forward, `S` Sentinel-2 RGB on or off |

The map zooms to each item and selects it. The filters narrow the queue to one kind, flag type or source. To change a decision, go back to the item and press another key.

Draw fixes with the normal QGIS editing tools. You can edit between items: the panel saves your unsaved edits before it writes a decision. If QGIS cannot save them, the panel writes nothing and shows QGIS's errors.
