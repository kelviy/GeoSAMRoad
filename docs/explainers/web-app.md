# Web app

How the GeoSAMRoad web app lets a user pick cells on a map and get back a predicted road graph. It is one FastAPI process in `src/geosamroad_app/`, shipped as one Docker container from `deploy/`.

## Core idea

The map shows a fixed grid of square cells, each one exactly the area of one 512 px fetched image. The user clicks up to 24 cells, picks a model and a date, and presses run. The server fetches a cloud-free Sentinel-2 and Sentinel-1 composite for each cell from Earth Engine and runs the model on it. It sends the predicted roads back as GeoJSON lines with a true-colour preview of each cell. Each cell is predicted on its own, and nothing joins roads across cell edges. One job runs at a time, and jobs live only in memory.

## How it works

1. **Load the page.** `GET /` serves `index.html` with a hash of `app.js` and `style.css` appended to their URLs. `app.js` calls `/api/config` for the variants that have a checkpoint on disk, the cell size and the per-job limit.
2. **Draw the grid.** After every pan or zoom, with a 200 ms debounce, the browser asks `/api/tiles` for the cells in the viewport. `grid.tiles_for_viewport` returns them as GeoJSON. Above 600 cells it returns none, and the panel says to zoom in.
3. **Select.** A click toggles a cell. The user picks an encoder, a date and a basemap.
4. **Submit.** `POST /api/run` checks that checkpoints exist, that the variant is known and that every cell id parses. Then `jobs.registry.submit` queues the job and returns its id. The browser polls `/api/jobs/<id>` every 1.2 s and draws a progress bar from `done`, `total` and `message`.
5. **Run the job.** `pipeline.run` starts Earth Engine and walks the cells in order. For each cell it:
   - fetches the composite into a scratch folder with `globalurbanmapper_gee.api.fetch_region`, in EPSG:4326 at the grid's own pixel size, so the written image is the cell;
   - loads the model on the first cell that has imagery, with `runner.load_model`;
   - runs `runner.infer_tile`, which upsamples the image to 1024 px and calls `inferencer.infer_one_tile`. That function fuses overlapping patch predictions into one mask per cell and extracts the graph, as [inference](inference.md) describes;
   - converts each graph node from pixels to lon and lat, makes one two-point LineString per edge, and writes a PNG preview;
   - deletes the scratch imagery and keeps the preview.
6. **Finish.** `pipeline._release` frees GPU memory. The result holds every cell's lines in one FeatureCollection, the preview list, the totals and the failed cells.
7. **Show.** The browser turns the result into a layer of previews with the roads drawn on top. Several layers can sit side by side. Save writes a layer to a GeoJSON FeatureCollection. A `geosamroad` member in that file holds the metadata and, if the user ticks the box, the previews as data URLs. Load reads such a file back as a new layer.

## Details that matter

- **The grid is global and in degrees.** One pixel is `PIXEL_DEG` = 9e-5°. One cell is 512 px, so `TILE_DEG` = 0.04608°, about 5.1 km north to south. Cells are anchored at lon 0, lat 0, and the id is `<col>-<row>`, for example `401--737` in the southern hemisphere. These cells are not ROSA tiles and do not line up with any zone grid.
- **Pixels are not square on the ground.** North to south a pixel is 9.95 m. East to west it is 9.29 m at 22° S, 8.68 m at 30° S and 8.23 m at 34.8° S. The model was trained on 10 m UTM pixels, so the input is squeezed sideways in the south of the country.
- **The composite window is 182 days on each side of the date.** The default date of 2020-06-01 gives 2019-12-02 to 2020-11-30. The server falls back to today, with only a log warning, when the date does not parse.
- **Variants.** `variants.VARIANTS` lists four. They are `sgcn`, `unet` and `terramind_base`, which read the `s1s2` band list, and `sam_rgb`, which reads `rgb`. Each one points at a YAML in `geosamroad/configs/hpc`. The UI lists only variants with a `*.ckpt` in `<GEOSAMROAD_CHECKPOINTS>/<name>/`, sorted by a hard-coded road IoU from 0.469 for `sgcn` down to 0.427 for `sam_rgb`. If a folder holds several checkpoints, the app takes the first one by name and logs a warning. The [models](models.md) explainer covers the encoders.
- **Inference settings are pinned in `runner.py`.** `UPSCALE` is 2 and `INFER_PATCHES_PER_EDGE` is 16. With `CROP_SIZE` 128, that gives 256 patches of 256 px over each 1024 px image, starting every 51 px or so. `INFER_BATCH_SIZE` defaults to 4. The encoder features are cached in host RAM unless `GEOSAMROAD_CACHE_FEATURES_CPU` is not `1`.
- **One bad cell does not fail the job.** A cell fails when Earth Engine writes no full 512 px image or when inference throws. The cell goes into `tiles_failed` and the job carries on. The browser shows the first error only. The job fails only if Earth Engine start-up or the whole loop throws.
- **The preview uses a fixed stretch.** The raw red, green and blue reflectance is divided by 0.30, clipped, and raised to the power 0.8. Neighbouring previews therefore match in colour. The preview is 1024 px, the upsampled size.
- **The model loads once per job, not once per process.** The first cell with imagery pays the load. `_release` then drops the model, so every job reloads its checkpoint.
- **The queue is a lock.** `JobRegistry.submit` starts a daemon thread per job, and each thread waits on `_worker_lock`. A job reports `queued` until it gets the lock. Only one job runs at a time.
- **Nothing persists across a restart.** The registry is a dict in the process. It keeps the newest 50 jobs and drops older ones. Previews stay on the `geosamroad-data` volume under `jobs/<id>/previews/`, but a restart loses the job records that point at them.
- **Deployment.** The Dockerfile installs the `app`, `geosamroad` and `globalurbanmapper` extras, then pins torch 2.5.1 for CUDA 12.1 unless `TORCH_VARIANT=cpu`. It bundles MapLibre 4.7.1 at build time. Compose mounts the checkpoint folder and the Earth Engine key read-only, and reserves one NVIDIA GPU. Traefik routes `geosamroad.<BASE_DOMAIN_URL>` to port 8000. A `cloudflared` tunnel starts only under the `public` profile. `auth.initialize` reads the service account and the project from the key file when their variables are empty.

## Why it's like this

- **Cells in EPSG:4326.** The `grid.py` docstring says the cell the user hovers must be exactly the cell that gets fetched. Sampling in lon and lat means nothing gets reprojected between the map, the imagery and the road graph. The docstring calls the non-square pixels "a deliberate accuracy-for-alignment trade". Commit 8894f0e added the `crs` argument to `globalurbanmapper_gee` for this.
- **Roads as GeoJSON lines, not a raster.** The `runner.py` docstring says the model already makes a graph. Lines stay sharp at every zoom, the browser can style them, and no imagery gets reprojected.
- **A wide composite window.** A comment on `COMPOSITE_HALF_WINDOW_DAYS` says roads are static, so a long window only adds cloud-free pixels.
- **No Redis and one worker.** The `jobs.py` docstring says one box with one GPU needs no broker, and dropping it removes two services. Parallel jobs would fight over VRAM.
- **Release VRAM after every job.** The `_release` docstring says the torch caching allocator would keep several GB reserved, and the next job would hit CUDA out of memory. A reload costs seconds against a fetch that takes minutes.
- **Batch of 4 and features in host RAM.** Comments in `runner.py` say the `sgcn` decoder needs well over 6 GB at the shipped batch of 16. The cached features of a 16 by 16 patch grid take about 5 GB on the GPU. Both changes let a 6 GB card run the heaviest variant.
- **Call `infer_one_tile`, not the inference CLI.** The `runner.py` docstring says `inferencer.main` is built around split CSVs.
- **List only variants that have a checkpoint.** The `variants.available` docstring says a missing checkpoint should show as a missing option, not as a failed job. `sgcn` is the default because it has the best road IoU.
- **Sentinel-2 basemap by default.** A comment in `app.js` says the user should judge a cell on the sensor the model reads.
- **Cache-busting.** The `asset_version` docstring says caches such as Cloudflare's edge served an old `app.js` with a new `index.html`. The page then broke in ways that did not look like a cache problem.
- **CUDA 12.1 wheels.** A Dockerfile comment says the target GPU is GTX 1060 class and cannot run the default CUDA 12.8 wheels.
- **Public access is opt-in.** A compose comment says a plain `up` stays VPN-only. The app has no login. A Cloudflare Access policy is the only gate, and it must be active before the tunnel first starts.
- **At most 24 cells per job.** Reason unknown, see KEL-39.
- **Jobs kept in memory, newest 50 only.** Reason unknown, see KEL-40.

## Weak spots and ideas

- **No stitching across cells.** Observed. `pipeline.run` concatenates each cell's lines. A road that crosses a cell edge ends in two dead ends with no shared node. The edges carry no cell id. Running the cells with overlap, or merging nodes near shared edges, would give a connected graph.
- **Input geometry differs from training.** Observed in code. The model reads EPSG:4326 pixels that are 8.2 m to 9.3 m wide in South Africa. It was trained on square 10 m UTM pixels. Nobody has measured how much this costs. A UTM fetch with a reprojected graph would avoid it.
- **Stale comments that contradict the code.** Observed. The `_save_preview_wgs84` docstring says the model reads the cell in native UTM, and the `_pixels_to_wgs84` docstring talks about the granule CRS differing from a UTM zone. The `fetch_tile` docstring and the preview-bounds comment in `pipeline.run` say the same. Since `fetch_tile` forces `EPSG:4326`, the preview warp and the coordinate transform both go from EPSG:4326 to EPSG:4326 and do nothing.
- **Half-pixel offset.** Suspected. `_pixels_to_wgs84` maps a node's pixel index to the pixel's top-left corner, not its centre. That would put the graph about 2.5 m north-west of the imagery.
- **Batch size defaults disagree.** Observed. `runner.py` defaults `GEOSAMROAD_INFER_BATCH` to 4 for a 6 GB card. `docker-compose.yml` defaults it to 16, the value the runner comment says overflows such a card.
- **The 24-cell limit lives in three places.** Observed. `RunRequest.tile_ids` has `max_length=24`, `get_config` returns a literal 24, and `app.js` falls back to 24. One constant in `grid.py` or `jobs.py` would do.
- **The area estimate is too high.** Observed. `renderSelection` squares the north-south cell size of 5.1 km. At 30° S a cell is about 4.4 km wide, so the shown area is about 15 % too large. `grid.ground_resolution` exists for this and nothing calls it.
- **Dead code.** Observed. Nothing calls `grid.tile_image_corners` or `grid.ground_resolution`. `main.py` creates `DATA_ROOT/previews`, but previews go to `DATA_ROOT/jobs/<id>/previews`.
- **Disk use grows without limit.** Observed. Nothing deletes `jobs/<id>/` folders.
- **The queue order is not guaranteed.** Suspected. `threading.Lock` does not promise first in, first out, so a later job can run first. Every submit starts a new thread, with no cap on how many wait.
- **A bad date fails silently.** Observed. `RunRequest.date` is a free string, and `composite_window` centres on today when it does not parse. A 422 error would be clearer.
- **Road IoU values are hard-coded.** Observed. `VARIANTS` holds IoU figures "from the eval run" with no link to that run. They drive the sort order and the default. The `_rgb` configs for `sgcn`, `unet` and `terramind` have no variant.
- **External runtime dependencies.** Observed. MapLibre is bundled, but the browser loads basemaps from EOX, Esri and OpenStreetMap and searches places with Nominatim. The Dockerfile comment "works offline" holds only for the map library.
- **Personal path in `.env.example`.** Observed. `CHECKPOINTS_PATH` and `BASE_DOMAIN_URL` hold one machine's values, not placeholders.
- **No tests.** Observed. Nothing under `tests/` imports `geosamroad_app`. `grid.parse_tile_id` and `composite_window` are pure functions that would be cheap to test.
- **`pipeline.run` does a lot.** It handles Earth Engine start-up, the per-cell loop, model loading and result assembly. It also builds a relative preview URL that `app.js` then rewrites to an absolute API path. The server could build the absolute URL.

## Code map

- `src/geosamroad_app/main.py`, `RunRequest`, `run`, `get_config`, `get_tiles`, `get_preview`, `index` and `RevalidatingStaticFiles`: the HTTP API, the page and the cache headers.
- `src/geosamroad_app/grid.py`, `tiles_for_viewport`, `tile_bounds`, `parse_tile_id`, `PIXEL_DEG` and `TILE_DEG`: the global EPSG:4326 cell grid.
- `src/geosamroad_app/jobs.py`, `Job` and `JobRegistry`: the in-memory job store and the single worker.
- `src/geosamroad_app/pipeline.py`, `run`, `fetch_tile`, `composite_window` and `_release`: one job from fetch to result.
- `src/geosamroad_app/runner.py`, `load_model`, `infer_tile`, `_pixels_to_wgs84` and `_save_preview_wgs84`: model loading, inference on one cell, coordinates and preview.
- `src/geosamroad_app/variants.py`, `VARIANTS`, `get_variant` and `available`: the encoder variants and their checkpoints.
- `src/geosamroad_app/static/app.js`, `refreshGrid`, `toggleTile`, `startJob`, `pollJob`, `addLayer`, `saveLayer` and `importFile`: the map, the selection, polling and result layers.
- `src/geosamroad_app/static/index.html` and `style.css`: the side panel and the legend.
- `src/globalurbanmapper_gee/api.py`, `fetch_region`, and `auth.py`, `initialize`: the Earth Engine fetch and login, covered in [imagery fetch](imagery-fetch.md).
- `deploy/Dockerfile`, `deploy/docker-compose.yml` and `deploy/.env.example`: the image, the Traefik and Cloudflare wiring, and the settings.
