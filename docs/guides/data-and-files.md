# Data Description

Every file the guides use: what it is for, which steps need it, where it goes and where to get it.

Terms such as zone package, baseline and second source are defined in the [glossary](../glossary.md). On this page, `<repo>` is your clone of the repo, `<zones-dir>` is a folder for zone packages, and `<data-dir>` is a folder for the full data. You choose where each folder goes.

## Cleaning labels

You need these things:

- The repo, set up as in [Setup](setup.md).
- The zone package of each zone you claim. That is the `<zone>/` folder and the four files in it, the first five rows of the table below.
- An internet connection. QGIS loads the NGI aerial imagery from the web each time you open a zone.

## Regenerating package zones or changing a detector

Run `package-zones`, `detect` or `assign-zones`, as described in [Change a detector or the packager](change-detector-or-packager.md). In addition to the repo, it needs the full data, the remaining rows of the table below.

Use the same frozen baseline, roads parquet and zone imagery. Each road's `raw_id` comes from its geometry and road class. A zone package built from other inputs gets other `raw_id` values, and its cleaned labels no longer match the baseline.

## All files

| File | Purpose | Steps that need it | Where it goes | Where to get it |
| --- | --- | --- | --- | --- |
| `<zone>/` | The zone package, the folder that holds the next four files. Keep them together, because the QGIS project finds the other files by relative path. | All cleaning steps | `<zones-dir>/<zone>/` | The shared Drive. Ask kelviy for the link. See [File Downloads](claim-and-download.md). |
| `<zone>.gpkg` | The zone GeoPackage. It holds the raw labels, the labels you edit, the tiles, the flags and your decisions. You hand back this file. | Review, clean by hand, hand back | `<zones-dir>/<zone>/` | In the zone package |
| `<zone>.qgs` | The QGIS project. Open this file to work on the zone. | Review, clean by hand | `<zones-dir>/<zone>/` | In the zone package |
| `<zone>_s2_rgbnir.tif` | The zone's 2020 Sentinel-2 red, green, blue and near-infrared bands. QGIS shows the file as the Sentinel-2 RGB and Sentinel-2 false colour layers. | Checking a temporal mismatch | `<zones-dir>/<zone>/` | In the zone package |
| `<zone>_overture.gpkg` | The Overture roads, clipped to the zone. Use them to find where to look. Never copy them. | Checking `missing?` and `phantom?` flags | `<zones-dir>/<zone>/` | In the zone package, if the zone was packaged with `--overture` |
| `cdngi_dense.parquet` | The baseline roads parquet. The `roads` command made it from CDNGI, and `generate` built the baseline from it. | `generate`, `package-zones --roads` | `<data-dir>/cdngi_dense.parquet` | Ask kelviy for the files. |
| `overture_dense.parquet` | The Overture roads in the same columns. The second source that the `missing?` and `phantom?` flags are measured against, and the source of each zone's Overture layer. | `package-zones --overture`, `detect` through `overture.path` | `<data-dir>/overture_dense.parquet` | Ask kelviy for the files. |
| `ROSASampling60zones2020_images/` | The zone imagery. One 2020 Sentinel-2 image per zone, 60 in all, sorted into `train/`, `val/` and `test/`. | `generate`, `package-zones --imagery-dir` | `<data-dir>/ROSASampling60zones2020_images/` | Ask kelviy for the files. |
| Baseline dataset folder | The frozen baseline that `generate` wrote. `package-zones` reads its `tile_status/` and `generation_report.yaml`. `assign-zones` reads its `metadata.parquet`. Never regenerate or edit it. | `package-zones --dataset-dir`, `assign-zones --dataset-dir` | `<data-dir>/<baseline>/` | Ask kelviy for the files. |
| CDNGI road GeoPackages | The raw national road layer, one GeoPackage per province. You need them only to rebuild `cdngi_dense.parquet`. | `roads --cdngi` | `<data-dir>/<cdngi>/` | Ask kelviy for the files. |
| Overture roads GeoParquet | The raw Overture download. You need it only to rebuild `overture_dense.parquet`. | `roads --overture` | `<data-dir>/<overture>.parquet` | Ask kelviy for the files. |
| `detectors.yaml` | The detector thresholds and switches. Set `overture.path` to your copy of `overture_dense.parquet`. | `detect --detector-config` | `<repo>/src/sentinel2data/configs/detectors.yaml` | In the repo |
| Assignment CSV | The zone copies each annotator cleans, one row per copy, with `zone`, `split`, `annotator`, `published` and `shared`. | Written by `assign-zones --out` | Any folder | Kelviy runs `assign-zones`. See [File Downloads](claim-and-download.md). |
