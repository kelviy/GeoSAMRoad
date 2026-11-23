# Detectors

How `sentinel2data.cli detect` runs the detectors over packaged zones and writes their flags into each zone GeoPackage.

> **Status, 2026-11-23.** KEL-48 built the runner and the first detector, `stub`. The gap, duplicate, crossing and second-source detectors come later, as do idempotent re-runs, which are KEL-49, and the review queue. [zone-cleaning.md](zone-cleaning.md) describes the zone package itself.

## Core idea

A detector is a model-agnostic check that reads only the zone GeoPackage and one config file, and places flags at suspected defects. `detect` runs every enabled detector on each zone and writes the flags into the zone's `flags` layer with `decision = pending`, for an annotator to decide on. Detectors place flags only inside or touching `kept` tiles, and they ignore road ends that dangle only because the zone edge cut the road. Each run is recorded in the GeoPackage with the full config it used.

## How it works

1. **Load the config.** `DetectorConfig.load` reads `src/sentinel2data/configs/detectors.yaml`, or the file given with `--detector-config`. The file must hold exactly the expected keys with the expected types. A missing, unknown or mistyped key stops the command before any zone runs.
2. **Pick the zones.** `find_packaged_zones` lists every `<zone>/<zone>.gpkg` under `--zones-dir`. `--zone` picks some of them. An unknown name stops the command.
3. **Check the zone.** `detect_zone` refuses a zone that has a `-wal` file, because QGIS has it open. It also refuses a zone packaged before KEL-47: the `flags` fields must match `FLAG_FIELDS`, and the `detector_runs` layer must exist. A refused zone is skipped, the other zones still run, and the command exits with code 1 at the end.
4. **Read the zone.** `load_zone` builds a `Zone`. Its roads are the label rows with status `kept`, `modified` or `added`, one 2D LineString each. It also holds the union of the `kept` tiles and the zone outline, which is the bounding box of the tile grid.
5. **Run the detectors.** Each enabled detector in `DETECTORS` returns its flags: `flag_key`, `type`, `source`, `raw_ids` and a geometry. The runner adds `detector`, `decision = pending` and empty `proposal_fid` and `annotator`.
6. **Write the flags.** `_replace_pending_flags` deletes the flags these detectors placed before that are still pending and have no `annotator`. Then it appends the new flags. It skips a new flag whose `flag_key` matches a flag it kept, or another new flag.
7. **Record the run.** `_record_run` appends a row to `detector_runs`: `run_at` in UTC and the config as JSON.
8. **Report.** The CLI prints the flag count per type for each zone, then a line of totals, for example `All zones: stub 1`.

## The stub detector

A stub is a dead-end branch shorter than `stub.max_length_m`, which is provisionally 30 m. The flag is a point at the dead end. The glossary calls this defect a dangling stub.

- **Branch.** `branches` line-merges the roads wherever exactly two road ends meet, so a road stored as several rows is one branch. The 30 m is measured along the branch, not along one row. The merge needs the two ends to share exact coordinates.
- **Dead end.** No other branch lies within `connect_tolerance_m` of the end, 0.5 m by default. The branch's own line doesn't count. `dead_ends` finds them with one STRtree `dwithin` query over every branch start and end.
- **Edge-cut branches are skipped.** The detector skips a branch if either end lies within `zone_edge_tolerance_m` of the zone outline, 1 m by default. The packager clipped it there, so its real length is unknown.
- **Scope.** The dead end must lie inside or touch a `kept` tile. Dead ends are still worked out against every road, so a short road that meets a road in a dropped tile is not a stub.
- **Where the flag goes.** It goes on the dead end. An isolated branch has two dead ends and gets one flag, at its start.
- **Flag key.** `stub:<x>,<y>`, the dead end's coordinates rounded to the metre.
- **`raw_ids`.** The sorted `raw_id` values of the rows in the branch. Rows with no `raw_id`, which are `added` rows, are left out. A row is matched to a branch when the branch covers it, so a duplicate row can appear in the `raw_ids` of its copy's branch too. On the 60 real zones, 65 of 313,964 rows match more than one branch, and every one is an exact or overlapping duplicate.
- **`source`.** `labels`.

## Details that matter

- **The config is the only place for thresholds.** The dataclasses in `detectors.py` have no defaults. Every value comes from the YAML, so the file the run records is the whole truth about that run.
- **`detector_runs` records every run,** even one with every detector disabled.
- **The counts are what the detectors found this run.** A found flag whose key matches a decided flag, or another found flag, is counted but not written.
- **`deleted` and `proposed` rows are not roads.** A pending gap connector does not connect anything for the stub detector.
- **Multi-part label geometries are split** into one road per part, all with the same labels fid.
- **Flags are 2D.** Labels from a real zone are `LineString Z`. `load_zone` drops the Z, so every flag point is 2D.
- **Deleting pending flags uses plain SQLite.** The GeoPackage delete triggers are plain SQL, so the R-tree and feature count stay right. Inserts go through GDAL, because the insert triggers call GDAL's `ST_*` functions.

## Why it's like this

- **Model-agnostic, GeoPackage-only input.** KEL-11 user story 13 keeps cleaned test labels from being biased towards the project's own model. The tests run `detect` on a zones folder holding only the GeoPackages.
- **One config, recorded per run.** KEL-11 user story 14: tune without code changes, and know which settings produced each flag.
- **Kept tiles only, and no edge-cut ends.** KEL-11 user stories 7 and 6: flags never point where annotators are told not to edit, and clipping artefacts don't flood the queue. The check is on the flag point, not only the road, because a road that touches a kept tile can have its dead end in a dropped one.
- **Vectorised STRtree queries.** The KEL-11 dry run found that per-segment loops with geometry unions don't finish on dense urban zones. With these queries, `detect` runs all 60 zones in 16 s, placing 1,532 stub flags.
- **Replace pending flags, keep decided ones.** KEL-48 allows a re-run to replace pending flags as long as it never touches a decided flag. KEL-49 makes re-runs fully idempotent.

## Weak spots and ideas

- **A location key moves with the road.** Observed. If an annotator edits a stub's road, its dead end moves, and the next run's flag gets a new key. A decided flag then sits next to a new pending flag for the same stub. A key that included the `raw_id` would survive small edits, but not splits, which share a `raw_id`.
- **Rounding to the metre can merge or split keys.** Observed for merging: two dead ends in the same 1 m cell get the same key, and only the first is written. A coordinate that sits on a .5 m boundary rounds the same way on every run, but a sub-millimetre change can flip it.
- **The zone outline is a box.** Observed. It is the tile grid's bounding box, which equals the zone COG bounds. Suspected: if a zone's valid data is not rectangular, for example a NaN-padded edge, roads would end at the data edge, not at the box, and the detector would flag those ends as dead ends.
- **A near-miss join splits a branch.** Suspected. Two rows whose ends are 0.3 m apart count as connected for `dead_ends`, but `line_merge` does not join them. A long road stored that way is checked row by row, so its short last row can still be flagged.
- **A loop counts as dead at both ends.** Observed. A closed road whose start equals its end touches only itself. If it is shorter than the threshold, it gets a stub flag.
- **The connect tolerance is very tight.** Suspected. 0.5 m treats a road that stops 1 m short of another as a dead end. That is right for a stub but means near-misses show up as stubs until the gap detector exists.
- **Flags a re-run replaces are deleted and re-inserted.** Observed. Their fids change on every run. Nothing holds a flag fid today, but a review queue that stores its position by fid would break.
- **Removing a pending proposal does not remove its `proposed` row.** Observed. No detector writes proposals yet. The gap detector must delete the row with its flag.

## Code map

- `src/sentinel2data/cli.py`, `detect`: the CLI entry point. Loops over zones, collects refusals and prints counts.
- `src/sentinel2data/configs/detectors.yaml`: the default thresholds and toggles.
- `cleaning/detectors.py`, `DetectorConfig`: the config schema and its strict YAML parser.
- `cleaning/detectors.py`, `Zone`, `DETECTORS`: what a detector sees, and the name-to-detector registry. The registry key must match a `DetectorConfig` field.
- `cleaning/detectors.py`, `branches`, `dead_ends` and `stub_flags`: the stub detector.
- `cleaning/contract.py`, `is_pending_detector_output`: the rule that the runner and the packager share.
- `cleaning/detect.py`, `detect_zone`: runs one zone, with the open-file and schema checks.
- `cleaning/detect.py`, `load_zone`, `_as_flag_rows`, `_replace_pending_flags` and `_record_run`: GeoPackage reads and writes. `_as_flag_rows` sets `decision = pending`.
- `tests/sentinel2data/test_detect.py`: end-to-end tests on the packaged synthetic zone.
