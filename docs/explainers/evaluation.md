# Evaluation

How `python -m geosamroad.eval_graphs` scores predicted road graphs against the tile road graphs with the TOPO and APLS metrics.

## Core idea

Each test tile has two road graphs. One is the label graph that tiling wrote from the raw labels. The other is the graph the inferencer predicted. The script scores each pair with two graph metrics taken from Sat2Graph and SAM-Road. TOPO asks a local question. Walk a fixed distance along the roads from many start points, drop points at even spacing, and count how many points on one graph have a partner on the other. That gives precision and recall. APLS asks a routing question. Pick points on one graph, compute the shortest path between each pair, and check that the same trip has a similar length on the other graph. A gap in a road makes APLS fall even when TOPO barely moves. The reported number for each metric is the plain mean over tiles.

## How it works

1. **List the tiles.** `read_split_csv` reads the split CSV, by default `test`. For each tile the script loads the label graph from the tile's `mask_adj_graphs/*.pkl` and the prediction from `<pred-dir>/<tile>.p`. Both are adjacency dicts in native 10 m pixel coordinates, keyed by x, y. The [inference](inference.md) explainer covers how the prediction pickle is made.
2. **Scale both graphs.** `scale_adj` multiplies every coordinate by `--metric-scale`, which is 4 by default. One coordinate unit is then 2.5 m of ground, and a 512 px tile spans 2048 units.
3. **Fake a GPS frame.** Both upstream metrics work in latitude and longitude. `xy2latlon` places the tile at a made-up anchor of 41.0 N, 71.0 W and treats one unit as 1/111111 of a degree, which is one metre in the metric's frame. The x coordinate becomes latitude and y becomes longitude.
4. **Run TOPO in process.** `run_topo` builds two `RoadGraph` objects with `build_road_graph`. It calls the upstream functions `TOPOGenerateStartingPoints`, `TOPOGeneratePairs` and `TOPOWithPairs` from `metrics/topo/topo.py`. Then it reads the last line of four numbers from the tile's text file.
5. **Run APLS as a Go binary.** `build_apls_binary` compiles `metrics/apls/main.go` once per run into `<out-dir>/apls_bin`. For each tile, `adj_to_apls_json` writes both graphs as `[nodes, edges]` JSON. `run_apls` calls `apls_bin gt.json prop.json out.txt` and reads the third number in the output.
6. **Aggregate.** The script writes one row per tile to `results.csv` with `topo_precision`, `topo_recall`, `topo_overall_recall` and `apls`. It prints the `nanmean` of each column.

## Details that matter

- **TOPO parameters are set in ground metres** and converted to degrees through the metric scale. The matching radius is 30 m, which is 12 units. The marble spacing is 15 m, which is 6 units. The walk radius from each start point is 400 m, which is 160 units. The upstream defaults are fixed degrees: a 0.00010 matching radius, a 0.00005 interval and a 300 m walk radius for CityScale.
- **APLS parameters are fixed in `main.go`** and assume 2048 m tiles. With the scale of 4, one APLS "metre" is 2.5 m of ground. APLS densifies edges to one node every 2 units, which is 5 m of ground. It snaps a control point to the other graph within 10 units, which is 25 m. It ignores pairs whose label graph path is shorter than 100 units, which is 250 m. It only places control points more than 100 units, or 250 m, inside the 2048 unit region.
- **The scale of 4 is what fits the tile to the APLS constants.** A 512 px tile at scale 4 is exactly the 2048 unit region that `region_size` expects. Any other `--metric-scale` keeps the TOPO metres correct but changes every APLS distance in ground terms.
- **APLS is symmetric.** The binary scores the label graph against the prediction and the prediction against the label graph, then writes both and their mean. The script keeps only the mean.
- **TOPO recall comes in two forms.** `topo_recall` is the mean recall over the start points that found a partner on the predicted graph. `topo_overall_recall` multiplies it by coverage, the share of label graph start points that found a partner. Coverage is read but not saved. Upstream SAM-Road reports overall recall as its TOPO recall.
- **One-to-one matching is on.** `TOPOWithPairs` runs with `one2oneMatching=True`, so each marble matches at most one hole.
- **The pairing threshold is our matching radius.** Upstream calls `TOPOGeneratePairs` with a fixed 0.00010. `run_topo` passes the same 30 m radius it uses for marble matching.
- **Empty graphs.** A tile with an empty label graph is skipped and does not count. A tile with a label graph but an empty prediction scores 0 on every metric. A tile with a missing prediction pickle is skipped.
- **Failures become NaN.** A TOPO or APLS exception on one tile prints a message and leaves that value as NaN. `nanmean` then leaves the tile out of that metric's mean, so the TOPO and APLS means can cover different tiles.
- **A tile with too little road for TOPO scores 0.** If the label graph gives no start points, upstream `TOPOWithPairs` divides by zero, catches it and writes `0 0 0 0`. The tile counts as precision 0 and recall 0.
- **Predicted nodes sit on whole native pixels.** `convert_to_sat2graph_format` rounds prediction coordinates to integers at 10 m. Label graph nodes keep 0.01 px precision. Each predicted node can sit up to half a pixel, or 5 m, off on each axis.
- **No Go means no APLS.** If `go` is not on `PATH` or the build fails, the script prints a warning and every `apls` value is NaN. Nothing fails.
- **Outputs.** The default output root is `<pred-dir>/../results`. It holds `results.csv`, `topo/<tile>.txt`, `apls/<tile>_gt.json`, `apls/<tile>_prop.json`, `apls/<tile>.txt` and `apls_bin`. `triage.py` reads `results/results.csv` to rank tiles for review.

## Why it's like this

- **TOPO and APLS.** Both metrics come from the SAM-Road and Sat2Graph code, and `README.md` lists both projects under acknowledgements. No document records why ROSA uses these two metrics and no others. Reason unknown, see [KEL-41](https://linear.app/kelvinwei/issue/KEL-41).
- **A metric scale of 4.** The code comment says scale 4 makes one unit 2.5 m. The APLS README says its constants are set for 2048 by 2048 tiles. The written reason for choosing 4 is missing. Reason unknown, see [KEL-42](https://linear.app/kelvinwei/issue/KEL-42).
- **TOPO radii of 30 m, 15 m and 400 m.** They appear in `parse_args` with no comment. Reason unknown, see [KEL-43](https://linear.app/kelvinwei/issue/KEL-43).
- **Scoring TOPO recall without coverage as its own column.** The code keeps both forms of recall and gives no reason. Reason unknown, see [KEL-44](https://linear.app/kelvinwei/issue/KEL-44).
- **Go binary built per run.** Commit `a204fa1` deleted three prebuilt `apls_bin` files from `metrics/apls/save/`. Its message says "Clean up APLS". The script now builds into the output folder.

## Weak spots and ideas

- **The label graph comes from the raw labels.** Every score measures agreement with the uncleaned CDNGI overlay. A model that draws a real road the labels miss loses precision. Scores on cleaned labels will need a re-run.
- **Mean over tiles, unweighted.** A tile with 50 m of road counts as much as a tile with 20 km. Nothing reports a TOPO F1, and no pooled score over the whole split exists. Upstream SAM-Road computes a per-tile F1 from precision and overall recall in `cityscale_metrics/topo.py`. Our CSV cannot be compared with it directly.
- **Different tile sets per metric.** NaN tiles drop out of one mean and not the other. The printout does not say how many tiles each mean covers.
- **TOPO scores a short-road tile 0.** This drags means down for tiles whose roads are too short for a start point. It is observed in code. Nobody has counted how many tiles it hits.
- **Tile edges are not handled.** Each tile is scored alone. A road cut by the tile border has two loose ends in both graphs, and APLS ignores the outer 250 m band for control points. Scores on a stitched zone graph would differ. This is suspected, not measured.
- **The TOPO region margin is inert.** `run_topo` pads the region by 300 units in latitude and 500 units in longitude. Upstream `TOPOGenerateStartingPoints` drops start points within 7% of the region edge. For a 2048 unit tile the padding is larger than that 7%, so no start point is dropped. This is worked out from the code, not tested.
- **Upstream code writes files into the working directory.** `metrics/topo/topo.py` calls `showTOPO.RenderRegion` and `RenderRegion2` on every tile, which overwrite `gt.svg` and `coverage.svg` in the folder the script runs from. Our SAM-Road copy in `sam_road/cityscale_metrics/topo/topo.py` comments these calls out. `main.go` also tries to write `debug/cp.json` and ignores the error.
- **`sys.path` import hack.** `eval_graphs.py` puts `metrics/topo` on `sys.path` and imports modules named `graph` and `topo` as top-level modules. Any other module called `graph` on the path would collide. A package with relative imports would remove the risk.
- **Two TOPO recall definitions in one CSV with no doc.** The column names do not say which recall matches the literature.
- **Hard-coded GPS anchor.** The lat and lon anchor, the 111111 factor and the `cos(41°)` correction appear in `eval_graphs.py`, `convert.py`, `metrics/topo/main.py` and `main.go`. They must agree, and nothing checks that they do.
- **Duplicated conversion logic.** `adj_to_apls_json` reimplements `metrics/apls/convert.py`, and adds a fix for neighbours that are not keys. `build_road_graph` reimplements `create_graph` in `metrics/topo/main.py`.
- **Dead upstream code.** `eval_graphs.py` never calls `metrics/topo/main.py` or `metrics/apls/convert.py`. `metrics/topo/main.py` still reads `../spacenet/data_split.json`. Only `topo.py`, `graph.py` and `showTOPO.py` are used. The whole of `sam_road/cityscale_metrics/` and `sam_road/spacenet_metrics/` is unused by our code. Only `sam_road/README.md` points at them. The SpaceNet copy is byte-identical to `metrics/` for `topo.py`, `graph.py` and `main.go`. The CityScale copy differs in `topo.py`, where the SVG calls are commented out and Python 2 `iteritems` remains, and in the small-tile `interval_2` of `main.go`, which is 15 instead of 10. Deleting both folders would remove about 300 KB of duplicate code.
- **No tests.** Nothing under `tests/` covers `eval_graphs.py`. The scale and the degree conversions are the parts most worth a test with a known graph pair.

## Code map

- `src/geosamroad/eval_graphs.py`, `main` and `parse_args`: the CLI, the tile loop, the CSV and the means.
- `eval_graphs.py`, `scale_adj` and `xy2latlon`: scale native pixels to scoring units and units to fake degrees.
- `eval_graphs.py`, `build_road_graph` and `run_topo`: build TOPO graphs, run the three upstream TOPO steps and parse the result line.
- `eval_graphs.py`, `adj_to_apls_json`, `build_apls_binary` and `run_apls`: write APLS JSON, compile the Go binary and run it per tile.
- `src/geosamroad/metrics/topo/topo.py` and `graph.py`: upstream Sat2Graph TOPO. `showTOPO.py` holds SVG helpers that `topo.py` imports.
- `src/geosamroad/metrics/apls/main.go`, `go.mod`: upstream APLS in Go, needs `github.com/dhconnelly/rtreego` v1.2.0.
- `src/geosamroad/metrics/topo/main.py` and `metrics/apls/convert.py`: upstream entry points that our code does not use.
- `src/geosamroad/sam_road/cityscale_metrics/` and `sam_road/spacenet_metrics/`: SAM-Road's bash-driven metric copies, not used by ROSA.
