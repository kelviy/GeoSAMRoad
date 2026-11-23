# Mask post-processing

How `geosamroad.mask_postprocessing` turns a predicted road mask into a road graph with the CRESIv2 method. The code was added for an ablation, and nothing in the repo calls it yet.

## Core idea

Threshold the road mask, clean up specks, and thin the road pixels to one-pixel centrelines. Read the centrelines as a graph, with a node at every junction and dead end. Then tidy the graph. Short dead-end spurs go. Small gaps between dead ends get bridged. Small isolated pieces go. The result is a road graph built from the road mask alone, with no keypoint mask and no learned edge scores. That makes it a baseline for the SAM-Road graph extraction in [inference](inference.md).

## How it works

1. **Read and threshold.** `road_png_to_nx` in `cresi_extract.py` takes the path of a saved road mask PNG and a threshold between 0 and 1. A pixel is road when its value is above 255 times the threshold.
2. **Pad the border.** `make_skeleton` copies the outer 5 px of the mask outward first. This stops the thinning from shrinking roads back from the tile edge.
3. **Clean specks.** The code removes road blobs of 4 px or less, then fills holes of 4 px or less.
4. **Thin.** `skimage.morphology.skeletonize` reduces the road to one-pixel centrelines. The code then cuts the 5 px of padding away, plus 2 more px, and zeroes a 2 px frame around the skeleton.
5. **Skeleton to graph.** `sknw.build_sknw` walks the skeleton. A pixel with exactly two skeleton neighbours belongs to an edge. Any other skeleton pixel is a node. Each edge keeps its pixel path and its length in pixels.
6. **Prune spurs.** `remove_small_terminal` deletes dead ends whose edge is shorter than 10 px. It runs up to 8 rounds and stops when a round removes nothing. Then the code drops self-loops.
7. **Simplify and bridge.** `G_to_wkt` simplifies each edge's pixel path with `cv2.approxPolyDP` at a tolerance of 2 px, and writes the edges as WKT lines. `add_small_segments` then adds straight lines between pairs of dead ends that look like a broken road.
8. **Rebuild and drop small pieces.** `wkt_list_to_nodes_edges` and `nodes_edges_to_G` turn the WKT lines back into a networkx graph. Every line vertex becomes a node. `clean_sub_graphs` removes connected pieces whose longest path is under 60 px.
9. **Convert for evaluation.** `nx_to_edges_xy` lists the edges as pixel coordinate pairs. `edges_xy_to_adj` divides by the upscale factor and builds the adjacency dict that [evaluation](evaluation.md) reads.

## Details that matter

- **The parameters assume 5 m pixels.** `DEFAULTS` gives a spur length of 10 px and a minimum piece length of 60 px. Its comments call these 50 m and 300 m at 5 m per pixel. That matches the 1024 px road mask the 10 m tiles give at `UPSCALE: 2`. At `UPSCALE: 4` the same pixel values mean half the distance on the ground.
- **The threshold comes from the caller.** No default exists. The model configs set `ROAD_THRESHOLD` per model, for example 0.3216 for `sam.yaml` and 0.4118 for `unet.yaml`. Nothing says the CRESI run uses the same values.
- **Only the skimage cleanup runs.** `preprocess` uses morphological closing and opening with 7 px kernels only for images over 10^10 pixels. A 1024 px mask never takes that branch. So `cv2_kernel_close` and `cv2_kernel_open` do nothing. `kernel_blur` and `min_background_frac` reach `make_skeleton` and are never read.
- **Spurs at the tile border survive.** `remove_small_terminal` keeps any edge with an end node within 4 px of the mask border. Roads that the tile edge cuts short are not pruned.
- **Gap bridging is generous at this resolution.** Two dead ends join when they are under 24 px apart, with no direction check. Dead ends 24 to 80 px apart join when their end directions line up within 30 degrees. At 5 m pixels that is 120 m and 400 m. A test on a synthetic mask bridged an 18 px gap.
- **Bridging runs before piece removal.** Two fragments that are each under 60 px can join and then pass the 60 px check together.
- **Large pieces skip the length check.** `clean_sub_graphs` only measures pieces with 20 nodes or fewer. Any piece with more nodes stays, however short it is.
- **One duplicate edge empties the tile.** `wkt_list_to_nodes_edges` returns `None` when it sees the same edge twice. The wrapper then prints a warning and returns an empty graph for the whole tile.
- **The adjacency dict is one-way.** `edges_xy_to_adj` stores each edge only under its first node and gives the second node an empty list. The SAM-Road output from `convert_to_sat2graph_format` stores both directions. `eval_graphs.py` copes with this. TOPO calls `ReverseDirectionLink`, and the APLS export removes duplicate edges.

## Why it's like this

- **CRESIv2, minimally adapted.** The package README says the code is for an ablation and was minimally adapted from CRESIv2. It cites the WACV 2020 paper and https://github.com/avanetten/cresi. The ablation compares what with what, and no doc records it. Reason unknown, see [KEL-45](https://linear.app/kelvinwei/issue/KEL-45).
- **What we changed from upstream.** A diff against CRESI `main` shows these changes:
  - `cresi_extract.py` is new. It replaces CRESI's config-driven `main` with one call per mask and fixes the parameters in `DEFAULTS`.
  - `skeletonize.py` comes from `04_skeletonize.py`. The `sknw_int64` branch for images over 32767 px is gone. A code comment says it no longer builds under numba 0.65, and our masks are 1024 px. The `remove_small_*` calls use the `max_size=` keyword in place of the removed `in_place=` form.
  - `wkt_to_graph.py` comes from `05_wkt_to_G.py`. The `utm`, `fiona`, `osmnx` and `osgeo` imports are gone. A code comment says only the pixel-space functions are reused.
  - `sknw.py` and `rdp.py` match upstream byte for byte, apart from line endings.
- **The parameter values.** Upstream's tutorial config uses a threshold of 0.25, a minimum piece length of 600 px and a 12 m spur length. Its script uses a 300 px hole size. Ours are 4 px holes, 10 px spurs and 60 px pieces. Upstream's gap-bridging distances stay unchanged. Reason unknown, see [KEL-46](https://linear.app/kelvinwei/issue/KEL-46).
- **No caller.** Commit `a204fa1` added the package. No later commit wires it into `inferencer.py`, `eval_graphs.py`, `train.py` or `triage.py`. Reason unknown, see [KEL-45](https://linear.app/kelvinwei/issue/KEL-45).

## Weak spots and ideas

- **Dead package.** Observed. Nothing in the repo imports `mask_postprocessing`. The pipeline can only run by hand on the `mask/<tile>_road.png` files that `inferencer.py` writes. A small CLI, or a flag in `inferencer.py`, would make the ablation reproducible.
- **Most of the vendored code is dead.** Observed. The wrapper never reaches `dl_post_process_pred`, `cv2_skeletonize`, `build_wkt_dir`, `main`, `wkt_to_G`, the geo-coordinate functions, `remove_short_edges`, `wkt_to_shp`, `shp_to_G`, `skeletonize.clean_sub_graphs` or `sknw.draw_graph`. `rdp.py` only serves `wkt_to_G`, so it is dead too. Some of it would crash if called, because it uses the removed `Config`, `make_logger` and `fiona`, or `graph.node`, which networkx 3 lacks. Trimming to the live path would cut about 2,500 lines to a few hundred.
- **Gap-bridging distances were tuned for 0.3 m imagery.** Observed constants, suspected effect. At 0.3 m pixels, 24 px is about 7 m. At 5 m pixels it is 120 m, with no direction check. This can join parallel roads or close loops that are not real. `add_small_segments` should take these as `DEFAULTS` entries in metres.
- **Pixel constants do not follow the resolution.** Observed. `DEFAULTS` holds pixel counts with a comment that assumes 5 m pixels. A 2.5 m run at `UPSCALE: 4` would halve every distance unless someone edits them. Metres and a pixel size as inputs would remove the trap.
- **Dead settings in the API.** Observed. `road_png_to_nx` passes 22 positional values into `img_to_ske_G`. Of the settings, `cv2_kernel_close`, `cv2_kernel_open`, `kernel_blur`, `min_background_frac` and `connect_crossroads` have no effect. Callers can change them and see nothing happen.
- **Input is a file path.** Observed. `road_png_to_nx` reads a PNG from disk, though the inferencer already holds the mask as an array.
- **Duplicated thinning logic.** Observed. `thin_road_mask` in `inferencer.py` repeats the threshold, speck removal and skeletonize steps with its own setting, `THIN_HOLE_SIZE`, which is 16 in the configs. The CRESI path uses 4. The two can drift apart.
- **Whole-tile failure on a duplicate edge.** Observed in code. How often it fires on real masks is unknown. Skipping the duplicate edge would keep the rest of the tile.
- **One-way adjacency and no tests.** Observed. Evaluation copes with the one-way dict, but a new consumer that expects both directions will see half the edges. No test covers the package. The synthetic mask run above would make a cheap regression test.

## Code map

- `src/geosamroad/mask_postprocessing/cresi_extract.py`, `DEFAULTS`, `road_png_to_nx`, `nx_to_edges_xy` and `edges_xy_to_adj`: our wrapper and the only entry point.
- `skeletonize.py`, `make_skeleton`, `preprocess`, `img_to_ske_G`, `remove_small_terminal`, `G_to_wkt` and `add_small_segments`: threshold, clean, thin, build the graph, prune spurs and bridge gaps. Adapted from CRESI `04_skeletonize.py`.
- `sknw.py`, `build_sknw`: skeleton pixels to a networkx graph. Vendored from the sknw project.
- `wkt_to_graph.py`, `wkt_list_to_nodes_edges`, `nodes_edges_to_G` and `clean_sub_graphs`: WKT lines back to a graph, then drop small pieces. Adapted from CRESI `05_wkt_to_G.py`.
- `rdp.py`, `rdp`: Ramer-Douglas-Peucker simplification. Only used by dead code.
- `README.md`: the source and the citation.
