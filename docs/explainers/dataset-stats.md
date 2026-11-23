# Dataset statistics

How `sentinel2data.cli norm-stats`, `summary` and `change-report` describe a generated ROSA dataset, and where the normalisation statistics end up in training.

## Core idea

Three small commands run on a finished dataset. `norm-stats` reads every train tile and computes one mean and one standard deviation per band. `summary` reads the metadata table and reports, per split, how the tiles spread over biome, urbanisation and road density. `change-report` compares a baseline with a previous dataset, zone by zone. None of them feeds training directly. The models normalise with values written into the code. The ROSA values there are a hand copy of the `norm-stats` run on the raw-labels baseline v2 (21 bands, KEL-10).

## How it works

1. **List the train tiles.** `norm-stats` reads `splits/train.csv` and takes the unique `image_path` values. Val and test tiles are never read.
2. **Name the bands.** It opens the first tile. The band names come from the tile's band descriptions, which `generate` writes. A tile without descriptions falls back to the `ROSA_BANDS` order.
3. **Accumulate per band.** For each tile and each band, it keeps the valid pixels and adds their sum, sum of squares and count to running totals in float64. With `--sar-clip LOW HIGH`, it clips the VV and VH bands to that range first.
4. **Finish and write.** The mean is sum over count. The standard deviation is the square root of the mean of squares minus the squared mean. The command writes both lists to `norm_stats.yaml` under `data.norm_mean` and `data.norm_std`, then prints a table by band name.
5. **Summarise the metadata.** `summary` reads `metadata.parquet` and groups the tiles by `split_set`. For each split it writes the tile count and the share of each `urbanisation_classification`, `road_density_classification` and `biome` value. It adds the mean, median, 90th percentile and maximum of `road_density`. The result goes to `dataset_summary.yaml` and the terminal.
6. **Compare with a previous dataset.** `change-report` reads the baseline's `generation_report.yaml` (tile statuses and excluded road segments per zone) and `tile_status/` (each zone's split), and the previous dataset's `generation_report.yaml` (its `zones` list). It writes `kept_dropped_report.md`: one table per split and one per zone, with kept tiles before and after, drops by reason (no road, NaN), untiled edge positions, and the segments and km each excluded road class lost. It stops if a zone's split differs between the two.
7. **Normalise in the model.** At training and inference time, each model divides `image * scale - mean` by `std`, with constants from `models/normalization.py`. The constants depend on the encoder:
   - SAM uses the ImageNet RGB stats, such as mean 123.675 for red, with scale 255.
   - TerraMind uses its own pretraining stats. The scale is 10000 for Sentinel-2 bands, 1 for SAR bands and 255 in RGB mode.
   - UNet and SGCN use the frozen ROSA train stats with scale 1.

## Details that matter

- **Zero counts as missing.** A tile with no nodata value drops every 0 pixel from the stats, because `--exclude-zero` is on by default. Generated tiles carry no nodata value, so this rule applies. It also drops real zeros, such as flat slope, north-facing aspect and enhanced RGB pixels at exactly 0.
- **Binary bands end up with mean 1 and std 0.** The frozen ROSA stats give `esa_urban_10m`, `gisa_urban_10m` and `wsf_urban_10m` exactly mean 1 and std 0. Only the 1 pixels survived the zero rule. `rosa_norm` replaces any std below 1e-6 with 1.0, so these bands would pass through as `x - 1`. No current model reads them.
- **The stats are pooled over pixels, not averaged over tiles.** Every valid pixel weighs the same, so a tile with many zeros counts less.
- **A missing tile file is skipped with a printed line.** A tile with a different band count from the first tile stops the run.
- **The frozen stats are keyed by band name.** `ROSA_NORM_STATS` maps each of the 21 `ROSA_BANDS` names to its pair. `ROSA_MEANS` and `ROSA_STDS` list them in that order. `rosa_norm` then picks the bands a model reads by 1-based index. `test_rosa_norm_stats_follow_the_band_by_name` checks that each band keeps its old value.
- **UNet and SGCN read 12 bands by default.** `ROSA_SEG_BANDS` holds the enhanced RGB, NIR, B5 to B7, B8A, B11, B12 and ascending VV and VH. In RGB mode they read only the enhanced RGB.
- **`summary` shares include missing values.** It counts NaN as its own category, and it rounds shares to 4 decimals and density stats to 6.

## Why it's like this

- **Train split only.** The CLI help says "Pre-compute per-band mean and std over train split". No document gives the reason. Reason unknown, see KEL-20.
- **Stats keyed by band name.** KEL-8 dropped the S1 descending bands. Its acceptance criteria require the normalisation stats to "select the same physical bands as before the change (checked by band name, not index)". Commit 3695d4e did this by keying the stats by name.
- **Frozen stats in code, not read from `norm_stats.yaml`.** The constants arrived in the initial import commit e30bb84 with no stated reason. Reason unknown, see KEL-21.
- **Zero treated as missing by default.** Reason unknown, see KEL-22.

## Weak spots and ideas

- **`norm-stats` output has no reader.** Observed. Nothing loads `norm_stats.yaml` or a `data.norm_mean` key. The `rosa.yaml` comment points at `src/unet/configs/norm_stats.yaml`, a path that does not exist. Updating the ROSA stats means copying numbers by hand into `_ROSA_MEANS` and `_ROSA_STDS`, in the 21-band order `norm-stats` prints.
- **The frozen stats only stay right while the train tiles do.** Observed. KEL-10 re-ran `norm-stats` on the raw-labels baseline v2 and got the old values (6 significant figures) for every kept band: removing Track and Slipway dropped no train tile, and labels never touch pixel stats. A later change to the train tile set or the imagery needs a new run and a new hand copy.
- **Excluded segment counts depend on how you count.** Observed in KEL-10. `change-report` (from `generate`) counts each source segment that touches the zone once (1,170 Track/Slipway segments), with km clipped to the zone (1,063.8 km). Counting the pieces left after clipping gives 1,183, and counting by WGS84 bounding box gives 1,197. Quote the method with the number.
- **Dead normalisation path in the dataset.** Observed. `RasterDatasetConfig` has `normalize`, `norm_mean` and `norm_std`, and `apply_norm` falls back to per-crop `standardize`. `build_dataset_config` never sets `normalize`, so it stays `False` and all normalisation happens in the model. Two places own the same job.
- **The zero rule is wrong for some bands.** Observed in the frozen numbers. Binary urban bands get a useless std of 0. Slope, aspect and the enhanced RGB lose real zeros, which biases their mean upward. A per-band rule, or a real nodata value written by `generate`, would fix it.
- **The SAR clip is not recorded.** Observed. `--sar-clip` changes the SAR stats, but `norm_stats.yaml` does not say whether it was used, and no model clips SAR at load time. A run with the clip would give stats that do not match the inputs the model sees.
- **The name fallback is wrong for legacy tiles.** Suspected, untested. A 23-band tile without descriptions gets the 21 `ROSA_BANDS` names plus `band22` and `band23`. Every name from index 12 on is shifted, so `--sar-clip` would skip the descending bands.
- **One-pass variance.** Suspected. Sum of squares minus squared mean in float64 is fine at these magnitudes, but Welford's method would be safer for elevation, with mean 690.6.
- **`summary` gives no per-zone or road length view.** Observed. It reports shares of tiles only. A split can match on tile shares and still differ in road length or zone count. Nothing consumes `dataset_summary.yaml`.
- **Code smells.** Observed. `compute` shadows the built-in `sum`. `IMAGERY_COLS` is a single string with a plural name, and `_train_imagery_paths` wraps it in an if with one branch. A sentinel2data command imports `geosamroad.dataset` helpers, so the dataset builder depends on the training package. `terramind_norm` returns a list for scale while the other two return a number, `RoadSegmentationModel` and the model in `models/model.py` each register the same three buffers with their own code.

## Code map

- `src/sentinel2data/cli.py`, `norm_stats`, `summary` and `change_report`: the CLI entry points and their defaults.
- `src/sentinel2data/supplementary/compute_norm_stats.py`, `compute`, `_band_names`, `write_stats_yaml`: per-band train stats.
- `src/sentinel2data/supplementary/change_report.py`, `build_change_report`: the kept/dropped report.
- `src/sentinel2data/supplementary/summary.py`, `summarise_metadata` and `summarise_dataset`: per-split shares and road density stats.
- `src/geosamroad/models/normalization.py`, `ROSA_NORM_STATS`, `rosa_norm`, `sam_norm`, `terramind_norm`: the frozen constants each encoder uses.
- `src/geosamroad/models/segmentation_wrappers.py`, `RoadSegmentationModel.normalize`: applies the ROSA stats for UNet and SGCN.
- `src/geosamroad/models/model.py`, the `pixel_mean`, `pixel_std` and `pixel_scale` buffers: applies the SAM and TerraMind stats.
- `src/geosamroad/dataset/helper.py`, `apply_norm` and `standardize`: the unused dataset-side normalisation.
- `src/geosamroad/dataset/bands.py`, `ROSA_BANDS`, `ROSA_BANDS_V2`, `ROSA_SEG_BANDS`: the band layouts the stats are keyed against.
