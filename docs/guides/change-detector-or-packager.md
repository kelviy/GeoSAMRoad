# Change a detector or the packager

Change how zones are flagged or packaged, check the change, and regenerate zone packages without losing cleaning work.

This is the secondary path. kelviy packages the zones and runs the detectors centrally, and most teammates only download a ready zone package. Follow this page only if you want to change a detector or the packager yourself.

Terms such as zone package, flag key, cleaning work and pending detector output are defined in the [glossary](../glossary.md).

!!! warning "Never use `uv run` or plain `uv sync`"
    Both sync `.venv` to the default dependency groups only. That uninstalls the extra groups, such as torch and terratorch, from `.venv`. Run every command on this page with the venv's own programs, for example `.venv/bin/python` and `.venv/bin/ruff`.

## What you need

You need the full data, not only a zone package. The [Data & files](../guides/data-and-files.md) page lists each file and where to get it. Packaging reads these four inputs:

- The roads parquet that `generate` was run with, for example `<data-dir>/cdngi_dense.parquet`.
- The pre-split imagery root that `generate` was run with. It holds `train/`, `val/` and `test/`, one zone COG per zone.
- The baseline dataset directory that `generate` wrote. The packager reads its `tile_status/` tables and its `generation_report.yaml`.
- The Overture roads parquet, for example `<data-dir>/overture_dense.parquet`. The packager needs it only with `--overture`. The `overture` detector always needs it unless you disable that detector.

Use the same roads parquet and imagery root that `generate` used. The packager does not check this, and a different parquet gives raw labels that no longer match the tiles.

You also need the repo and its `.venv`, set up as the [Setup](setup.md) guide describes.

## Package a zone

Run `package-zones` from `<repo>`:

```sh
.venv/bin/python -m sentinel2data.cli package-zones \
  --roads <data-dir>/cdngi_dense.parquet \
  --imagery-dir <data-dir>/<imagery root> \
  --dataset-dir <data-dir>/<baseline dataset> \
  --output-dir <data-dir>/<zones folder> \
  --overture <data-dir>/overture_dense.parquet \
  --zone <zone filename stem>
```

- `--zone` takes a zone filename stem. Repeat it to package several zones. Leave it out to package every zone.
- `--overture` adds a read-only Overture roads layer to each zone, written as `<zone>_overture.gpkg`. Give it the same file as `overture.path` in the detector config, so the layer shows the roads that `missing?` and `phantom?` flags were measured against. Without `--overture`, the zone has no Overture layer, and the packager deletes an old `<zone>_overture.gpkg`.

The command prints `Packaged <zone> -> <folder>` for each zone. Each zone folder holds `<zone>.gpkg`, `<zone>.qgs`, `<zone>_s2_rgbnir.tif` and, with `--overture`, `<zone>_overture.gpkg`. A `--zone` name with no zone COG stops the command before it packages anything.

A new package has no flags. Run `detect` on it next, as [Tune a detector](#tune-a-detector) shows.

## Repackage safely

The packager refuses to replace a zone in two cases:

- **The zone holds cleaning work.** Cleaning work is any human change in the GeoPackage. Examples are a changed label `status`, `defect`, `annotator` or geometry, an added or removed row, a reviewed tile, a tile note, or a decided flag.
- **QGIS has the zone open.** A `<zone>.gpkg-wal` file next to the GeoPackage means some program has it open. The packager refuses such a zone even with `--overwrite`. Close the zone in QGIS and run the command again.

Pending detector output is not cleaning work. That is every `pending` flag, and every `proposed` road that such a flag points at, while no one has saved a change to it in QGIS. Repackaging drops it. Run `detect` again to bring it back.

For each refused zone the command prints `Refused <zone>: <reason>`. It packages the other zones, then exits with code 1.

!!! warning "`--overwrite` replaces the zone"
    With `--overwrite`, the packager repackages a zone that holds cleaning work. It first renames the old file to `<zone>.gpkg.<timestamp>.bak`, for example `<zone>.gpkg.20261123T153000.bak`. The new GeoPackage has none of the old cleaning work. No command copies work from a `.bak` file back into a zone. Do not use `--overwrite` on a zone that a teammate is cleaning or has handed back.

The packager always rewrites `<zone>.qgs` and `<zone>_s2_rgbnir.tif`. A layer or style that someone added to the project is lost.

`raw_id` depends only on each road's clipped geometry and its road class. Repackaging from the same baseline gives the same `raw_id` values.

## Tune a detector

Every detector threshold and toggle is in `src/sentinel2data/configs/detectors.yaml`. The file has one section per detector, `gap`, `stub`, `duplicate`, `crossing` and `overture`, plus two shared tolerances. Each section has `enabled` and `hidden`. `enabled` runs the detector. `hidden` still writes its flags, but leaves them out of the review queue.

1. Copy the default config, for example to `<data-dir>/detectors.yaml`.
2. Set `overture.path` to your copy of the Overture parquet. `~` is expanded.
3. Change the thresholds you want to tune.
4. Close every zone you want to run on in QGIS.
5. Run `detect`:

    ```sh
    .venv/bin/python -m sentinel2data.cli detect \
      --zones-dir <data-dir>/<zones folder> \
      --detector-config <data-dir>/detectors.yaml \
      --zone <zone filename stem>
    ```

    Without `--detector-config`, `detect` reads the default file. Without `--zone`, it runs on every zone under `--zones-dir`.

6. Read the counts. `detect` prints the flags found per type for each zone, then a line of totals, for example `All zones: crossing 1, duplicate 1, stub 1`.

The config must hold exactly the expected keys with the expected types. A missing, unknown or mistyped key stops `detect` before it runs on any zone. A missing Overture file stops it too, unless `overture.enabled` is `false`.

`detect` refuses a zone that QGIS has open, and a zone packaged before the current flag contract. It runs the other zones and exits with code 1. Repackage an outdated zone first.

A re-run never loses review work. `detect` matches this run's flags against the stored flags by flag key:

- A decided flag never changes and is never removed, even if the new config no longer finds it.
- A pending flag found again stays as it is, with the same fid.
- A pending flag not found again is deleted, with its `proposed` road.
- A new flag is added as `pending`, unless a stored flag already has its key.

So a second run with the same config changes nothing. A tighter threshold removes only pending flags. A looser threshold only adds flags. A disabled detector keeps its pending flags. Each run appends one row to the zone's `detector_runs` layer with the time and the full config it used.

The review queue reads `hidden` from the zone's latest run. After you change `hidden`, run `detect` again.

## Add a detector

A detector is a function that reads one zone and the config and returns its flags. The [detectors explainer](../explainers/detectors.md) describes the detectors that exist and how the runner merges their flags. All the code named below is in `src/sentinel2data/cleaning/`.

1. **Write the function** in `detectors.py`, with the `Detector` signature `(zone: Zone, config: DetectorConfig) -> gpd.GeoDataFrame`. A `Zone` holds:
    - `roads`, the label rows with status `kept`, `modified` or `added`, one 2D LineString each, indexed by labels fid, with their `raw_id`.
    - `kept_area`, the union of the kept tiles. Use `zone.in_kept_tiles` to keep only flags that lie inside or touch a kept tile.
    - `boundary`, the zone outline. Use `zone.on_boundary` to skip ends that the zone edge cut.

    Return one row per suspected defect, with the columns `flag_key`, `type`, `source`, `raw_ids` and a geometry. `source` is `labels` or `overture`. The geometry is a point, or a single-part polygon for an area. Add a `proposal` column with a connector line only if the detector proposes a fix, as `gap_flags` does. Build the rows with `_flag_rows`, or `_flags_from_labels` when the detector and the flag type share a name. The runner adds `detector`, `decision = pending` and the empty fields.

2. **Make the flag key stable.** Build every key with `flag_key`. It gives `<detector>:<type>:<raw_ids>@<x>,<y>`, with the location rounded to the metre. The same roads must give the same key on every run, or a re-run treats a decided flag as gone and adds a pending twin next to it. Do not put a fid, a run time, a row order or a random value in the key or in the flag point. Take `raw_ids` from the roads the flag names.

3. **Add its config.** Write a frozen dataclass with `enabled: bool`, `hidden: bool` and one field per threshold. Give no field a default, because every value must come from the YAML. Add it to `DetectorConfig` under the detector's name.

4. **Add its section** to `src/sentinel2data/configs/detectors.yaml`, with every field. The parser rejects a config that lacks the section, so tell teammates to add it to their own copies too.

5. **Register it** in `DETECTORS` at the end of `detectors.py`. The registry key must match the `DetectorConfig` field. The same name is the detector's config section and the `detector` field of its flags.

6. **Add a new flag type, if it has one.** Add the type to `FLAG_TYPES` in `contract.py`. Add its style to `FLAG_TYPE_STYLES` in `qgis_project.py`, with `Point` or `Polygon` to pick the flag layer. The module fails to import if a type has no style. If `real` on the flag should delete roads, add the type to `DELETING_FLAG_TYPES` in `review.py`.

7. **Write the tests.** Test the detector end to end on the packaged synthetic zone, as `tests/sentinel2data/test_geometry_detectors.py` does. Check that two runs with the same config leave the flags unchanged.

## Check your change

Run each check from `<repo>`:

1. Run the tests:

    ```sh
    .venv/bin/python -m pytest tests/sentinel2data
    ```

    The QGIS tests run only when they find a Python that can import `qgis.core`. Set `QGIS_PYTHON` to that Python to run them. Otherwise they are skipped.

2. Run ruff, then pyright, on the files you changed:

    ```sh
    .venv/bin/ruff check <paths>
    .venv/bin/ruff format <paths>
    .venv/bin/pyright <paths>
    ```

3. If you changed the docs, build the site:

    ```sh
    .venv/bin/python tools/build_docs.py
    ```

    It fails on a broken link or anchor, a local path, a Google Drive or Docs link, or a credential.

In the same change, update the explainer for the feature: [detectors](../explainers/detectors.md) for a detector, [zone cleaning](../explainers/zone-cleaning.md) for the packager. If you changed a command, a flag or a file that this page names, update this page too.

## Measure detector precision

Detector precision is `real / (real + not_defect)` for one detector and flag type. Read it after annotators have reviewed some flags:

```sh
.venv/bin/python -m sentinel2data.cli precision \
  --zones-dir <data-dir>/<zones folder> \
  --zone <zone filename stem>
```

Without `--zone`, the report covers every zone. It prints one line per zone, detector and flag type, for example:

```text
Z: stub:stub 66.7% (2 real / 3 judged), needs_qgis 1, pending 4
```

Then it prints the same lines for all the zones together, labelled `All zones`, or `N selected zone(s)` under `--zone`. The overall lines add up the counts over the zones before they divide, so a zone with many flags counts for more.

- The share leaves out `needs_qgis` and `pending` flags. The report prints their counts, so you can recompute the share either way.
- Hidden detectors are counted. The report reads every flag in the zone.
- A flag does not record the config that placed it. If you tuned a detector between reviews, one share mixes flags from both configs.
- A `decision` value outside `pending`, `real`, `not_defect` and `needs_qgis` stops the report with exit code 1.

Use the shares to decide which detectors to tune, or to mark `hidden`.
