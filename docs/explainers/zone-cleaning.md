# Zone cleaning

How `sentinel2data.cli package-zones` turns the frozen baseline into one zone package per zone, the folder an annotator opens in QGIS to clean that zone.

> **Status, 2026-11-23.** This feature is in progress. The packager landed in commit `6a5f2b3` and KEL-9 is in review, with its manual QGIS check still open. KEL-47 added the flag key, the detector-run layer and the pending-output rule to the contract. KEL-48 added the `detect` command and its first detector, `stub`, described in [detectors.md](detectors.md), and KEL-49 made its re-runs idempotent. KEL-52 added the review core, described in [review-queue.md](review-queue.md). The other detectors, the QGIS review plugin and the assembler from KEL-5 and KEL-11 do not exist yet. The usage manual is [docs/cleaning/zone-package.md](../cleaning/zone-package.md).

## Core idea

Cleaning happens per zone, not per tile. For each zone, the packager writes one GeoPackage that holds two copies of the zone's roads. One copy is the raw labels, which stay frozen. The other is the labels layer, which annotators edit in place. Each road row in both copies carries the same `raw_id`, so every cleaned row traces back to at most one raw row. The packager also writes a QGIS project that opens the GeoPackage with styling, value lists and the reference imagery already set up. It refuses to rebuild a zone that already holds cleaning work. Pending detector output does not count as cleaning work, because a detector re-run regenerates it.

## How it works

1. **Find the zone.** `find_zones` reads the pre-split imagery folder the same way `generate` does and maps each zone filename to its split and COG path. A `--zone` name with no COG stops the command before any zone is packaged.
2. **Check the existing package.** If `<zone>.gpkg` already exists, `package_zone` first looks for a `-wal` file next to it. A `-wal` file means QGIS has the file open, so the zone is refused even with `--overwrite`. Then `zone_cleaning_work` lists any cleaning work in the file, ignoring pending detector output. Any work refuses the zone unless `--overwrite` is given. With `--overwrite`, the old file is renamed to `<zone>.gpkg.<timestamp>.bak` first.
3. **Rebuild the raw labels.** `raw_labels` reads the roads parquet inside the zone's bounding box, reprojects them and clips them to the zone. Then it drops the excluded classes. It reads the class list from the baseline's `generation_report.yaml`. It merges lines that the clip split at their own vertices and explodes multi-part lines into one row per piece. Then it sorts the rows by geometry and class and numbers them from 1 as `raw_id`.
4. **Write the GeoPackage.** `write_zone_gpkg` writes the five contract layers to a hidden `.tmp` file, which then replaces the real file:
   - `raw_labels` holds the rows from step 3.
   - `labels` copies them with `status = kept`, and `defect` and `annotator` empty.
   - `tiles` is the zone's `tile_status` table from the baseline, with `reviewed = False` and empty `note` and `annotator` added.
   - `flags` is created empty.
   - `detector_runs` is created empty. It is a table with no geometry.
5. **Write the Sentinel-2 RGB.** `write_s2_rgb` reads bands 1 to 3 of the zone COG and stretches the whole zone with the generator's `enhance_rgb` gamma and CLAHE. It writes the result as 8-bit, with 0 kept for no data.
6. **Write the QGIS project.** `write_zone_project` builds the `.qgs` XML directly, with no QGIS install needed.
7. **Report.** The CLI packages every zone it can. It prints each refusal and exits with code 1 at the end if any zone was refused.

## Details that matter

- **The contract.** `cleaning/contract.py` fixes the layer names and the allowed values. Label `status` is one of `kept`, `modified`, `added`, `deleted` or `proposed`. `defect` is one of `gap`, `stub`, `misalignment`, `phantom`, `missing` or `temporal`, or empty. Flag `type` is one of `gap`, `stub`, `duplicate`, `crossing`, `missing?` or `phantom?`. Flag `decision` is one of `pending`, `real`, `not_defect` or `needs_qgis`.
- **The flag schema.** `FLAG_FIELDS` in `contract.py` holds the flag field names, order and types: `flag_key`, `type`, `detector`, `source`, `raw_ids`, `proposal_fid`, `decision` and `annotator`. `empty_flags` builds the layer from it, and the QGIS project gives every field in it a config. Every field except `decision` and `annotator` is read-only in QGIS.
- **The flag key.** `flag_key` is a string the detector sets. It must be the same for the same suspected defect on every run, so a re-run can match the flag it placed before and leave a decided flag alone. The contract fixes only the field. [detectors.md](detectors.md) describes how the key is built and how a re-run uses it.
- **The detector-run layer.** `detector_runs` gets one row per detector run: `run_at`, an ISO 8601 UTC time, and `config`, the run's full detector config as JSON. `DETECTOR_RUN_FIELDS` in `contract.py` holds the schema.
- **`raw_id` depends only on the clipped geometry and the road class.** The sort key is the hex WKB of the line, then `road_class`. Shuffling the parquet rows gives the same `raw_id` values, and a test checks this. A road that leaves and re-enters the zone becomes one row per piece.
- **What counts as cleaning work.** `zone_cleaning_work` reports any of the following:
  - a row count or `raw_id` set in `labels` that differs from `raw_labels`, which catches both added and deleted rows
  - any `status` other than `kept`
  - any `defect` or `annotator` value
  - any label geometry that is not exactly equal to its raw geometry
  - any tile with `reviewed` set, or with a `note` or `annotator`
  - any flag whose `decision` is not `pending`, including an empty one
  - any flag with an `annotator`
  - a file it cannot read as a zone GeoPackage

  It ignores pending detector output. That is every `pending` flag with no `annotator`, and every `status = proposed` label row with no `annotator` that such a flag's `proposal_fid` points at. QGIS stamps `annotator` whenever someone saves a row, so a stamped row is human work even while its flag is pending. A `proposed` row whose flag is decided or missing still counts as a status other than `kept`. It never reads `detector_runs`, so zones packaged before that layer existed still repackage. It checks geometry only when no other label work was found. It does not check tile `status` or tile geometry.
- **Raw labels come from the parquet again, not from the baseline dataset.** The baseline directory supplies only the `tile_status` table and the excluded classes. If `generation_report.yaml` is missing, the packager falls back to `EXCLUDED_ROAD_CLASSES`, which is Footpath, Track and Slipway.
- **Columns that hold lists or dicts are dropped.** `_is_scalar` checks the first non-null value of each column. On CDNGI the road columns kept are `data_source`, `road_class`, `scale_class`, `buffer` and `surface`.
- **The Sentinel-2 RGB stretch covers the whole zone.** The generator stretches each tile on its own, so a pixel can look different in the zone RGB and in the tile imagery. NaN pixels are set to 0. The file uses 256 px deflate blocks and average overviews at 2, 4, 8 and 16.
- **The QGIS project encodes the editing rules.** Layers from top to bottom are Flags (points), Flags (areas), Labels, Raw labels, Tiles, Sentinel-2 RGB and NGI aerial.
  - The two flag layers read the one `flags` table, filtered by geometry type.
  - Raw labels are read-only and drawn as a 1.6 mm yellow casing.
  - A new label row defaults to `status = added` and `road_class = Other Road`.
  - The `road_class` drop-down offers only Other Road, Main Road and Street.
  - `buffer` and `scale_class` are read-only. A default expression that QGIS reapplies on every save recomputes both from `road_class`, using `CDNGI_ROAD_CLASSIFICATION`. Other Road gets 5 m, Main Road 8 m and Street 4 m.
  - A hard constraint makes a row with a `raw_id` keep its raw class. QGIS will not save a reclassified existing road.
  - `annotator` defaults to `@user_full_name` and updates on every save, in labels, tiles and flags.
  - Splitting a road copies its `raw_id` to both pieces. That is QGIS default behaviour, and `qgis_inspect.split_road` tests it.
  - The reference imagery is an XYZ layer from `aerial.openstreetmap.org.za`, up to zoom 20.
  - Every local path starts with `./`, so the folder works after it is copied to another machine.
- **Layer ids start with `rosa_zone_`.** QGIS throws away a saved layer id of 10 characters or fewer. The tree node then points at nothing, and the layer is missing from the panel.
- **A failed run leaves no half-written GeoPackage.** The `.tmp` swap ensures this. The `.tif` and `.qgs` are written in place and are always overwritten on a repackage.

## Why it's like this

- **One GeoPackage per zone, edited in plain QGIS.** KEL-5 chose GeoPackage because it is reliable in QGIS. KEL-9 requires the project to work with no plugin, so hand-cleaning can start before the review queue exists. Editing per zone keeps tile seams consistent, because the assembler will merge the zones into one road layer and rerun `generate`.
- **Frozen raw labels next to editable labels.** The paper compares raw labels with cleaned labels, so every raw row must survive. That is also why a deletion is `status = deleted` and never a removed row. KEL-5 user story 19 sets this rule.
- **Splits keep `raw_id` and rows never merge.** This keeps each cleaned row traceable to at most one raw row, as KEL-5 user stories 20 and 21 require.
- **`raw_id` from geometry and class.** The `raw_labels` docstring says this keeps `raw_id` independent of the parquet's row order. KEL-9 asks for `raw_id` to stay stable across repackaging.
- **No reclassification of existing roads.** KEL-5 puts class fixing out of scope, so it does not eat the time budget of about 2 hours per zone.
- **The refusal rule.** KEL-9 requires that cleaning work cannot be destroyed by accident. The `-wal` check exists because QGIS holds an open GeoPackage in WAL mode, and unsaved edits can sit in that file. GDAL deletes the `-wal` file when it closes the GeoPackage, so the check runs before the packager reads the file.
- **Pending detector output is not cleaning work.** [KEL-23](https://linear.app/kelvinwei/issue/KEL-23) asked why a `pending` flag used to refuse the zone, and found no reason. KEL-47 picked option A. Detector output is cheap to regenerate, so it should not need `--overwrite`. Reserving `--overwrite` for human work keeps the option meaningful. Decided flags are human work, so they still refuse the zone.
- **The `.qgs` is hand-written XML, not built with PyQGIS.** Reason unknown, see [KEL-24](https://linear.app/kelvinwei/issue/KEL-24).
- **Raw labels are rebuilt from the roads parquet, not read from the baseline dataset.** Reason unknown, see [KEL-25](https://linear.app/kelvinwei/issue/KEL-25).
- **Flags carry only a location.** Overture and OSM are ODbL, and copying their geometry would make ROSA a derivative database. KEL-5 and [open directions](../cleaning/open-directions.md) record this. The labels-only detectors write a point. The `overture` detector writes a rounded bounding box, never Overture geometry. See [detectors.md](detectors.md).
- **The reference imagery decides position, and 2020 Sentinel-2 decides a clear temporal mismatch.** See [ADR 0001](../adr/0001-aerial-authority-with-temporal-override.md). This is why the project ships both the NGI aerial layer and the hidden Sentinel-2 RGB, and why `temporal` is a defect value.

## How detectors fit in

`detect` reads the zone GeoPackage and writes pending rows into `flags`, and records each run in `detector_runs`. [detectors.md](detectors.md) explains it. A zone whose only changes are `detect` output repackages without `--overwrite`, and a test checks this. A proposal is a flag whose `proposal_fid` points at a `status = proposed` row in `labels`. The gap detector writes them, and the QGIS project styles `proposed` as a dashed magenta line. `detect` refuses a zone packaged before KEL-47, so zones packaged earlier, such as `~/Documents/ROSAZones_v2`, need repackaging first. The written cleaning spec that annotators follow is KEL-7, in the backlog.

## Weak spots and ideas

- **`raw_id` is fragile to any upstream change.** Observed. `raw_id` is a rank, not a hash, so adding or removing one road in the zone renumbers every row after it. A pyproj, GEOS or GDAL version change that moves a clipped coordinate by one bit changes the sort key too. A content hash would survive both, at the cost of a less readable id.
- **Nothing ties the packaged raw labels to the frozen baseline.** Observed. The packager trusts that `--roads` and `--imagery-dir` are the ones `generate` used. A different or edited parquet gives raw labels that no longer match the tiles, and no check catches it. The generation report could store a hash of the parquet, and the packager could compare it.
- **Human work on pending output rests on the `annotator` stamp.** Observed. Repackaging drops pending detector output with no backup, and a detector re-run brings it back. An edit to a pending flag or proposal survives only because QGIS stamps `annotator` on save. A tool that edits the GeoPackage without setting `annotator` would lose its edits to pending rows.
- **Moving a road does not set `status = modified`.** Observed. The packager detects the move as cleaning work. A future assembler that counts revisions by `status` would miss it.
- **A missing `tile_status` table aborts the whole run.** Observed. `package_zone` raises `FileNotFoundError`, and the CLI catches only the two refusal errors. One unbaselined zone stops the zones after it.
- **The `.qgs` is always overwritten.** Observed. A layer an annotator added, or a style they changed, is lost on repackage, and nothing reports it.
- **The `road_class` drop-down lists only the three added classes.** Suspected. An existing National Road shows a value that is not in its own drop-down. It may look like an error to the annotator.
- **The whole zone is held in memory as float32 for the RGB.** Suspected. CLAHE then runs over the full array. The largest zones have not been measured.
- **The label and tile schemas still live in several places.** Observed. The flag and detector-run schemas are in `contract.py`. The label and tile fields are not: `editable_labels` and `tile_grid` in `packager.py` hold their names and order, and `qgis_project.py` repeats them in its field configs. The tests repeat every field list as literals, which is fine for a black-box test.
- **Cross-module coupling into the generator.** Observed. The packager imports `PreSplitImagerySource` from `generator/pipeline.py` and `enhance_rgb` from `generator/helper.py`. It also re-implements the generator's zone road loading by calling `load_zone_roads` and `exclude_road_classes` itself.
- **`write_zone_project` does too much.** Observed. One function of about 125 lines builds every layer, renderer, field config and canvas setting. A function per layer would make each one testable and easier to change.
- **`zone_cleaning_work` mixes rules and messages.** Observed. It returns strings, so callers cannot tell which kind of work was found. A small result type would let the CLI or a future assembler act on it.

## Code map

- `src/sentinel2data/cli.py`, `package_zones`: the CLI entry point. It loops over zones and collects refusals.
- `cleaning/contract.py`: layer names, allowed values, `FLAG_FIELDS`, `DETECTOR_RUN_FIELDS`, the added road classes, and `ROAD_BUFFER` and `ROAD_SCALE` from `CDNGI_ROAD_CLASSIFICATION`.
- `cleaning/packager.py`, `package_zone`: packages one zone, with the refusal check and the `.tmp` swap.
- `cleaning/packager.py`, `raw_labels`: rebuilds the zone's raw labels and assigns `raw_id`.
- `cleaning/packager.py`, `editable_labels`, `tile_grid`, `empty_flags` and `write_zone_gpkg`: build and write the five layers.
- `cleaning/packager.py`, `zone_cleaning_work` and `is_open_elsewhere`: decide whether a zone is safe to replace.
- `cleaning/packager.py`, `write_s2_rgb`: writes the zone's 8-bit Sentinel-2 RGB.
- `cleaning/qgis_project.py`, `write_zone_project`: writes the `.qgs`. `_road_fields` and `_road_class_rule` hold the class and buffer rules.
- `tests/sentinel2data/test_package_zones.py`: end-to-end tests on the synthetic zone, covering the contract, `raw_id` stability, refusal, the RGB and the project.
- `tests/sentinel2data/qgis_inspect.py`: loads the project in a real QGIS 3 Python, outside the repo venv, and reports what an annotator would see. The tests skip it when no QGIS Python is found.
