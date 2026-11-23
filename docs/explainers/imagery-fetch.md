# Imagery fetch

How `globalurbanmapper_gee` builds a Sentinel-1 and Sentinel-2 composite in Google Earth Engine and downloads it as ROSA-layout GeoTIFFs. The web app calls it for every tile a user selects.

## Core idea

Collect every Sentinel-2 scene over the area in a date window, mask the clouds and their shadows, and take the per-pixel median. Average the Sentinel-1 radar scenes per orbit direction. Stack the bands, download them in 512 by 512 px blocks, and append the same enhanced RGB bands the dataset tiles carry. The result has the 21-band tile layout, so a model trained on ROSA can read it unchanged. The package is a Python port of the GlobalUrbanMapper Earth Engine JavaScript. It does not fetch the elevation and urban bands, and writes zeros in their place.

## How it works

1. **Log in.** `auth.initialize` connects to Earth Engine's high-volume endpoint. It uses a service account key when `GEE_SERVICE_ACCOUNT` and `GEE_PRIVATE_KEY_FILE` are set, and default credentials otherwise. A key file alone is enough, because the account and project are read from the key.
2. **Define the area.** A centre point with a width and height in km becomes a rectangle in the local UTM zone. A west, south, east, north box becomes a rectangle in EPSG:4326.
3. **Pick the bands.** The band list decides which source bands to fetch. `rgb` fetches B4, B3 and B2. `s1s2` fetches all ten Sentinel-2 bands and VV and VH ascending. Either way the RGB bands are fetched, because the enhanced RGB is computed from them.
4. **Build the Sentinel-2 composite.** `imagery.gen_sentinel2_data` keeps scenes in the window with a scene cloud cover of 90% or less. It masks clouds and shadows in each scene. `stack.build_rosa_image` takes the median and divides by 10,000 to get reflectance.
5. **Build the Sentinel-1 composite.** `imagery.gen_sentinel1_data` keeps scenes with both VV and VH. It prefers IW mode at 10 m and falls back to EW mode at 40 m, resampled to 10 m. The ascending and descending passes get a separate mean each. The descending pair is then selected away.
6. **Choose the CRS.** By default the port samples in the CRS of the first Sentinel-2 scene, which is a UTM zone. A caller can force a CRS.
7. **Download in blocks.** `fetch.region_grid` snaps the area to the pixel size and cuts it into 512 px blocks from the top-left. `fetch._compute_pixels` sends one `computePixels` request per block. A partial block at the right or bottom edge is fetched at its smaller size.
8. **Fill the layout.** `fetch.fetch_tiles` puts the fetched bands into an 18-band array of zeros at their ROSA positions. `enhance.append_enhanced_rgb` appends the three enhanced RGB bands. This gives the 21-band layout.
9. **Write the output.** `api.fetch_region` has three output modes:
   - `out_path` writes one COG over the whole area, with overviews at 2, 4, 8 and 16.
   - `out_dir` writes one COG per full 512 px block into `imagery/`, plus `metadata.csv`. It skips partial blocks.
   - With neither, it returns a `(21, H, W)` float32 array.

## Details that matter

- **Cloud and shadow mask.** A pixel is cloud when its s2cloudless probability is above 50. A pixel is dark when its B8 reflectance is below 0.15 and its SCL class is not water. A dark pixel is shadow when it lies on the cloud's projection away from the sun. The projection runs 10 pixels at a 100 m scale, which is 1 km. The union of cloud and shadow is eroded by 2 px, then dilated by 5 px at 20 m, which is the 50 m `BUFFER`.
- **Empty collections become zeros.** When no Sentinel-2 scene passes the filters, the composite is a constant 0 image. The same holds for each Sentinel-1 pass with no scenes. Nothing in the output tells a reader that the bands are fake.
- **Missing pixels become zeros.** `_compute_pixels` replaces NaN and infinity with 0. A pixel masked as cloud in every scene comes out as 0 reflectance. The generator's `dropped_nan` rule could never catch these pixels.
- **Bands 13 to 18 are always zero.** Elevation, slope, aspect and the three urban layers keep their slots in the layout but hold 0. With `rgb`, every band except RGB and enhanced RGB is also 0. `bands.source_bands` raises an error if a band list ever asks for a band above 12. No band list asks for one today.
- **Sentinel-1 is averaged in decibels.** The port takes the mean of the Earth Engine GRD values with no conversion to linear power first.
- **The enhanced RGB is stretched per 512 px block.** It uses `enhance_rgb` from the generator, with gamma 0.4 and CLAHE with a 32 px kernel and a 0.01 clip limit. This matches the per-tile stretch in [dataset tiling](dataset-tiling.md). In `out_path` mode the stretch restarts at every block, so the zone COG has seams in bands 19 to 21. A partial edge block is stretched over its own few pixels.
- **Retries.** A failed `computePixels` call is retried 5 times, with waits of 2, 4, 8, 16 and 32 s. The loop also sleeps after the last failure, so a dead request costs 62 s before it raises.
- **Grid snapping in degrees.** The web app fetches each cell in EPSG:4326 at 9e-5 degrees per pixel. Floating-point snapping in `region_grid` gives 513 or 514 px instead of 512 for about 37% of app cells. This figure comes from a local run over 10,000 South African cells. The port then fetches one to three extra sliver blocks per cell, and `write_tiles` throws them away.
- **Tile names.** `write_tiles` names files `region_r<row>_c<col>`. `api.fetch_region` cannot change the `region` prefix.
- **Dates.** `api.fetch_region` defaults to the 2020 calendar year. The web app passes a window of 182 days on each side of the date the user picks.

## Why it's like this

- **The 21-band layout with zero fill.** Commit 3695d4e for KEL-8 states that "the GEE port writes the same 21-band layout" as the generator. Band indices stay the same, so every encoder reads the same physical bands at training and at inference. No band list reads bands 13 to 18, so the port does not fetch them.
- **Only the bands the model reads.** The port fetches only the source bands of the chosen band list. `tests/globalurbanmapper_gee/test_bands.py` checks this.
- **The composite recipe.** The median for Sentinel-2, the per-pass mean for Sentinel-1, IW before EW and the 90% scene filter are ported from GlobalUrbanMapper. No record says how closely they follow the JavaScript, or why those values were chosen. Reason unknown, see KEL-26.
- **NaN written as 0.** Reason unknown, see KEL-27.
- **Zone imagery comes from somewhere else.** The 60 zone COGs that `generate` reads have 20 bands, including elevation, slope, aspect and the urban layers. Their band names follow the GlobalUrbanMapper layout, for example `B4` and `gisa_urban_30m`. `generator/processor.py` `zone_band_names` rejects any zone that does not have exactly 20 bands. The port writes 21 bands and cannot fetch elevation or urban layers, so it did not make the zone imagery. Which script made the zone COGs, and with which dates and settings, is not recorded. Reason unknown, see KEL-28.

## Weak spots and ideas

- **Training and inference imagery can differ.** Observed. The zone COGs come from a different script than the app's imagery. Nobody has checked that the port gives the same pixels as the zone COGs over the same area and dates. A fetch over one zone, compared band by band, would settle it.
- **The app imagery is compressed east to west.** Observed. The app samples in degrees, so a pixel is 10 m north to south and about 8.4 m east to west at 33 degrees south. The model was trained on square 10 m UTM pixels. `geosamroad_app/grid.py` says this trade is deliberate.
- **Silent zeros.** Observed. Empty collections, all-cloud pixels and the unfetched bands all come out as 0. A NaN or a nodata value would let the caller tell real data from fill.
- **Seams in the zone COG.** Observed. The per-block stretch makes `out_path` output unsuitable as zone imagery for `generate`. It would fail the band count check anyway.
- **Wasted requests in the app.** Observed. The degree grid fetches sliver blocks that are thrown away. The snapped origin may also sit one pixel off the cell edge, which is only suspected. Snapping to the cell bounds, instead of to `floor(min_x / scale)`, would fix both.
- **Cross-package coupling.** Observed. `enhance.py` imports `RGBEnhanceConfig` and `enhance_rgb` from `sentinel2data.generator`, and `bands.py` builds on `geosamroad.dataset.bands`. The port works only when installed with both packages. Any new import added to `generator/helper.py` or `generator/config.py` can break the app's fetch. The shared enhancement would be cleaner in a small module that both packages import.
- **API shape.** Observed. `api.fetch_region` checks that `out_path` and `out_dir` are not both set only after it has called Earth Engine. Three output modes behind one function, each returning a different type, make the call sites hard to read. `fetch.fetch_region` and `api.fetch_region` share a name but do different things.
- **Stale comments.** Observed. `CLD_PRJ_DIST` says "in units of 10 m", but the projection runs at 100 m, so 1 means 1 km. `geosamroad_app/runner.py` says Earth Engine samples in the granule's UTM zone, but the app forces EPSG:4326.
- **CRS from the first scene.** Suspected. An area that straddles two UTM zones gets the zone of whichever scene comes first. With no Sentinel-2 scenes, the fallback CRS is the bounding box's, which may be EPSG:4326.

## Code map

- `src/globalurbanmapper_gee/api.py`, `build_region_image` and `fetch_region`: the entry points, which pick the area, build the image and choose the output mode.
- `src/globalurbanmapper_gee/imagery.py`, `gen_sentinel2_data` and `gen_sentinel1_data`: the Earth Engine collections and the cloud and shadow mask.
- `src/globalurbanmapper_gee/stack.py`, `build_rosa_image` and `resolve_crs`: composite, scale and stack the bands.
- `src/globalurbanmapper_gee/bands.py`, `BAND_LIST`, `source_bands` and `written_band_names`: the band lists and the 21-band layout.
- `src/globalurbanmapper_gee/fetch.py`, `region_grid`, `_compute_pixels` and `fetch_tiles`: the block grid, the download with retries and the zero fill.
- `src/globalurbanmapper_gee/enhance.py`, `append_enhanced_rgb`: append the enhanced RGB bands.
- `src/globalurbanmapper_gee/io.py`, `write_zone_cog` and `write_tiles`: write the COGs and `metadata.csv`.
- `src/globalurbanmapper_gee/geometry.py`, `create_rectangular_bbox` and `planar_bounds`: area rectangles and their bounds in the sampling CRS.
- `src/globalurbanmapper_gee/auth.py`, `initialize`: Earth Engine login, run once per process.
- `src/geosamroad_app/pipeline.py`, `fetch_tile`: the only caller of `api.fetch_region`. See [web app](web-app.md) and [inference](inference.md).
- `tests/globalurbanmapper_gee/test_bands.py`: band layout and band list tests. Nothing tests the Earth Engine code.
