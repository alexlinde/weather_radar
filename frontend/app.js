/**
 * Weather Radar — Main application with tile-based radar rendering.
 *
 * Both 2D composite and 3D voxel modes use deck.gl TileLayer for
 * viewport-based lazy loading. deck.gl manages tile lifecycle,
 * request cancellation (AbortSignal), and viewport culling.
 *
 * Modes:
 *   - Composite: TileLayer → BitmapLayer per tile (2D reflectivity PNG)
 *   - 3D Layers: TileLayer → PointCloudLayer per tile (volumetric voxels)
 */

const API_BASE = window.location.port === '8000'
  ? ''
  : `${window.location.protocol}//${window.location.hostname}:8000`;
const FRAME_COUNT = 60;
const REFRESH_INTERVAL_MS = 120_000;
const MAX_503_RETRIES = 24;
const TILE_MIN_ZOOM = 3;
const TILE_MAX_ZOOM = 8;
const VOXEL_MIN_ZOOM = 4;
const POINT_SIZE_PX = 4;

// ── Map style selection ───────────────────────────────────────────────────────

async function resolveMapStyle() {
  try {
    const resp = await fetch(`${API_BASE}/api/config`);
    if (resp.ok) {
      const cfg = await resp.json();
      if (cfg.stadia_api_key) {
        return `https://tiles.stadiamaps.com/styles/stamen_terrain.json?api_key=${cfg.stadia_api_key}`;
      }
      if (cfg.maptiler_api_key) {
        return `https://api.maptiler.com/maps/outdoor-v2/style.json?key=${cfg.maptiler_api_key}`;
      }
    }
  } catch (err) { console.warn('Could not load map config, using fallback:', err.message); }
  return 'https://tiles.openfreemap.org/styles/liberty';
}

// ── Animation state ───────────────────────────────────────────────────────────

let timestamps = [];
let currentIndex = 0;
let playing = false;
let frameInterval = 500;
let lastFrameTime = 0;
let animationId = null;
let mapRef = null;
let userOpacity = 0.8;
let fetchAbort = null;
let timestampFetchRetries = 0;
let refreshIntervalId = null;

// ── Status helpers ────────────────────────────────────────────────────────────

function setStatus(state, message) {
  const dot = document.getElementById('status-dot');
  const ts  = document.getElementById('timestamp');
  dot.className = state;
  ts.textContent = message || '';
}

function formatTimestamp(iso) {
  if (!iso) return '--:--';
  return new Date(iso).toLocaleTimeString('en-US', {
    timeZone: 'America/New_York',
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatTimestampFull(iso) {
  if (!iso) return '--';
  return new Date(iso).toLocaleString('en-US', {
    timeZone: 'America/New_York',
    hour12: false,
  });
}

// ── Timestamp loading ─────────────────────────────────────────────────────────

async function fetchTimestamps() {
  if (fetchAbort) fetchAbort.abort();
  fetchAbort = new AbortController();

  setStatus('loading', 'Loading timestamps…');
  try {
    const resp = await fetch(
      `${API_BASE}/api/radar/timestamps`,
      { cache: 'no-store', signal: fetchAbort.signal },
    );
    if (resp.status === 503) {
      timestampFetchRetries++;
      if (timestampFetchRetries < MAX_503_RETRIES) {
        setStatus('loading', 'Server seeding cache…');
        setTimeout(fetchTimestamps, 5_000);
      } else {
        setStatus('error', 'Server unavailable — try refreshing');
      }
      return false;
    }
    timestampFetchRetries = 0;
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    timestamps = data.timestamps;
    currentIndex = timestamps.length - 1;

    updateScrubber();
    updateFrameDisplay();
    showFrame(currentIndex);

    const newest = timestamps[timestamps.length - 1];
    setStatus('', `Latest: ${formatTimestampFull(newest?.timestamp)} ET`);

    return true;
  } catch (err) {
    if (err.name === 'AbortError') return false;
    console.error('Timestamp fetch error:', err);
    setStatus('error', `Error: ${err.message}`);
    return false;
  }
}

// ── 2D Composite tile layer ──────────────────────────────────────────────────

function buildTileLayer(timestamp) {
  const tileUrl = `${API_BASE}/api/radar/tiles/${encodeURIComponent(timestamp)}/{z}/{x}/{y}.png`;
  return new deck.TileLayer({
    id: 'radar-tiles',
    data: tileUrl,
    minZoom: TILE_MIN_ZOOM,
    maxZoom: TILE_MAX_ZOOM,
    tileSize: 256,
    opacity: userOpacity,
    loadOptions: { fetch: { cache: 'force-cache' } },
    renderSubLayers: props => {
      const { west, south, east, north } = props.tile.bbox;
      return new deck.BitmapLayer(props, {
        data: null,
        image: props.data,
        bounds: [west, south, east, north],
        textureParameters: {
          minFilter: 'linear',
          magFilter: 'linear',
        },
      });
    },
  });
}

// ── 3D Voxel tile layer ─────────────────────────────────────────────────────

let deckOverlay = null;
let viewMode = 'composite';
let verticalExaggeration = 3.0;

function dbzToRgba(dbz) {
  const bands = NWS_DBZ_COLORS;
  const alpha = Math.min(255, Math.max(100, Math.round(100 + (dbz - 10) * (155 / 50))));
  for (const band of bands) {
    if (dbz >= band.min && dbz < band.max) return [band.r, band.g, band.b, alpha];
  }
  if (dbz >= 65) return [200, 200, 255, 255];
  return [0, 0, 0, 0];
}

function buildVoxelTileLayer(timestamp) {
  const exag = verticalExaggeration;
  const tileUrl = `${API_BASE}/api/radar/volume/tiles/${encodeURIComponent(timestamp)}/{z}/{x}/{y}.json`;
  return new deck.TileLayer({
    id: 'radar-volume-tiles',
    data: tileUrl,
    minZoom: VOXEL_MIN_ZOOM,
    maxZoom: TILE_MAX_ZOOM,
    tileSize: 256,
    getTileData: (tile) => {
      const { x, y, z } = tile.index;
      const url = `${API_BASE}/api/radar/volume/tiles/${encodeURIComponent(timestamp)}/${z}/${x}/${y}.json`;
      return fetch(url, { signal: tile.signal, cache: 'force-cache' })
        .then(res => {
          if (!res.ok) return null;
          return res.json();
        })
        .catch(err => {
          if (err.name === 'AbortError') return null;
          console.warn('Voxel tile fetch error:', err);
          return null;
        });
    },
    renderSubLayers: props => {
      if (!props.data?.voxels?.length) return null;
      return new deck.PointCloudLayer({
        ...props,
        id: `${props.id}-points`,
        data: props.data.voxels,
        getPosition: d => [d[0], d[1], d[2] * exag],
        getColor: d => dbzToRgba(d[3]),
        pointSize: POINT_SIZE_PX,
        sizeUnits: 'pixels',
        opacity: 0.9,
        pickable: false,
        material: false,
        parameters: { depthTest: true },
      });
    },
  });
}

// ── Frame display ────────────────────────────────────────────────────────────

function showFrame(index) {
  if (!mapRef || !deckOverlay || timestamps.length === 0) return;
  const ts = timestamps[index];
  if (!ts) return;

  if (viewMode === 'composite') {
    deckOverlay.setProps({ layers: [buildTileLayer(ts.timestamp)] });
  } else {
    deckOverlay.setProps({ layers: [buildVoxelTileLayer(ts.timestamp)] });
  }
}

// ── Animation loop ────────────────────────────────────────────────────────────

function animationTick(now) {
  if (!playing) return;

  if (now - lastFrameTime >= frameInterval) {
    lastFrameTime = now;
    currentIndex = (currentIndex + 1) % timestamps.length;
    showFrame(currentIndex);
    updateFrameDisplay();
  }

  animationId = requestAnimationFrame(animationTick);
}

function play() {
  if (timestamps.length < 2) return;
  playing = true;
  lastFrameTime = performance.now();
  document.getElementById('icon-play').style.display = 'none';
  document.getElementById('icon-pause').style.display = '';
  animationId = requestAnimationFrame(animationTick);
}

function pause() {
  playing = false;
  document.getElementById('icon-play').style.display = '';
  document.getElementById('icon-pause').style.display = 'none';
  if (animationId) {
    cancelAnimationFrame(animationId);
    animationId = null;
  }
}

function togglePlay() {
  playing ? pause() : play();
}

// ── UI sync ───────────────────────────────────────────────────────────────────

function updateScrubber() {
  const scrubber = document.getElementById('frame-scrubber');
  scrubber.max = Math.max(0, timestamps.length - 1);
  scrubber.value = currentIndex;
}

function updateFrameDisplay() {
  const scrubber = document.getElementById('frame-scrubber');
  scrubber.value = currentIndex;

  const timeEl = document.getElementById('frame-time');
  const counterEl = document.getElementById('frame-counter');
  const ts = timestamps[currentIndex];

  timeEl.textContent = ts ? formatTimestamp(ts.timestamp) : '--:--';
  counterEl.textContent = timestamps.length > 0 ? `${currentIndex + 1}/${timestamps.length}` : '0/0';
}

// ── View mode toggle ──────────────────────────────────────────────────────────

function setViewMode(mode) {
  if (mode === viewMode) return;
  viewMode = mode;

  const map = mapRef;
  if (!map) return;

  document.querySelectorAll('#view-mode-seg .seg-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });

  const rowExag = document.getElementById('row-exaggeration');

  if (mode === 'composite') {
    map.easeTo({ pitch: 0, bearing: 0, duration: 600 });
    rowExag.style.display = 'none';
  } else {
    map.easeTo({ pitch: 50, duration: 600 });
    rowExag.style.display = '';
  }

  showFrame(currentIndex);
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  const styleUrl = await resolveMapStyle();

  const map = new maplibregl.Map({
    container: 'map',
    style: styleUrl,
    center: [-98.5, 39.8],
    zoom: 4,
    pitch: 0,
    bearing: 0,
    attributionControl: true,
  });
  mapRef = map;

  deckOverlay = new deck.MapboxOverlay({ layers: [] });
  map.addControl(deckOverlay);

  map.addControl(new maplibregl.NavigationControl(), 'top-left');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

  map.on('load', async () => {
    map.addSource('terrain', {
      type: 'raster-dem',
      tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
      encoding: 'terrarium',
      tileSize: 256,
      maxzoom: 15,
    });
    map.setTerrain({ source: 'terrain', exaggeration: 1.5 });

    const ok = await fetchTimestamps();
    if (ok && timestamps.length >= 2) {
      play();
    }

    refreshIntervalId = setInterval(async () => {
      const wasPlaying = playing;
      if (wasPlaying) pause();
      await fetchTimestamps();
      if (wasPlaying && timestamps.length >= 2) play();
    }, REFRESH_INTERVAL_MS);
  });

  // ── Opacity slider ──────────────────────────────────────────────────────

  const opacitySlider = document.getElementById('opacity-slider');
  const opacityValue  = document.getElementById('opacity-value');

  opacitySlider.addEventListener('input', () => {
    userOpacity = parseFloat(opacitySlider.value);
    opacityValue.textContent = Math.round(userOpacity * 100) + '%';
    if (viewMode === 'composite') showFrame(currentIndex);
  });

  // ── Play / Pause ────────────────────────────────────────────────────────

  document.getElementById('btn-play').addEventListener('click', togglePlay);

  // ── Frame scrubber ──────────────────────────────────────────────────────

  const scrubber = document.getElementById('frame-scrubber');
  let scrubTimer = null;
  scrubber.addEventListener('input', () => {
    if (playing) pause();
    currentIndex = parseInt(scrubber.value, 10);
    updateFrameDisplay();
    if (scrubTimer) clearTimeout(scrubTimer);
    scrubTimer = setTimeout(() => showFrame(currentIndex), 150);
  });

  // ── Speed select ────────────────────────────────────────────────────────

  document.getElementById('speed-select').addEventListener('change', (e) => {
    frameInterval = parseInt(e.target.value, 10);
  });

  // ── View mode segmented control ──────────────────────────────────────────

  document.querySelectorAll('#view-mode-seg .seg-btn').forEach(btn => {
    btn.addEventListener('click', () => setViewMode(btn.dataset.mode));
  });

  // ── Vertical exaggeration ───────────────────────────────────────────────

  document.getElementById('exag-slider').addEventListener('input', (e) => {
    verticalExaggeration = parseFloat(e.target.value);
    document.getElementById('exag-value').textContent = `${verticalExaggeration}x`;
    if (viewMode === '3d') showFrame(currentIndex);
  });

  // ── Refresh button ──────────────────────────────────────────────────────

  document.getElementById('btn-refresh').addEventListener('click', async () => {
    const wasPlaying = playing;
    if (wasPlaying) pause();
    await fetchTimestamps();
    if (wasPlaying && timestamps.length >= 2) play();
  });

  // ── Reset view ──────────────────────────────────────────────────────────

  document.getElementById('btn-reset-view').addEventListener('click', () => {
    const pitch = viewMode === '3d' ? 50 : 0;
    map.flyTo({ center: [-98.5, 39.8], zoom: 4, pitch, bearing: 0, duration: 800 });
  });

  // ── Legend ──────────────────────────────────────────────────────────────

  const legendEl     = document.getElementById('legend');
  const legendToggle = document.getElementById('btn-legend');

  buildLegend(legendEl);

  legendToggle.addEventListener('click', () => {
    legendEl.classList.toggle('visible');
    legendToggle.textContent = legendEl.classList.contains('visible') ? 'Hide legend' : 'Show legend';
  });
}

init();
