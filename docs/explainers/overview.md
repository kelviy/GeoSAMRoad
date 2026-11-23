# Overview

How the parts of the repository fit together, from source imagery and road lines to a scored road graph. Read this first, then the explainer for the part you care about.

## Core idea

ROSA is a road dataset built by laying a national road layer on Sentinel-2 images of 60 zones in South Africa and cutting them into tiles. A road model learns to predict, for each tile, where the roads are and where they meet, and turns that into a road graph. The graph is scored against the graph of the tile's labels with two connectivity metrics, TOPO and APLS. The labels are a naive overlay, so a cleaning step lets annotators correct them zone by zone before the dataset is built again.

## How it works

```text
road source (CDNGI)  --roads-->  road parquet --+
                                                +--generate-->  ROSA dataset (baseline)
zone imagery (60 COGs, pre-split) --------------+                 |            |
                                                      package-zones|            |train
                                                                   v            v
                                     zone packages, cleaned in QGIS     checkpoint per variant
                                     (assembler planned, regenerates)          |inferencer
                                                                               v
                                                              predicted graphs per tile
                                                                               |eval_graphs
                                                                               v
                                                              TOPO and APLS per tile, then triage
```

1. **Road lines.** The `roads` command maps one road source into a single road parquet with a road class and a buffer per line. The production baseline uses CDNGI. See [road vectors](road-vectors.md).
2. **Zone imagery.** Each zone is one 10 m image in UTM with 20 bands, already sorted into `train/`, `val/` and `test/` folders. Where these images were made is not recorded, see KEL-28.
3. **Tiling.** `generate` burns the roads into a mask, cuts a fixed 512 px grid, keeps tiles with roads and no missing pixels, and writes five artifacts per tile plus the metadata and split CSVs. The first run, frozen, is the baseline. See [dataset tiling](dataset-tiling.md). `norm-stats` and `summary` describe the result, see [dataset stats](dataset-stats.md).
4. **Cleaning.** `package-zones` turns the baseline into one zone package per zone for annotators to correct in QGIS against aerial imagery. Merging cleaned zones back into a road layer and running `generate` again is planned, not built. See [zone cleaning](zone-cleaning.md).
5. **Training data.** The dataset reads 128 px crops of a tile, upsamples them to 256 px and derives road, keypoint and graph labels. See [training data](training-data.md).
6. **Models.** Every variant follows SAM-Road: a segmentation part predicts road and keypoint masks, and a topology head scores which nearby points are joined. The variants swap the backbone and the input bands. See [models](models.md) and [training](training.md).
7. **Inference.** The inferencer slides overlapping crops over each tile, merges the masks, picks graph points and keeps the links the topology head scores high. See [inference](inference.md).
8. **Evaluation.** `eval_graphs` scores each predicted graph against the tile's label graph and writes per-tile TOPO and APLS. `triage` ranks and draws tiles from those scores. See [evaluation](evaluation.md).
9. **Web app.** A separate path: the user picks cells on a map, the app fetches a fresh Earth Engine composite per cell, runs the same per-tile inference and returns GeoJSON roads. See [web app](web-app.md) and [imagery fetch](imagery-fetch.md).

The CRESI mask post-processing in `mask_postprocessing/` is a second way to turn a road mask into a graph. Nothing calls it today, see [mask post-processing](mask-postprocessing.md).

## Details that matter

- **One YAML per variant drives every stage.** A file in `src/geosamroad/configs/hpc/` holds training settings, crop and upsampling, and the inference thresholds. `train`, `test`, `inferencer` and the web app all load it through `geosamroad/utils.py`.
- **Normalisation lives in the model.** The dataset returns raw band values. Each model rescales them with its backbone's statistics, so inference and the app need no dataset file.
- **Bands are found by name.** The band layout of a tile is named, and models select bands by name through `resolve_input` in `dataset/bands.py`. The SAM variant always reads RGB only.
- **Scores measure agreement with raw labels.** Until cleaned labels exist, every TOPO and APLS number compares a prediction with the uncleaned CDNGI overlay.
- **Training and app imagery come from different code.** Training uses the 60 pre-made zone images in 10 m UTM. The app fetches through `globalurbanmapper_gee` in degrees, so its pixels are not square, and six of its 21 bands are always 0.

## Why it's like this

- **Reference imagery decides road position during cleaning**, and 2020 Sentinel-2 decides existence when the two clearly disagree. See ADR 0001.
- **Second sources such as Overture are flags only**, never copied into labels, because of ODbL licensing. See `docs/cleaning/open-directions.md`.
- Most other design choices have no recorded reason yet. Each one is a sub-issue of KEL-12, linked from the explainer it belongs to.

## Weak spots and ideas

- **The inference thresholds may be tuned on test.** Their values match the 256-bin search that `test.py` prints on the test split. See KEL-36.
- **The UNet and SGCN normalisation stats are a hand copy.** They are hard-coded in `normalization.py` (last re-checked against the raw-labels baseline v2 in KEL-10), and nothing reads the stats `norm-stats` writes. See KEL-21.
- **Package dependencies run in a chain.** `sentinel2data` imports `geosamroad.dataset`, `globalurbanmapper_gee` imports `sentinel2data.generator.helper`, and the web app imports all three. The dataset builder cannot be installed or tested without the training package.
- **Much of the vendored code is dead.** Our code imports only `BilinearSampler`, `graph_utils` and `extract_graph_points` from `sam_road/`. Its `train.py`, `test.py`, `triage.py`, `inferencer.py`, `dataset.py`, `config/` and both metric copies are never imported. `mask_postprocessing/` has no caller, and `RasterRoadDataModule` is unused.
- **The README is stale.** It points at `configs/local/sam.yaml`, which does not exist. Only `configs/hpc/` exists.
- **Little is tested outside dataset building.** `sentinel2data` has a synthetic-zone test harness. `roads`, the models, inference, evaluation and the web app have no tests.

## Code map

- `src/sentinel2data/`: the dataset builder. `cli.py` has the `roads`, `generate`, `norm-stats`, `summary`, `change-report` and `package-zones` commands. `generator/` tiles, `cleaning/` packages zones, `supplementary/` holds roads and stats.
- `src/geosamroad/`: the road models. `dataset/`, `models/`, `train.py`, `test.py`, `inferencer.py`, `eval_graphs.py`, `triage.py`, `configs/` and the vendored `sam_road/`, `metrics/` and `mask_postprocessing/`.
- `src/globalurbanmapper_gee/`: the Earth Engine composite and download port, used by the web app.
- `src/geosamroad_app/` and `deploy/`: the web app and its container.
- `docs/adr/`, `docs/guides/`, `GLOSSARY.md`: decisions, the usage guides and the shared vocabulary.
