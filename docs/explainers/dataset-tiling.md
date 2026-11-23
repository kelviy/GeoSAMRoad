# Dataset tiling

How `sentinel2data.cli generate` turns each zone's Sentinel-2 image and the road vector layer into the ROSA tiles.

## Core idea

Each zone is one large image. Lay the road vectors directly on top of it and burn them into a road mask. Then cut the image and the mask with a fixed grid of 512 by 512 px squares that starts at the zone's top-left corner. Keep a tile if it has at least one road pixel and no missing pixels. Nothing checks that the roads line up with the imagery. The labels are a naive overlay, and that is why the cleaning step exists.

## How it works

1. **Find the zones.** The imagery folder is already split into `train/`, `val/` and `test/`, and each holds one image per zone. The folder sets the zone's split. The biome and urbanisation class come from the file name.
2. **Load the zone's roads.** Read the roads inside the zone's bounding box from the road parquet. Reproject them to the zone's CRS and clip them to the zone edge.
3. **Remove non-roads.** Drop Footpath, Track and Slipway, the classes that are not roads under the ROSA definition. The generation report records how many segments and metres each class lost in each zone.
4. **Burn the mask once for the whole zone.** Buffer each road line by a half-width that depends on its class. Rasterize the buffered lines to a 0/1 mask the same size as the zone image.
5. **Walk the grid.** Visit each 512 px grid position, row by row, and give it one status:
   - `untiled`: the partial square at the right or bottom edge. It is discarded.
   - `dropped_no_road`: the mask has no road pixels.
   - `dropped_nan`: the imagery has at least one NaN pixel.
   - `kept`: everything else. The generator writes its five artifacts.
6. **Tag over the whole dataset.** After every zone is done, each kept tile gets a Low, Medium or High road density class. The class boundaries are Jenks breaks over all tiles. Then the generator writes the metadata table, the split CSVs and the generation report.

## Details that matter

- **Buffer half-widths.** They come from the road parquet's `buffer` column, which the `roads` command fills from `CDNGI_ROAD_CLASSIFICATION`. National, arterial and main roads get 8 m. Ramps get 6 m. Secondary and other roads get 5 m. Streets get 4 m. At 10 m pixels, a 5 m half-width gives a road about one pixel wide.
- **`all_touched=True` when rasterizing.** Any pixel the buffered polygon touches becomes a road pixel. Roads end up one or two pixels wider than their buffer suggests, most of all on diagonals.
- **The grid starts at pixel 0, 0 and does not overlap.** A zone 5,300 px wide gives 10 full columns and a 180 px strip that is never used. Every position gets a row in the zone's `tile_status` table, the partial ones included, so the discarded area shows up in QGIS.
- **The road check runs before the imagery is read.** Rejecting a road-free tile costs almost nothing.
- **Bands.** The generator removes the S1 descending bands from every tile. It appends three enhanced RGB bands: each band is rescaled to 0 to 1, gamma-corrected with gamma 0.4, then put through CLAHE. The rescale uses each tile's own min and max.
- **The five artifacts of a kept tile.** All are named `<zone_filename>_r<row>_c<col>`.
  - `imagery/*.tif` holds the bands as a COG with 256 px internal blocks.
  - `masks_raster/*.tif` holds the tile's slice of the zone mask.
  - `masks_graph/*.parquet` holds the road lines clipped to the tile, in map coordinates.
  - `mask_adj_graphs/*.pkl` holds an adjacency dict in pixel coordinates. Every vertex of every line is a node, keyed by its x, y position rounded to 0.01 px. Two lines join only where they share an exact vertex.
  - `mask_keypoints/*.tif` draws a 3 by 3 px square on every node whose degree is not 2, which means an intersection or a dead end. Nodes within 1 px of the tile border are skipped, because those are points where the clip cut a road.
- **Road density** is the road pixel count divided by 512 squared. Tiles with zero density get `Empty`. When the dataset has fewer than three distinct densities, it gets fewer than three classes.
- **Dates.** `satellite_image_dates` is hard-coded to 2020-01-01 until 2021-01-01 for every tile.

## Why it's like this

- **Fixed grid from the top-left corner, no overlap, partial edge squares discarded.** Reason unknown, see KEL-13. The cleaning spec keeps the grid fixed for now, and `docs/cleaning/open-directions.md` discusses moving tiles to cover more valuable roads.
- **Split per zone, not per tile.** Reason unknown, see KEL-14.
- **Drop road-free tiles, and drop a tile for a single NaN pixel.** Reason unknown, see KEL-15.
- **Per-tile contrast stretch for the enhanced RGB.** Reason unknown, see KEL-16.
- **Naive overlay.** The raw labels are the frozen baseline that cleaning starts from, so they must be reproducible from the source layer alone. Correcting labels against the imagery is the cleaning spec's job, as ADR 0001 sets out.

## Weak spots and ideas

- **Each zone loses up to 511 px** on its right and bottom edges. Centring the grid, or letting the last tile overlap, would use that area.
- **Label width follows the class, not the real road.** A wide urban street and a narrow rural "Other Road" get the same mask width. `all_touched` widens both further.
- **Intersections depend on a noded source.** Two roads that cross without a shared vertex stay unconnected in the adjacency graph and get no keypoint. Nobody has measured how often this happens in CDNGI.
- **The enhanced RGB is stretched per tile.** Its brightness cannot be compared with a neighbouring tile's. A per-zone stretch would keep tiles consistent and avoid seams when tiles are stitched back together.
- **One NaN pixel drops the whole tile,** even a single corner pixel. A threshold would keep more tiles. Nobody has tested whether that hurts training.
- **API shape.** `ROSAProcessor._tile_zone` walks the grid, decides keep or drop, builds the artifacts and writes the files, all in one method. It also calls `RoadGraphLabeler._clip_roads`, a private method of another class. The pipeline never calls `RasterMaskLabeler.generate` or `RoadGraphLabeler.generate`, the methods the `LabelGenerator` base class defines. One cleaner split is three parts. A tile plan lists the grid positions and their status. A tile builder makes the arrays and graph for one tile. A writer saves them.

## Code map

- `src/sentinel2data/cli.py`, `generate`: the CLI entry point.
- `generator/pipeline.py`, `make_rosa_pipeline` and `RosaPipeline.run`: find the zones, run each one, tag density, write the metadata and the report.
- `generator/processor.py`, `ROSAProcessor.process`, `_tile_zone` and `_tile_verdict`: turn one zone into tiles and apply the keep or drop rule.
- `generator/labels.py`, `load_zone_roads`, `exclude_road_classes`, `RasterMaskLabeler.rasterize` and `RoadGraphLabeler._clip_roads`: load, filter, rasterize and clip the roads.
- `generator/artifacts.py`, `native_adjacency` and `keypoint_mask_from_adjacency`: build the pixel graph and the keypoint mask.
- `generator/helper.py`, `enhance_rgb`: make the enhanced RGB bands.
- `generator/tagging.py`, `RoadDensityClassifier`: assign the density classes.
- `generator/config.py`: buffers, excluded classes, dropped bands, tile size and the metadata schema.
- `generator/naming.py`, `parse_zone_filename`: read zone metadata from the file name.
