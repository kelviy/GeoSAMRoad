# Road vectors

How `sentinel2data.cli roads` turns one raw road source into the road parquet that `generate` reads.

## Core idea

Each road source names its road classes in its own way. The `roads` command reads one source and keeps only the classes that count as roads. It gives every line a fixed set of columns, so all sources look the same to `generate`. A lookup table per source maps each class to a size group and a buffer half-width in metres. The output is one GeoParquet in WGS84. Each line records its source, its class, its size group, its buffer and its surface type, such as paved.

## How it works

1. **Pick one source.** The command takes exactly one of `--cdngi`, `--overture` or `--frst`, plus `--out`. Zero or two sources is an error.
2. **Read and filter.** Each source class reads its own format and keeps the road classes in its table.
   - CDNGI reads the `TRAN_ROADS_EXP` layer of every `*.gpkg` under a folder, one GeoPackage per province. A SQL `where` on `FEAT_TYPE` keeps only the listed classes.
   - Overture reads a GeoParquet from the Overture CLI. It keeps rows with `subtype == "road"` and a `class` in the table. A row whose `subclass` is `link` gets the class name `<class>_link`, for example `motorway_link`.
   - FRST reads a shapefile and keeps every line. It reads only the `Surface` column.
3. **Fill the columns.** `road_class` is the source's class name. `scale_class` and `buffer` come from the table. FRST has no classes, so its `road_class` and `scale_class` are `unknown` and its `buffer` is 10 m.
4. **Reproject to WGS84.** The CRS is EPSG:4326.
5. **Check and write.** `RoadVectorExtractor.build` fills `surface` with `unknown` where it is missing. It rejects any extra column and puts the columns in a fixed order. A `.gpkg` path writes a GeoPackage layer named after the source. Any other path writes GeoParquet with a bounding-box column.

## The classification tables

`CDNGI_ROAD_CLASSIFICATION` and `OVERTURE_ROAD_CLASSIFICATION` in `generator/config.py` nest classes under a size group. `flatten_classification` turns a table into three things. It returns a class-to-group map, a class-to-buffer map and the list of kept classes. It skips the `ignored` group by default.

| `scale_class` | CDNGI classes | Overture classes | Buffer |
| --- | --- | --- | --- |
| large | National Freeway, National Road, Arterial Road, Main Road | motorway, trunk, primary, secondary | 8 m |
| medium | On/OffRamp | tertiary | 6 m |
| medium | Secondary Road, Other Road | unclassified, residential | 5 m |
| links | none | motorway, trunk and primary links at 8 m, secondary_link at 6 m, tertiary_link at 5 m | 5 to 8 m |
| small | Street | living_street | 4 m |
| small | none | track | 3 m |
| ignored | Footpath, Track, Slipway | pedestrian, path, footway, service, unknown, steps, bridleway, cycleway | dropped |

The buffer is a half-width. A comment in the config notes that 5 m gives a road one 10 m pixel wide.

## Details that matter

- **What gets dropped.** CDNGI drops Footpath, Track and Slipway, and any `FEAT_TYPE` missing from the table. Overture drops every class outside the table, and every row whose `subtype` is not `road`. Nothing counts or reports these drops. FRST drops nothing.
- **Strict checks fail the whole run.** An Overture link whose `<class>_link` is not in the table raises an error, for example a `residential` link. An FRST `Surface` value other than paved, unpaved or empty raises an error. A null in any output column raises an error.
- **`surface` only means something for FRST.** FRST lowercases `Surface` and turns empty values into `unknown`. CDNGI and Overture rows always get `unknown`.
- **Buffers are metres, but the file is in degrees.** The output stays in WGS84. `generate` reprojects each zone's roads to the zone's CRS before it buffers them, so `generate` applies the half-width in metres.
- **How `generate` uses the file.** `load_zone_roads` in `generator/labels.py` reads only the roads inside each zone's bounding box, using the bounding-box column. `exclude_road_classes` then drops classes by `road_class`, by default Footpath, Track and Slipway. `RasterMaskLabeler.rasterize` buffers each line by its `buffer` value. `generate` copies the other columns into the tile graph parquets, and the zone packager copies them into the zone GeoPackage. See [dataset tiling](dataset-tiling.md) and [zone cleaning](zone-cleaning.md).
- **`generate` needs GeoParquet.** It reads the file with `gpd.read_parquet`. A `.gpkg` output works for QGIS but not as `generate` input.
- **`union_with` does nothing.** `configs/rosa.yaml` has a commented `union_with` key in its `roads` section. The `roads` command has no such option. If someone uncomments it, the `--config` loader passes it to Click as a default, and Click ignores it without a warning. No code merges two road files.

## Why it's like this

- **CDNGI is the label source.** The KEL-5 spec builds the raw labels from the CDNGI layer. Overture and OSM are second sources for flags only, because they are ODbL and copying their geometry would make ROSA a derivative database.
- **Track and Slipway are ignored.** They are not roads under the ROSA definition in `GLOSSARY.md`. The config comment says Track is faint vehicle tracks and Slipway is boat ramps. Commit 3695d4e, for KEL-8, moved them into `ignored`. Before that they were `small` with a 3 m buffer. `generate` drops them a second time, because road parquets built before that commit still hold them.
- **Per-class buffers.** Mask width follows road size. The config comment notes that 5 m gives a road one pixel wide at 10 m. The Overture link buffers carry the comment "not certain about the buffers".
- **GeoPackage output.** The KEL-5 spec makes GeoPackage the editing format and GeoParquet the pipeline format.
- **One source per run.** The command reads one source and writes one file. Reason unknown, see [KEL-17](https://linear.app/kelvinwei/issue/KEL-17).
- **FRST gets a 10 m buffer.** The code comment says FRST has no buffer values, so the code uses a constant. Why 10 m, twice the CDNGI Other Road width, is not recorded. Reason unknown, see [KEL-18](https://linear.app/kelvinwei/issue/KEL-18).
- **Overture keeps `track`.** It is `small` with a 3 m buffer, while CDNGI Track is ignored. Reason unknown, see [KEL-19](https://linear.app/kelvinwei/issue/KEL-19).

## Weak spots and ideas

- **Overture tracks become labels.** Observed. The excluded classes in `EXCLUDED_ROAD_CLASSES` are CDNGI names only. An Overture parquet keeps `track` lines, and `generate` does not drop them. That breaks the ROSA road definition if an Overture parquet is ever passed to `generate`.
- **`generate` cannot exclude Overture or FRST classes by default.** Observed. The class names differ between sources, and FRST has only `unknown`. Excluding by `scale_class` or a shared class vocabulary would work across sources.
- **`--cdngi` with a single file fails.** Observed. The help text says it takes a GeoPackage file or a folder. `CdngiSource._gpkg_paths` calls `rglob` on the path, which finds nothing for a file. The run then fails with "No road features extracted".
- **The config pre-fill conflicts with the command line.** Observed from the code. `rosa.yaml` sets `overture` under `roads`. Running `--config rosa.yaml roads --cdngi <dir>` gets both sources and fails the exactly-one check.
- **`union_with` is a dead config key.** Observed. Either build it, by concatenating an existing road parquet with the new source, or delete the line.
- **Silent drops.** Observed. The command prints a segment count per source, but no count per dropped class. `generate` reports its own exclusions, so the `roads` step is the only one with no record.
- **No geometry checks.** Observed. The code does not check for empty or invalid geometries, explode multi-part lines, remove duplicates or node crossings. [Dataset tiling](dataset-tiling.md) notes that intersections depend on a noded source.
- **No tests.** Observed. No test imports `supplementary/roads.py`. The synthetic zone fixture writes its road parquet by hand with `flatten_classification`.
- **Code structure.**
  - The null check and its error message are copied in all three source classes. `build` could run it once.
  - `RoadVectorExtractor.from_paths` repeats the exactly-one check from the CLI, and its last `else` branch cannot run.
  - `OvertureSource.__init__` calls `flatten_classification` twice to get two class lists.
  - `CdngiSource.load` parses a `province` from the file name and uses it only in print output.
  - The tables live in `generator/config.py`, but `supplementary/roads.py`, `cleaning/contract.py` and the test fixture all read them. A small `road_classes` module would make that shared role clear.
  - Progress goes through `print`, not logging.
- **FRST width is a guess.** Suspected. A 10 m half-width paints about two pixels for every FRST road, highways and farm roads alike. Nobody has checked that against the imagery.

## Code map

- `src/sentinel2data/cli.py`, `roads`: the CLI entry point and the one-source check.
- `src/sentinel2data/supplementary/roads.py`, `RoadSource`: the interface, with `data_source`, `gpkg_layer_name` and `load`.
- `supplementary/roads.py`, `CdngiSource`, `OvertureSource`, `FrstSource`: read, filter and fill columns for each source.
- `supplementary/roads.py`, `RoadVectorExtractor.from_paths` and `build`: pick the source, enforce the schema and write the file.
- `src/sentinel2data/generator/config.py`, `CDNGI_ROAD_CLASSIFICATION`, `OVERTURE_ROAD_CLASSIFICATION`, `flatten_classification`, `EXCLUDED_ROAD_CLASSES`: the class tables and the classes `generate` removes.
- `src/sentinel2data/configs/rosa.yaml`, `roads` section: example paths for each source and the unused `union_with` key.
- `src/sentinel2data/generator/labels.py`, `load_zone_roads`, `exclude_road_classes`, `RasterMaskLabeler`: where `generate` reads the output.
