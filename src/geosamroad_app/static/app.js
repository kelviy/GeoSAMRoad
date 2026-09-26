/* GeoSAMRoad region selector.
 *
 * The grid drawn here is the fetcher's own tiling (one cell == one 512 px tile
 * in EPSG:4326), served by /api/tiles for the current viewport. Hover and
 * selection use MapLibre feature-state so highlighting never re-renders.
 *
 * Every result -- whether just computed or loaded from disk -- becomes a
 * "layer" with its own sources, so several runs can be compared side by side
 * and saved independently. */

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
  layers: [],
  seq: 0,
};

/* Basemap. Default is a Sentinel-2 cloudless mosaic rather than high-res
 * aerial, because that is the sensor the model consumes: what you judge a tile
 * on should be what the model is given. The mosaic year follows the date. */
const S2_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025];
const s2TilesFor = (dateStr) => {
  const wanted = parseInt((dateStr || "").slice(0, 4), 10) || 2020;
  const year = Math.min(Math.max(wanted, S2_YEARS[0]), S2_YEARS.at(-1));
  return [`https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-${year}_3857/default/g/{z}/{y}/{x}.jpg`];
};

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      s2: {
        type: "raster",
        tiles: s2TilesFor($("date").value),
        tileSize: 256,
        maxzoom: 14,
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
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        maxzoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      },
    },
    layers: [
      { id: "basemap-s2", type: "raster", source: "s2" },
      { id: "basemap-hires", type: "raster", source: "hires", layout: { visibility: "none" } },
      { id: "basemap-osm", type: "raster", source: "osm", layout: { visibility: "none" } },
    ],
  },
  center: [18.45, -33.93],
  zoom: 11,
  maxZoom: 17,
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

const emptyFC = () => ({ type: "FeatureCollection", features: [] });

// Basemap options, keyed by the <select> value. Exactly one is visible.
const BASEMAPS = {
  s2: {
    layer: "basemap-s2",
    hint: (year) => `Sentinel-2 cloudless ${year} — the sensor the model reads`,
  },
  hires: {
    layer: "basemap-hires",
    hint: () => "High-res aerial — for checking detections, not what the model sees",
  },
  osm: {
    layer: "basemap-osm",
    hint: () => "OpenStreetMap — street names and existing roads, for orientation",
  },
};

/* ---------------------------------------------------------------- layers */
map.on("load", async () => {
  map.addSource("tiles", {
    type: "geojson",
    data: emptyFC(),
    promoteId: "tile_id",
  });

  map.addLayer({
    id: "tiles-fill",
    type: "fill",
    source: "tiles",
    paint: {
      "fill-color": [
        "case",
        ["boolean", ["feature-state", "selected"], false], "#4dd4ac",
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

  wireInteractions();
  await loadConfig();
  await refreshGrid();
});

/* ------------------------------------------------------------ grid load */
let gridTimer = null;
map.on("moveend", () => {
  clearTimeout(gridTimer);
  gridTimer = setTimeout(refreshGrid, 200);
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
      `${fc.tile_count} tiles in view · ${state.config?.tile_km ?? 5.1} km each · EPSG:${fc.epsg}`;
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
    if (e.features.length) toggleTile(e.features[0].properties.tile_id);
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

  $("date").addEventListener("change", (e) => {
    map.getSource("s2").setTiles(s2TilesFor(e.target.value));
    renderBasemapHint();
  });

  $("basemap").addEventListener("change", (e) => {
    for (const [key, cfg] of Object.entries(BASEMAPS)) {
      map.setLayoutProperty(
        cfg.layer, "visibility", key === e.target.value ? "visible" : "none"
      );
    }
    renderBasemapHint();
  });

  $("panel-toggle").addEventListener("click", togglePanel);
  $("goto-btn").addEventListener("click", goToLocation);
  $("goto").addEventListener("keydown", (e) => {
    if (e.key === "Enter") goToLocation();
  });

  $("import-btn").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", importFile);
}

/* -------------------------------------------------------- panel collapse */
function togglePanel() {
  const collapsed = document.body.classList.toggle("panel-collapsed");
  const btn = $("panel-toggle");
  btn.setAttribute("aria-expanded", String(!collapsed));
  btn.title = collapsed ? "Show panel" : "Hide panel";
  // The map's canvas size changed; let MapLibre re-measure once the CSS
  // transition has finished or the centre drifts.
  setTimeout(() => map.resize(), 260);
}

/* ------------------------------------------------------- go to location */
async function goToLocation() {
  const query = $("goto").value.trim();
  if (!query) return;
  const hint = $("goto-hint");

  // "lat, lon" (or "lat lon") is handled locally -- no network round trip, and
  // it is the form the tile ids and bounds are already in.
  const m = query.match(/^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/);
  if (m) {
    const lat = parseFloat(m[1]);
    const lon = parseFloat(m[2]);
    if (Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      hint.textContent = "Latitude must be ±90 and longitude ±180.";
      return;
    }
    map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 12) });
    hint.textContent = `Moved to ${lat.toFixed(4)}, ${lon.toFixed(4)}`;
    return;
  }

  hint.textContent = "Searching…";
  try {
    const url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" +
      encodeURIComponent(query);
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(res.statusText);
    const hits = await res.json();
    if (!hits.length) {
      hint.textContent = `No match for “${query}”.`;
      return;
    }
    const hit = hits[0];
    if (hit.boundingbox) {
      const [s, n, w, e] = hit.boundingbox.map(Number);
      map.fitBounds([[w, s], [e, n]], { padding: 60, maxZoom: 14 });
    } else {
      map.flyTo({ center: [+hit.lon, +hit.lat], zoom: 12 });
    }
    hint.textContent = hit.display_name.split(",").slice(0, 3).join(",");
  } catch (err) {
    hint.textContent = `Lookup failed (${err.message}). Try “lat, lon”.`;
  }
}

/* ------------------------------------------------------------- selection */
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
  const km = state.config?.tile_km ?? 5.1;
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
    sel.innerHTML = "<option>No checkpoints found</option>";
    sel.disabled = true;
  }
  sel.value = state.config.default_variant;
  renderVariantHint();
  renderBasemapHint();
  renderSelection();
}

function renderBasemapHint() {
  const year = s2TilesFor($("date").value)[0].match(/s2cloudless-(\d{4})/)[1];
  $("basemap-hint").textContent = BASEMAPS[$("basemap").value]?.hint(year) ?? "";
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
  } catch {
    return; // transient; keep polling
  }
  setProgress(job.done, job.total, job.message);

  if (job.status === "completed") {
    clearInterval(state.poll);
    $("progress").hidden = true;
    const r = job.result;
    // Rewrite preview urls to absolute API paths so a layer keeps working
    // after the job id is no longer the "current" one.
    const previews = (r.previews || []).map((p) => ({
      ...p,
      url: `/api/previews/${state.jobId}/${p.tile_id}.png`,
    }));
    addLayer({ ...r, previews }, layerName(r));
    if ((r.tiles_failed || []).length) {
      showError(`${r.tiles_failed.length} tile(s) failed: ${r.tiles_failed[0].error}`);
    }
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

const layerName = (r) => {
  const label = state.config?.variants.find((v) => v.name === r.variant)?.label || r.variant;
  const n = r.tiles_processed ?? (r.previews || []).length;
  return `${label} · ${n} tile${n === 1 ? "" : "s"}`;
};

/* --------------------------------------------------------- result layers */
function addLayer(result, name) {
  const id = `L${++state.seq}`;
  const layer = {
    id,
    name,
    visible: true,
    showRoads: true,
    showImagery: true,
    roads: result.roads || emptyFC(),
    previews: result.previews || [],
    meta: {
      variant: result.variant,
      date: result.composite?.start ? `${result.composite.start} → ${result.composite.end}` : "",
      nodes: result.total_nodes ?? 0,
      edges: result.total_edges ?? 0,
      tiles: (result.previews || []).map((p) => p.tile_id),
    },
  };

  // Imagery first so roads always draw above it.
  layer.previews.forEach((p, i) => {
    const sid = `${id}-img-${i}`;
    map.addSource(sid, { type: "image", url: p.url, coordinates: p.corners });
    map.addLayer({ id: sid, type: "raster", source: sid, paint: { "raster-opacity": 1 } });
  });

  map.addSource(`${id}-roads`, { type: "geojson", data: layer.roads });
  map.addLayer({
    id: `${id}-roads-casing`, type: "line", source: `${id}-roads`,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#000000", "line-opacity": 0.5, "line-width": 4.5 },
  });
  map.addLayer({
    id: `${id}-roads-core`, type: "line", source: `${id}-roads`,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": "#ffd34d", "line-width": 2 },
  });

  state.layers.unshift(layer);
  renderLayers();
  zoomToLayer(id);
  return layer;
}

const layerMapIds = (l) =>
  [`${l.id}-roads-core`, `${l.id}-roads-casing`, ...l.previews.map((_, i) => `${l.id}-img-${i}`)];

function removeLayer(id) {
  const i = state.layers.findIndex((l) => l.id === id);
  if (i < 0) return;
  const l = state.layers[i];
  layerMapIds(l).forEach((mid) => {
    if (map.getLayer(mid)) map.removeLayer(mid);
  });
  [`${l.id}-roads`, ...l.previews.map((_, k) => `${l.id}-img-${k}`)].forEach((sid) => {
    if (map.getSource(sid)) map.removeSource(sid);
  });
  state.layers.splice(i, 1);
  renderLayers();
}

function applyVisibility(l) {
  // The layer checkbox is the master switch; roads and imagery are independent
  // beneath it, so imagery can be inspected with the detections hidden.
  const roadsOn = l.visible && l.showRoads ? "visible" : "none";
  const imgOn = l.visible && l.showImagery ? "visible" : "none";
  [`${l.id}-roads-core`, `${l.id}-roads-casing`].forEach((mid) => {
    if (map.getLayer(mid)) map.setLayoutProperty(mid, "visibility", roadsOn);
  });
  l.previews.forEach((_, i) => {
    const mid = `${l.id}-img-${i}`;
    if (map.getLayer(mid)) map.setLayoutProperty(mid, "visibility", imgOn);
  });
}

function layerBounds(l) {
  const b = new maplibregl.LngLatBounds();
  let any = false;
  l.previews.forEach((p) => {
    (p.corners || []).forEach((c) => { b.extend(c); any = true; });
  });
  l.roads.features.forEach((f) => {
    f.geometry.coordinates.forEach((c) => { b.extend(c); any = true; });
  });
  return any ? b : null;
}

function zoomToLayer(id) {
  const l = state.layers.find((x) => x.id === id);
  const b = l && layerBounds(l);
  if (b) map.fitBounds(b, { padding: 60, maxZoom: 15 });
}

function renderLayers() {
  const list = $("layer-list");
  list.querySelectorAll(".layer").forEach((n) => n.remove());
  $("layer-empty").hidden = state.layers.length > 0;
  $("save-imagery-wrap").hidden = state.layers.length === 0;

  for (const l of state.layers) {
    const el = document.createElement("div");
    el.className = "layer";
    el.innerHTML = `
      <div class="layer-top">
        <label class="check tight">
          <input type="checkbox" data-act="vis" ${l.visible ? "checked" : ""} />
          <span class="layer-name" title="${escapeHtml(l.name)}">${escapeHtml(l.name)}</span>
        </label>
        <div class="layer-btns">
          <button class="icon" data-act="zoom" title="Zoom to">⤢</button>
          <button class="icon" data-act="save" title="Save to file">⭳</button>
          <button class="icon danger" data-act="del" title="Remove">✕</button>
        </div>
      </div>
      <div class="layer-meta">
        ${l.meta.edges.toLocaleString()} segments · ${l.meta.nodes.toLocaleString()} nodes
      </div>
      <div class="layer-toggles">
        <label class="check tight inline">
          <input type="checkbox" data-act="roads" ${l.showRoads ? "checked" : ""} /> roads
        </label>
        ${l.previews.length ? `<label class="check tight inline">
          <input type="checkbox" data-act="img" ${l.showImagery ? "checked" : ""} /> imagery
        </label>` : ""}
      </div>`;

    el.querySelector('[data-act="vis"]').addEventListener("change", (e) => {
      l.visible = e.target.checked;
      applyVisibility(l);
    });
    el.querySelector('[data-act="roads"]').addEventListener("change", (e) => {
      l.showRoads = e.target.checked;
      applyVisibility(l);
    });
    const img = el.querySelector('[data-act="img"]');
    if (img) img.addEventListener("change", (e) => {
      l.showImagery = e.target.checked;
      applyVisibility(l);
    });
    el.querySelector('[data-act="zoom"]').addEventListener("click", () => zoomToLayer(l.id));
    el.querySelector('[data-act="save"]')
      .addEventListener("click", (ev) => saveLayer(l, ev.currentTarget));
    el.querySelector('[data-act="del"]').addEventListener("click", () => removeLayer(l.id));
    list.appendChild(el);
  }
}

const escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------- save (download) */
async function saveLayer(layer, btn) {
  const withImagery = $("save-imagery").checked;
  if (btn) btn.textContent = "…";

  try {
    const previews = [];
    for (const p of layer.previews) {
      const entry = { tile_id: p.tile_id, bounds: p.bounds, corners: p.corners };
      if (withImagery) entry.image = await toDataUrl(p.url);
      previews.push(entry);
    }

    // A valid GeoJSON FeatureCollection -- so it opens directly in QGIS -- with
    // our extras under one namespaced member that other readers ignore.
    const bundle = {
      type: "FeatureCollection",
      features: layer.roads.features,
      geosamroad: {
        version: 1,
        saved_at: new Date().toISOString(),
        name: layer.name,
        ...layer.meta,
        previews,
      },
    };
    download(
      new Blob([JSON.stringify(bundle)], { type: "application/geo+json" }),
      `${layer.name.replace(/[^\w.-]+/g, "_")}.geojson`
    );
  } catch (err) {
    showError(`Save failed: ${err.message}`);
  } finally {
    if (btn) btn.textContent = "⭳";
  }
}

function toDataUrl(url) {
  return fetch(url)
    .then((r) => {
      if (!r.ok) throw new Error(`preview ${r.status}`);
      return r.blob();
    })
    .then((blob) => new Promise((res, rej) => {
      const fr = new FileReader();
      fr.onload = () => res(fr.result);
      fr.onerror = () => rej(new Error("could not read preview"));
      fr.readAsDataURL(blob);
    }));
}

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* --------------------------------------------------------- load (upload) */
async function importFile(e) {
  const file = e.target.files?.[0];
  e.target.value = "";           // let the same file be picked again
  if (!file) return;
  hideError();

  try {
    const data = JSON.parse(await file.text());
    if (data.type !== "FeatureCollection" || !Array.isArray(data.features)) {
      throw new Error("not a GeoJSON FeatureCollection");
    }
    const meta = data.geosamroad || {};
    const previews = (meta.previews || [])
      .filter((p) => p.image && p.corners)     // imagery-less saves still load
      .map((p) => ({ ...p, url: p.image }));   // data: URI works as an image source

    addLayer(
      {
        roads: { type: "FeatureCollection", features: data.features },
        previews,
        variant: meta.variant,
        total_nodes: meta.nodes ?? 0,
        total_edges: meta.edges ?? data.features.length,
        tiles_processed: meta.tiles?.length,
      },
      meta.name || file.name.replace(/\.(geo)?json$/i, "")
    );
  } catch (err) {
    showError(`Could not load ${file.name}: ${err.message}`);
  }
}

/* ---------------------------------------------------------------- errors */
function showError(message) {
  $("error").hidden = false;
  $("error").textContent = message;
}
function hideError() { $("error").hidden = true; }
