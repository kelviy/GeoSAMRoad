# Training data

How `SAMROAD_Dataset` turns ROSA tiles on disk into training and evaluation samples for every encoder that `geosamroad.train` runs.

## Core idea

A sample is a small square crop of one tile, not the whole tile. Training picks a random 128 px crop from a random tile, upsamples it 2x to 256 px, and flips or rotates it. Evaluation cuts each tile into a fixed grid of 16 crops. Each crop comes with three labels: a road mask, a keypoint mask for intersections and dead ends, and graph point pairs that say which road points connect. The image keeps its raw band values, and the model normalises it.

## How it works

1. **Pick the bands by name.** `resolve_input` maps the encoder and `RGB_INPUT` to band names, then to 1-based indices into the 21-band tile layout.
2. **List the tiles.** Read `splits/<split>.csv`. With `FINAL_TRAIN`, train reads `train.csv` and `val.csv`, and val reads `test.csv`.
3. **Choose a crop.** In training, draw a random tile and a random 128 px window. If the window has fewer than 50 road pixels, draw again, up to 20 times. In val and test, sample `idx` maps to one cell of a 4 by 4 grid on one tile.
4. **Read the image.** Read the window and upsample it to 256 px with bicubic resampling. NaN and infinite values become 0.
5. **Make the road mask.** Buffer the tile's road lines from `masks_graph/` by their class half-width and rasterize them at 5 m.
6. **Augment.** In training only, rotate by a random multiple of 90 degrees and flip horizontally with probability 0.5.
7. **Make the keypoint mask.** Draw a disk of radius 2 px on every graph node whose degree is not 2, then apply the same rotation and flip.
8. **Sample the graph labels.** Scale the tile's pixel adjacency graph by 2. `GraphLabelGenerator.sample_patch` picks graph points inside the crop, labels which pairs connect and applies the same rotation and flip.
9. **Batch.** `graph_collate_fn` pads `graph_points` with zeros to the longest list in the batch.

## Details that matter

- **Bands per encoder.**

  | Config | Bands read |
  | --- | --- |
  | `sam`, `*_rgb` | `B4_R_enhanced`, `B3_G_enhanced`, `B2_B_enhanced` |
  | `terramind` | the 10 Sentinel-2 bands, `VV_ascending`, `VH_ascending` |
  | `unet`, `sgcn` | the 3 enhanced RGB bands, `B8_NIR`, `B5`, `B6`, `B7`, `B8A`, `B11`, `B12`, `VV_ascending`, `VH_ascending` |

  `RGB_SOURCE: raw` swaps in `B4_R`, `B3_G`, `B2_B`. No config uses it. No encoder reads the DEM, slope, aspect or the three urban layers. The `sam` encoder reads 3 bands whatever `RGB_INPUT` says.
- **Geometry.** `CROP_SIZE: 128` and `UPSCALE: 2` give a 256 px patch at 5 m per pixel that covers 1,280 m. `utils.py` sets `PATCH_SIZE` to `CROP_SIZE * UPSCALE`. `RasterRoadDataset.image_size` is hard-coded to 512, and nothing checks the real tile size.
- **Epoch length.** A training epoch has `TRAIN_LEN_FACTOR` times the tile count samples, which is 10 times. Every sample draws its tile with replacement and ignores `idx`, so some tiles appear several times per epoch and others not at all.
- **The road-pixel retry favours busy tiles.** The 50 px threshold counts on the native 10 m `masks_raster` crop of 16,384 px. Each retry draws a new tile as well as a new window, so sparse tiles are sampled less often. After 20 failures the last crop is used.
- **The road mask depends on `UPSCALE`.** At `UPSCALE: 1` the mask is the tile's `masks_raster` slice, burned at 10 m. Above 1, `_graph_mask` re-rasterizes the buffered lines at the finer pixel size with `all_touched=True`. `all_touched` widens a road by about one pixel, which is 5 m at 2x and 10 m at 1x. The 2x mask is narrower on the ground.
- **The keypoint mask also depends on `UPSCALE`.** At 1x it is the 3 by 3 squares in `masks_keypoints/`. Above 1x it is a disk of radius `KEYPOINT_BUFFER_M` over the pixel size, so 10 m gives 2 px. Nodes within 1 native px of the tile border get no disk, the `BORDER_EPS` rule from tiling.
- **No normalisation in the dataset.** `build_dataset_config` never sets `normalize`, so samples carry raw values. Sentinel-2 band means sit between 0.06 and 0.27, enhanced RGB is in 0 to 1 and SAR is in dB. SAM scales by 255 and uses its ImageNet stats. TerraMind scales Sentinel-2 by 10,000 and uses its pretraining stats. UNet and SGCN use `rosa_norm`, the frozen ROSA train stats keyed by band name. See [models](models.md).
- **Graph labels.** `GraphLabelGenerator` follows upstream SAM-Road.
  - It splits graph edges into pieces of at most 4 px.
  - It finds crossovers, where two edges cross without a shared node, and never samples points within 4 px of one.
  - Non-maximum suppression thins the points to `ROAD_NMS_RADIUS` apart, 8 px or 40 m. Nodes of degree other than 2 always survive.
  - It draws `TOPO_SAMPLE_NUM` source points, 128, with replacement. Points within 16 px of an intersection or crossover get weight 0.9 and the rest get 0.1.
  - Each source gets up to 16 neighbours within `NEIGHBOR_RADIUS`, 32 px or 160 m. A pair is connected when a breadth-first walk on the graph reaches the neighbour within 32 // 4 = 8 hops without passing another candidate first.
  - It adds Gaussian noise with a standard deviation of 1 px to every point.
- **Empty crops.** An empty adjacency graph, or a crop with no graph points, gives one fake point at 0, 0 and all-invalid pairs.
- **Caching.** `PRELOAD_GRAPHS: true` builds every tile's `GraphLabelGenerator` before training. It also fills `_load_graph`, an LRU cache of only 256 road parquets per process. `__getstate__` empties the generator cache on pickling, so only forked workers keep the preload.

## Why it's like this

- **Bands are chosen by name.** KEL-8 dropped the S1 descending bands from the tiles, which moved later band indices. Commit 3695d4e keyed the band groups and norm stats by name "so every encoder reads the same physical bands and stats as before." `tests/geosamroad/test_band_selection.py` pins each encoder to literal band names and the stats each band had before the change.
- **Graph radii are half of upstream.** Code comments say the upstream 16 px NMS and 64 px neighbour radius "work for 2.5m". The 5 m configs use 8 and 32, the same ground distance. The interesting-point radius is halved from 32 to 16 the same way. The 4 px subdivision step and the 4 px crossover radius are not halved. Reason unknown, see KEL-29.
- **`TOPO_SAMPLE_NUM` is 128.** Upstream SAM-Road uses 128 for its 256 px patch configs and 512 for 512 px patches.
- **Upsampling 2x a 128 px crop.** Every config does this. Reason unknown, see KEL-30.
- **Final runs move val into train.** `apply_final_run` in `train.py` trains on train and val and logs test in the val slot "for monitoring only". Checkpoints are picked on `train_road_iou`.
- **The flip is ours.** Upstream `sample_patch` only rotates. `d4_augment` also flips, so `sample_patch` gained a `flip` argument to keep the points aligned with the image.

## Weak spots and ideas

- **Val and test labels are random.** `sample_patch` draws random NMS scores, random source points and 1 px noise in every split. Val topology loss changes between epochs with a fixed model. Observed. An eval flag that turns this off would fix it.
- **The label definition changes with `UPSCALE`.** Road width and keypoint shape differ between 1x and 2x, so results at the two settings do not measure the same target. Observed.
- **Hop count is not distance.** An edge shorter than 4 px is still one hop. CDNGI lines have a node at every vertex, so on a densely digitised curve 8 hops can cover much less than 32 px. Nearby points on one road may then be labelled unconnected. Suspected, not measured.
- **Unscaled graph constants.** The subdivision step and crossover radius cover twice the upstream ground distance. Suspected to matter little.
- **Crossovers come from un-noded lines.** Roads that cross without a shared vertex count as a crossover and their area gets no sampled points. [Dataset tiling](dataset-tiling.md) notes nobody has measured how often this happens in CDNGI.
- **`min_road_density` also filters val and test,** and its error message says "train" for every split. Observed. The default is 0.
- **Retry bias.** Retrying windows inside the same tile would keep the tile mix uniform.
- **Band source mismatch.** `RoadSegmentationModel` calls `resolve_input` with `"enhanced"` hard-coded, while the dataset uses `RGB_SOURCE`. A UNet or SGCN run with `RGB_SOURCE: raw` would normalise raw RGB with enhanced-RGB stats. `encoder_modalities` also ignores `rgb_source`. Observed in code, no config triggers it.
- **TerraMind RGB input.** `terramind_rgb` feeds enhanced RGB, mean near 0.5, scaled by 255 against TerraMind RGB stats with mean near 80. Suspected input shift, not measured.
- **Augmentation is geometric only.** There is no brightness, contrast or noise augmentation on the image.
- **Dead code.**
  - `RasterRoadDataModule` is never imported. It is the only caller that sets `debug=True`, so the crop size check never runs.
  - `apply_norm`, `standardize`, `normalize`, `norm_mean` and `norm_std` never run. `compute_norm_stats` writes `norm_mean` keys that nothing reads.
  - `S2_10M`, `S2_20M`, `S2_SAR` and `TERRAMIND_MODALITIES` in `bands.py` have no users.
  - The dataset never reads `DATASET`, `TOPONET_VERSION` or `ITSC_NMS_RADIUS` in `SamRoadDatasetConfig`.
  - Our code never imports the vendored `sam_road/dataset.py`, with `SatMapDataset` and its own `GraphLabelGenerator`.
- **Code structure.**
  - `GraphLabelGeneratorConfig` has the comments for `TOPO_SAMPLE_NUM` and `NEIGHBOR_RADIUS` swapped.
  - The 4 px subdivision, 4 px crossover radius, 16 px interesting radius and 1 px noise are hard-coded in `GraphLabelGenerator`. They belong in config next to the radii they depend on.
  - `SAMROAD_Dataset.__getitem__` replays the augmentation from `rot_k` and `flip` keys the parent returns. One transform object applied to image, masks and points would make this contract explicit.
  - `_empty_topo` duplicates the empty fallback inside `sample_patch`. `SAMROAD_Dataset` repeats the parent's length formula for `dev_run`.
  - The dataset imports `window_bounds` and `BORDER_EPS` from `sentinel2data.generator`, so training depends on the tiling package.

## Code map

- `src/geosamroad/dataset/bands.py`, `resolve_input`, `band_indices` and `ROSA_BANDS`: the band layout and which bands each encoder reads.
- `dataset/raster_dataset.py`, `RasterRoadDataset` and `_graph_mask`: pick the crop, read and upsample, make the road mask, augment.
- `dataset/samroad_dataset.py`, `SAMROAD_Dataset` and `SamRoadDataModule`: add keypoint and graph labels. Training uses this class.
- `dataset/graph_dataset.py`, `GraphLabelGenerator.sample_patch` and `graph_collate_fn`: the adapted SAM-Road graph label sampler and batching.
- `dataset/helper.py`, `train_crop_bounds`, `eval_crop_bounds`, `d4_augment` and `read_upsampled_window`: crop positions, augmentation and window reads.
- `src/geosamroad/sam_road/graph_utils.py`: vendored `subdivide_graph`, `find_crossover_points`, `nms_points` and `bfs_with_conditions`.
- `src/geosamroad/utils.py`, `build_dataset_config`: maps the YAML keys to `SamRoadDatasetConfig`.
- `src/geosamroad/configs/hpc/*.yaml`: crop, upscale, sampling and graph settings per encoder.
- `tests/geosamroad/test_band_selection.py`: pins each encoder's bands and norm stats by name.
