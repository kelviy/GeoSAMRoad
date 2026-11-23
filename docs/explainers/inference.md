# Inference

How `python -m geosamroad.inferencer` runs a trained model over every tile of one split and writes a road graph per tile.

## Core idea

The model only sees small patches, so the inferencer slides a patch window over each tile in an overlapping grid. It averages the patches into one road mask and one keypoint mask for the whole tile. It then places graph nodes on the strong pixels of those masks, spaced apart so no two sit closer than a few pixels. For each node it asks the topology head which nearby nodes it connects to. Pairs that score above a threshold become edges. Every tile is processed on its own, and the graph never crosses a tile edge.

## How it works

1. **Load the model and the split.** Read the training config and load the checkpoint into `GeoSAMRoad`. Read the split CSV, such as the test split.
2. **Read and upscale the tile.** Read the 512 px tile and upsample it with bicubic resampling by `UPSCALE`. All configs use 2, which gives a 1,024 px image. The enhanced RGB bands are read the same way for the preview image.
3. **Pass 1, the masks.** Cut the upscaled tile into a 16 by 16 grid of overlapping 256 px patches. Run the patches through the model in batches of 16. Each patch gives a keypoint score and a road score per pixel, plus an encoder feature map. Add each patch's scores into a tile-sized sum and divide by how many patches covered each pixel. Keep every feature map for pass 2.
4. **Thin the road mask.** Threshold the road mask, remove specks and holes up to 16 px, and skeletonize it to a one-pixel centreline. The road scores on the centreline pixels make the mask that nodes are picked from.
5. **Pick the nodes.** `graph_extraction.extract_graph_points` runs non-maximum suppression, NMS, three times. The first run is over keypoint pixels, the second over centreline pixels, and the third over both sets together with keypoints ranked first.
6. **Pass 2, the edges.** For each patch, find the nodes inside it. Pair each node with up to 16 nearest neighbours within 32 px. Feed the pairs and the cached feature map to the topology head, which scores each pair from 0 to 1. A pair seen in several patches gets the mean of its scores. A pair whose mean beats `TOPO_THRESHOLD` becomes an edge.
7. **Write the outputs.** Save the masks, a preview and the graph pickle for the tile, and move to the next tile.

## Details that matter

- **Patch grid.** `PATCH_SIZE` is `CROP_SIZE` times `UPSCALE`, so 128 times 2, which is 256 px. `patch_offsets` spaces 16 starts evenly from 0 to 768, about 51 px apart. That is 256 patches per tile. A pixel near the centre is averaged over 25 patches, and a corner pixel over 1. `patch_offsets` raises an error when `INFER_PATCHES_PER_EDGE` is too small to cover the tile. `main` runs this check once before loading the model.
- **The merge is a plain mean.** Every patch has the same weight at every pixel. Patch borders, where the model has the least context, count as much as patch centres.
- **Thresholds differ per model.** Each `configs/hpc/*.yaml` sets its own `ITSC_THRESHOLD`, `ROAD_THRESHOLD` and `TOPO_THRESHOLD`. The SAM config uses 0.3098, 0.3216 and 0.5294. The UNet config uses 0.4078, 0.4118 and 0.5098. Every value is a whole number over 255.
- **NMS radii are in upscaled pixels.** `ITSC_NMS_RADIUS` is 4 and `ROAD_NMS_RADIUS` is 8. `NEIGHBOR_RADIUS` is 32, which is 16 native px or 160 m. The third NMS run uses `ROAD_NMS_RADIUS`, so the final nodes are at least 8 px apart, keypoints included. The keypoint radius of 4 has no effect on the final spacing.
- **Keypoints win ties, they are never forced.** The third NMS gives keypoints score 1 and road points score 0. `nms_points` only spares a neighbour whose score is above 1, so a keypoint also removes other keypoints within 8 px.
- **Edges are scored per direction.** The pair from A to B and the pair from B to A are averaged apart. One direction above the threshold is enough for an edge. `convert_to_sat2graph_format` adds the reverse of every edge.
- **NaN scores.** A patch with no valid pairs makes the topology head return NaN. The inferencer replaces NaN with -100. It only reads pairs marked valid, so those scores are never used.
- **Feature cache.** Pass 2 reuses the encoder features from pass 1, so the encoder runs once per patch. All 256 feature maps stay on the GPU unless `INFER_CACHE_FEATURES_CPU` is true.
- **`PRETRAINED` is set to false** when a checkpoint is given, unless `--set PRETRAINED=...` overrides it. The checkpoint replaces every weight anyway.
- **Output folder.** It defaults to `save/<RUN_NAME>_<split>`. It holds:
  - `config.yaml`, the resolved config.
  - `mask/<tile>_road.png` and `mask/<tile>_itsc.png`, the merged road and keypoint scores as 0 to 255 at 1,024 px.
  - `mask/<tile>_road_thin.png`, the centreline mask, when thinning is on and the tile has road.
  - `viz/<tile>.png`, the enhanced RGB at 1,024 px with the graph drawn on it.
  - `graph/<tile>.p`, a pickled adjacency dict. Keys and neighbours are x, y positions divided by `UPSCALE` and rounded to whole native pixels. This matches the label graphs in `mask_adj_graphs` that [evaluation](evaluation.md) compares against. A tile with no nodes gets an empty dict.
  - `inference_time.txt`, the summed time of the model and graph steps, without file reading and writing.
- **Segmentation models use the same path.** The UNet and SGCN configs wrap a CNN inside `GeoSAMRoad`. The CNN outputs the same two channels, keypoint and road, and its feature map feeds its own topology head. The inferencer does not tell them apart from SAM or TerraMind. The inferencer does not hand off to the CRESI code in [mask post-processing](mask-postprocessing.md). `cresi_extract.road_png_to_nx` reads a road PNG like the ones in `mask/`, but no code in the repo calls it.
- **The web app reuses the core.** `geosamroad_app/runner.py` imports `infer_one_tile`, `patch_offsets` and `NATIVE_TILE_PX`. See [web app](web-app.md).

## Why it's like this

- **The method is SAM-Road's.** Patch masks, NMS to nodes and a topology head that scores neighbour pairs all come from the vendored SAM-Road code. Our `inferencer.py` rewrites its `infer_one_tile` for ROSA tiles. It drops the 64 px margin that the SAM-Road patch grid keeps from the image edge, starting at 0 instead. It adds the coverage check, the centreline thinning, the CPU feature cache and the output in native-pixel x, y. Reason for each change unknown, see KEL-34.
- **Thinning before NMS.** Commit `a204fa1` added `THIN_BEFORE_NMS` and turned it on in every HPC config. Its code comment says the thin mask is "the centreline the graph points". Reason unknown, see KEL-35.
- **Per-model thresholds.** `GeoSAMRoad.on_test_end` prints the threshold with the best F1 for keypoints, roads and topology, from a 256-bin precision-recall curve. The config values are all multiples of 1/255, which matches those bins. Which split they were tuned on is unknown, see KEL-36.

## Weak spots and ideas

- **Thresholds may be tuned on the test split.** It is observed that `test.py` only evaluates `test_dataloader`, and `on_test_end` picks the best-F1 thresholds there. It is only suspected that the config thresholds came from that run. If so, the test scores are optimistic. Tuning on val would fix it.
- **Thresholds were tuned on pixels, then used for graphs.** `on_test_end` maximises pixel F1 on 128 px crops. The inferencer uses the same numbers to pick nodes and edges on whole tiles, where APLS and TOPO decide quality.
- **No stitching across tiles.** Each tile is a separate graph. Roads that cross a tile edge stay broken at the edge.
- **The mean merge treats patch borders like centres.** A distance-weighted blend would down-weight the edges of each patch. Suspected to matter less here because centre pixels average 25 patches.
- **Upscaling precision is thrown away.** Nodes are found at 1,024 px and then rounded to whole 512 px pixels, so up to 5 m of shift per node. The masks and preview stay at 1,024 px, so the `mask/` files and the graph use different pixel grids. None of the outputs is georeferenced.
- **`ITSC_NMS_RADIUS` is close to dead.** The final NMS at 8 px overrides it. Observed from the code.
- **`infer_one_tile` does too much.** One function runs pass 1, merges masks, thins, picks nodes, builds KD-trees, pads batches, runs pass 2 and votes on edges. The edge vote is a triple Python loop with an `assert` on every score. Splitting it into a mask stage, a node stage and an edge stage would make each one testable.
- **Duplicated work in `main`.** `main` calls `thin_road_mask` a second time to save the PNG, after `infer_one_tile` already computed it.
- **Misleading names.** `pred_nodes` holds row, col, but `main` flips it to x, y before `convert_to_sat2graph_format`, whose comment says it expects row, col. The output is correct, but the comment is wrong for our call.
- **Memory.** All 256 feature maps per tile stay on the GPU by default. Large encoders may run out of memory. Observed in the code, not measured.
- **Dead vendored code.** Nothing imports `sam_road/inferencer.py`. It still refers to `SAMRoad`, cityscale and spacenet. In `graph_extraction.py` only `extract_graph_points` and `get_points_and_scores_from_mask` are used. The A* path is unused. That covers `extract_graph_astar`, `create_cost_field_astar` and `is_connected_astar`. The Bresenham check, the drawing helpers and the `__main__` block are unused too. `graph_utils.py` defines `edge_list_to_adj_table` twice, and the second definition replaces the first without warning. It also carries a `unittest` class and matplotlib imports.
- **The CRESI path is not wired in.** Nothing calls `cresi_extract.road_png_to_nx`. If the ablation needs it, it needs a script that reads `mask/*_road.png` and writes `graph/*.p` in the same coordinates.

## Code map

- `src/geosamroad/inferencer.py`, `main`: CLI, model loading, the tile loop and all file writing.
- `inferencer.py`, `infer_one_tile`: both passes for one tile, from image to nodes and edges.
- `inferencer.py`, `patch_offsets` and `min_patches_per_edge`: the overlapping patch grid and its coverage check.
- `inferencer.py`, `thin_road_mask`: threshold, clean and skeletonize the road mask.
- `models/model.py`, `GeoSAMRoad.infer_masks_and_img_features` and `infer_toponet`: the two model calls.
- `models/model.py`, `TopoNet`: the topology head that scores node pairs.
- `sam_road/graph_extraction.py`, `extract_graph_points`: the three NMS runs that pick nodes.
- `sam_road/graph_utils.py`, `nms_points` and `convert_to_sat2graph_format`: NMS and the adjacency dict format.
- `triage.py`, `visualize_image_and_graph`: draws the preview.
- `sam_road/inferencer.py`: upstream SAM-Road inferencer, unused.
