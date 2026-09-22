/* GeoSAMRoad region selector.
 *
 * The grid drawn here is the fetcher's own tiling (one cell == one 512 px
 * tile), served by /api/tiles for the current viewport. Hover and selection use
 * MapLibre feature-state so highlighting never re-renders the source. */

const $ = (id) => document.getElementById(id);
const api = (path, opts) =>
  fetch(path, opts).then(async (r) => {
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  });

const state = {
  config: null,
  selected: new Set(),
  hovered: null,
  jobId: null,
  poll: null,
  previews: [],
};

/* Basemap. The default is a Sentinel-2 cloudless mosaic rather than high-res
 * aerial imagery, because that is the sensor the model actually consumes: what
 * you judge a tile on should be roughly what the model will be given. The
 * mosaic year follows the selected date, so moving the date moves the imagery.
 * Esri is kept as an optional high-res reference for checking detections. */
const S2_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025];

const s2TilesFor = (dateStr) => {
  const wanted = parseInt((dateStr || "").slice(0, 4), 10) || 2020;
  const year = Math.min(Math.max(wanted, S2_YEARS[0]), S2_YEARS[S2_YEARS.length - 1]);
  return [`https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-${year}_3857/default/g/{z}/{y}/{x}.jpg`];
};

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      s2: {
        type: "raster",
        tiles: s2TilesFor(document.getElementById("date").value),
        tileSize: 256,
        maxzoom: 14, // the mosaic has no detail beyond this; MapLibre overzooms
        attribution:
          '<a href="https://s2maps.eu">Sentinel-2 cloudless</a> by EOX ' +
          "(contains modified Copernicus Sentinel data)",
      },
      hires: {
        type: "raster",
        tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
        tileSize: 256,
        attribution: "Imagery &copy; Esri",
      },
    },
    layers: [
      { id: "basemap-s2", type: "raster", source: "s2" },
      {
        id: "basemap-hires", type: "raster", source: "hires",
        layout: { visibility: "none" },
      },
    ],
  },
  center: [18.45, -33.93], // Cape Town
  zoom: 11,
  maxZoom: 17,
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

/* ---------------------------------------------------------------- layers */
map.on("load", async () => {
  map.addSource("tiles", {
    type: "geojson",
    data: { type: "FeatureCollection", features: [] },
    promoteId: "tile_id", // string ids -> usable with feature-state
  });

  map.addLayer({
    id: "tiles-fill",
    type: "fill",
    source: "tiles",
    paint: {
      "fill-color": [
        "case",
        ["boolean", ["feature-state", "selected"], false], "#4dd4ac",
        ["boolean", ["feature-state", "hover"], false], "#ffffff",
        "#ffffff",
      ],
      "fill-opacity": [
        "case",
        ["boolean", ["feature-state", "selected"], false], 0.22,
        ["boolean", ["feature-state", "hover"], false], 0.14,
        0.03,
      ],
    },
  });

  map.addLayer({
    id: "tiles-line",
    type: "line",
    source: "tiles",
    paint: {
      "line-color": [
        "case",
        ["boolean", ["feature-state", "selected"], false], "#4dd4ac",
        "rgba(255,255,255,0.45)",
      ],
      "line-width": [
        "case",
        ["boolean", ["feature-state", "selected"], false], 2,
        ["boolean", ["feature-state", "hover"], false], 1.5,
        0.5,
      ],
    },
  });

  map.addSource("roads", { type: "geojson", data: emptyFC() });
  // Two passes: a dark casing under a bright core keeps roads legible over
  // both bright sand and dark water.
  map.addLayer({
    id: "roads-casing", type: "line", source: "roads",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#000000", "line-opacity": 0.5, "line-width": 4.5 },
  });
  map.addLayer({
    id: "roads-core", type: "line", source: "roads",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#ffd34d", "line-width": 2 },
  });

  wireInteractions();
  await loadConfig();
  await refreshGrid();
});

const emptyFC = () => ({ type: "FeatureCollection", features: [] });

/* ------------------------------------------------------------ grid load */
let gridTimer = null;
map.on("moveend", () => {
  clearTimeout(gridTimer);
  gridTimer = setTimeout(refreshGrid, 200); // debounce panning
});

async function refreshGrid() {
  const b = map.getBounds();
  try {
    const fc = await api(
      `/api/tiles?west=${b.getWest()}&south=${b.getSouth()}&east=${b.getEast()}&north=${b.getNorth()}`
    );
    if (fc.truncated) {
      map.getSource("tiles").setData(emptyFC());
      $("grid-status").textContent =
        `Zoom in to pick tiles — this view spans ${fc.tile_count.toLocaleString()} of them`;
      return;
    }
    map.getSource("tiles").setData(fc);
    $("grid-status").textContent =
      `${fc.tile_count} tiles in view · ${state.config?.tile_km ?? 5.12} km each · UTM ${fc.epsg}`;
    // Re-assert selection: setData clears feature-state.
    state.selected.forEach((id) =>
      map.setFeatureState({ source: "tiles", id }, { selected: true })
    );
  } catch (err) {
    $("grid-status").textContent = `Grid unavailable: ${err.message}`;
  }
}

/* ---------------------------------------------------------- interactions */
function wireInteractions() {
  map.on("mousemove", "tiles-fill", (e) => {
    if (!e.features.length) return;
    const id = e.features[0].properties.tile_id;
    if (state.hovered === id) return;
    if (state.hovered) map.setFeatureState({ source: "tiles", id: state.hovered }, { hover: false });
    state.hovered = id;
    map.setFeatureState({ source: "tiles", id }, { hover: true });
    map.getCanvas().style.cursor = "pointer";
  });

  map.on("mouseleave", "tiles-fill", () => {
    if (state.hovered) map.setFeatureState({ source: "tiles", id: state.hovered }, { hover: false });
    state.hovered = null;
    map.getCanvas().style.cursor = "";
  });

  map.on("click", "tiles-fill", (e) => {
    if (!e.features.length) return;
    toggleTile(e.features[0].properties.tile_id);
  });

  $("clear").addEventListener("click", () => {
    state.selected.forEach((id) =>
      map.setFeatureState({ source: "tiles", id }, { selected: false })
    );
    state.selected.clear();
    renderSelection();
  });

  $("run").addEventListener("click", startJob);
  $("variant").addEventListener("change", renderVariantHint);

  // Date drives the composite AND the mosaic year, so the two stay in step.
  $("date").addEventListener("change", (e) => {
    map.getSource("s2").setTiles(s2TilesFor(e.target.value));
    renderBasemapHint();
  });

  $("basemap").addEventListener("change", (e) => {
    const s2 = e.target.value === "s2";
    map.setLayoutProperty("basemap-s2", "visibility", s2 ? "visible" : "none");
    map.setLayoutProperty("basemap-hires", "visibility", s2 ? "none" : "visible");
    renderBasemapHint();
  });
  $("show-roads").addEventListener("change", (e) => {
    const v = e.target.checked ? "visible" : "none";
    ["roads-core", "roads-casing"].forEach((l) => map.setLayoutProperty(l, "visibility", v));
  });
  $("show-imagery").addEventListener("change", (e) => {
    state.previews.forEach((p) =>
      map.setLayoutProperty(`preview-${p.tile_id}`, "visibility", e.target.checked ? "visible" : "none")
    );
  });
}

function toggleTile(id) {
  if (state.selected.has(id)) {
    state.selected.delete(id);
    map.setFeatureState({ source: "tiles", id }, { selected: false });
  } else {
    if (state.selected.size >= (state.config?.max_tiles_per_job ?? 24)) {
      showError(`At most ${state.config.max_tiles_per_job} tiles per run.`);
      return;
    }
    state.selected.add(id);
    map.setFeatureState({ source: "tiles", id }, { selected: true });
  }
  renderSelection();
}

function renderSelection() {
  const n = state.selected.size;
  const km = state.config?.tile_km ?? 5.12;
  $("sel-count").textContent = n;
  $("sel-area").textContent = n ? `${(n * km * km).toFixed(0)} km²` : "—";
  $("clear").hidden = n === 0;
  const run = $("run");
  run.disabled = n === 0 || !state.config?.checkpoints_found;
  run.textContent = !state.config?.checkpoints_found
    ? "No checkpoints mounted"
    : n === 0
    ? "Select tiles on the map"
    : `Detect roads in ${n} tile${n > 1 ? "s" : ""}`;
}

/* --------------------------------------------------------------- config */
async function loadConfig() {
  state.config = await api("/api/config");
  const sel = $("variant");
  sel.innerHTML = "";
  for (const v of state.config.variants) {
    const opt = document.createElement("option");
    opt.value = v.name;
    opt.textContent = v.label;
    sel.appendChild(opt);
  }
  if (!state.config.variants.length) {
    sel.innerHTML = '<option>No checkpoints found</option>';
    sel.disabled = true;
  }
  sel.value = state.config.default_variant;
  renderVariantHint();
  renderBasemapHint();
  renderSelection();
}

function renderBasemapHint() {
  const s2 = $("basemap").value === "s2";
  const year = s2TilesFor($("date").value)[0].match(/s2cloudless-(\d{4})/)[1];
  $("basemap-hint").textContent = s2
    ? `Sentinel-2 cloudless ${year} — the sensor the model reads`
    : "High-res aerial — for checking detections, not what the model sees";
}

function renderVariantHint() {
  const v = state.config?.variants.find((x) => x.name === $("variant").value);
  $("variant-hint").textContent = v
    ? `road IoU ${v.road_iou.toFixed(3)} · needs ${v.band_list === "rgb" ? "RGB" : "S2 + S1"} bands`
    : "";
}

/* ----------------------------------------------------------------- jobs */
async function startJob() {
  hideError();
  $("summary").hidden = true;
  $("run").disabled = true;
  $("progress").hidden = false;
  setProgress(0, 1, "Queued");

  try {
    const job = await api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tile_ids: [...state.selected],
        variant: $("variant").value,
        date: $("date").value,
      }),
    });
    state.jobId = job.job_id;
    state.poll = setInterval(pollJob, 1200);
  } catch (err) {
    showError(err.message);
    $("progress").hidden = true;
    renderSelection();
  }
}

async function pollJob() {
  let job;
  try {
    job = await api(`/api/jobs/${state.jobId}`);
  } catch (err) {
    return; // transient; keep polling
  }
  setProgress(job.done, job.total, job.message);

  if (job.status === "completed") {
    clearInterval(state.poll);
    $("progress").hidden = true;
    showResult(job.result);
    renderSelection();
  } else if (job.status === "failed") {
    clearInterval(state.poll);
    $("progress").hidden = true;
    showError(job.error || "Job failed");
    renderSelection();
  }
}

function setProgress(done, total, message) {
  $("progress-fill").style.width = `${total ? (done / total) * 100 : 0}%`;
  $("progress-msg").textContent = message || "";
}

function showResult(result) {
  map.getSource("roads").setData(result.roads);
  addPreviews(result.previews);

  const failed = result.tiles_failed || [];
  $("summary").hidden = false;
  $("summary").innerHTML = `
    <dl>
      <dt>Road segments</dt><dd>${result.total_edges.toLocaleString()}</dd>
      <dt>Intersections</dt><dd>${result.total_nodes.toLocaleString()}</dd>
      <dt>Tiles</dt><dd>${result.tiles_processed}${failed.length ? ` (${failed.length} failed)` : ""}</dd>
      <dt>Ran on</dt><dd>${result.device || "—"}</dd>
    </dl>`;
  $("toggles").hidden = false;
  if (failed.length) showError(`${failed.length} tile(s) failed: ${failed[0].error}`);
}

function addPreviews(previews) {
  // Drop previous overlays before adding the new run's.
  state.previews.forEach((p) => {
    const id = `preview-${p.tile_id}`;
    if (map.getLayer(id)) map.removeLayer(id);
    if (map.getSource(id)) map.removeSource(id);
  });
  state.previews = previews || [];

  for (const p of state.previews) {
    const id = `preview-${p.tile_id}`;
    map.addSource(id, {
      type: "image",
      url: `/api/previews/${state.jobId}/${p.tile_id}.png`,
      coordinates: p.corners, // UTM square -> 4 lon/lat corners, not a bbox
    });
    map.addLayer({ id, type: "raster", source: id, paint: { "raster-opacity": 0.85 } },
                  "roads-casing");
  }
  $("show-imagery").checked = true;
}

function showError(message) {
  $("error").hidden = false;
  $("error").textContent = message;
}
function hideError() { $("error").hidden = true; }
