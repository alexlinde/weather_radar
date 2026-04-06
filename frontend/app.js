/**
 * NYC Weather Radar — Main application with frame animation + 3D volume.
 *
 * All radar rendering uses deck.gl on top of a MapLibre base map:
 *   - Composite mode: BitmapLayer (2D reflectivity PNG)
 *   - 3D Layers mode: PointCloudLayer (volumetric voxels)
 */

const API_BASE = window.location.port === '8000'
  ? ''
  : `${window.location.protocol}//${window.location.hostname}:8000`;
const FRAME_COUNT = 60;
const REFRESH_INTERVAL_MS = 120_000;
const MAX_503_RETRIES = 24; // ~2 minutes at 5s intervals

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

let frames = [];           // { timestamp, imageUrl, bounds }
let currentIndex = 0;
let playing = false;
let frameInterval = 500;   // ms between frames (1x speed)
let lastFrameTime = 0;
let animationId = null;
let mapRef = null;
let userOpacity = 0.8;
let fetchAbort = null;
let frameFetchRetries = 0;
let volumeFetchRetries = 0;
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

// ── Frame loading ─────────────────────────────────────────────────────────────

function preloadImage(dataUri) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = dataUri;
  });
}

async function fetchFrames() {
  if (fetchAbort) fetchAbort.abort();
  fetchAbort = new AbortController();

  setStatus('loading', 'Loading frames…');
  try {
    const resp = await fetch(
      `${API_BASE}/api/radar/frames?count=${FRAME_COUNT}`,
      { cache: 'no-store', signal: fetchAbort.signal },
    );
    if (resp.status === 503) {
      frameFetchRetries++;
      if (frameFetchRetries < MAX_503_RETRIES) {
        setStatus('loading', 'Server seeding cache…');
        setTimeout(fetchFrames, 5_000);
      } else {
        setStatus('error', 'Server unavailable — try refreshing');
      }
      return false;
    }
    frameFetchRetries = 0;
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    const loaded = await Promise.all(
      data.frames.map(async (f) => {
        await preloadImage(f.image);
        return {
          timestamp: f.timestamp,
          imageUrl: f.image,
          bounds: f.bounds,
        };
      })
    );

    frames = loaded;
    currentIndex = frames.length - 1;

    updateScrubber();
    updateFrameDisplay();
    showFrame(currentIndex);

    const newest = frames[frames.length - 1];
    setStatus('', `Latest: ${formatTimestampFull(newest?.timestamp)} ET`);

    return true;
  } catch (err) {
    if (err.name === 'AbortError') return false;
    console.error('Frame fetch error:', err);
    setStatus('error', `Error: ${err.message}`);
    return false;
  }
}

// ── Radar layer rendering (deck.gl for both composite + 3D) ──────────────────

function buildBitmapLayer(frame) {
  const b = frame.bounds;
  return new deck.BitmapLayer({
    id: 'radar-composite',
    image: frame.imageUrl,
    bounds: [b.west, b.south, b.east, b.north],
    opacity: userOpacity,
    pickable: false,
    textureParameters: {
      minFilter: 'linear',
      magFilter: 'linear',
    },
  });
}

function showFrame(index) {
  if (!mapRef || !deckOverlay || frames.length === 0) return;
  const frame = frames[index];
  if (!frame) return;

  if (viewMode === 'composite') {
    deckOverlay.setProps({ layers: [buildBitmapLayer(frame)] });
  } else {
    showVolumeFrame(index);
  }

  updateBbox(mapRef, frame.bounds);
}

function updateBbox(map, bounds) {
  const bboxGeoJSON = {
    type: 'Feature',
    geometry: {
      type: 'Polygon',
      coordinates: [[
        [bounds.west, bounds.south],
        [bounds.east, bounds.south],
        [bounds.east, bounds.north],
        [bounds.west, bounds.north],
        [bounds.west, bounds.south],
      ]],
    },
  };

  if (map.getSource('radar-bbox')) {
    map.getSource('radar-bbox').setData(bboxGeoJSON);
  } else {
    map.addSource('radar-bbox', { type: 'geojson', data: bboxGeoJSON });
    map.addLayer({
      id: 'radar-bbox-line',
      type: 'line',
      source: 'radar-bbox',
      paint: {
        'line-color': '#58a6ff',
        'line-width': 1.5,
        'line-dasharray': [4, 3],
        'line-opacity': 0.7,
      },
    });
  }
}

// ── Animation loop ────────────────────────────────────────────────────────────

function animationTick(now) {
  if (!playing) return;

  if (now - lastFrameTime >= frameInterval) {
    lastFrameTime = now;
    currentIndex = (currentIndex + 1) % frames.length;
    showFrame(currentIndex);
    updateFrameDisplay();
  }

  animationId = requestAnimationFrame(animationTick);
}

function play() {
  if (frames.length < 2) return;
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
  scrubber.max = Math.max(0, frames.length - 1);
  scrubber.value = currentIndex;
}

function updateFrameDisplay() {
  const scrubber = document.getElementById('frame-scrubber');
  scrubber.value = currentIndex;

  const timeEl = document.getElementById('frame-time');
  const counterEl = document.getElementById('frame-counter');
  const frame = frames[currentIndex];

  timeEl.textContent = frame ? formatTimestamp(frame.timestamp) : '--:--';
  counterEl.textContent = frames.length > 0 ? `${currentIndex + 1}/${frames.length}` : '0/0';
}

// ── 3D Volume (deck.gl) ───────────────────────────────────────────────────────

let deckOverlay = null;
let viewMode = 'composite'; // 'composite' | '3d'
let verticalExaggeration = 3.0;
let volumeFrames = [];   // [{timestamp, voxels}] — parallel to frames[]
const POINT_SIZE_PX = 3;

function dbzToRgba(dbz) {
  const bands = NWS_DBZ_COLORS;
  const alpha = Math.min(255, Math.max(100, Math.round(100 + (dbz - 10) * (155 / 50))));
  for (const band of bands) {
    if (dbz >= band.min && dbz < band.max) return [band.r, band.g, band.b, alpha];
  }
  if (dbz >= 65) return [200, 200, 255, 255];
  return [0, 0, 0, 0];
}

function buildPointCloudLayer(voxels, exaggeration) {
  return new deck.PointCloudLayer({
    id: 'radar-volume',
    data: voxels,
    // d = [lon, lat, altitude_m, dbz]
    getPosition: d => [d[0], d[1], d[2] * exaggeration],
    getColor: d => dbzToRgba(d[3]),
    pointSize: POINT_SIZE_PX,
    sizeUnits: 'pixels',
    opacity: 0.9,
    pickable: false,
    material: false,
    parameters: { depthTest: true },
  });
}

function showVolumeFrame(index) {
  if (viewMode !== '3d' || !deckOverlay || volumeFrames.length === 0) return;
  const vf = volumeFrames[Math.min(index, volumeFrames.length - 1)];
  if (!vf) return;
  deckOverlay.setProps({ layers: [buildPointCloudLayer(vf.voxels, verticalExaggeration)] });
}

async function fetchVolumeFrames() {
  if (viewMode !== '3d' || !deckOverlay) return;
  document.getElementById('volume-status').textContent = 'Loading…';
  try {
    const resp = await fetch(
      `${API_BASE}/api/radar/volume/frames?count=${FRAME_COUNT}`,
      { cache: 'no-store' },
    );
    if (resp.status === 503) {
      volumeFetchRetries++;
      if (volumeFetchRetries < MAX_503_RETRIES) {
        document.getElementById('volume-status').textContent = 'Seeding…';
        setTimeout(fetchVolumeFrames, 10_000);
      } else {
        document.getElementById('volume-status').textContent = 'Unavailable';
      }
      return;
    }
    volumeFetchRetries = 0;
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    volumeFrames = data.frames;
    const totalVoxels = data.frames.reduce((s, f) => s + f.voxels.length, 0);
    document.getElementById('volume-status').textContent =
      `${data.count} frames · ${(totalVoxels / 1000).toFixed(0)}K pts`;
    showVolumeFrame(currentIndex);
  } catch (err) {
    console.error('Volume frames fetch error:', err);
    document.getElementById('volume-status').textContent = 'Error';
  }
}

function clearVolumeLayer() {
  volumeFrames = [];
  if (deckOverlay) deckOverlay.setProps({ layers: [] });
  document.getElementById('volume-status').textContent = '';
}

async function setViewMode(mode) {
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
    volumeFrames = [];
    showFrame(currentIndex);
  } else {
    map.easeTo({ pitch: 50, duration: 600 });
    rowExag.style.display = '';
    await fetchVolumeFrames();
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  const styleUrl = await resolveMapStyle();

  const map = new maplibregl.Map({
    container: 'map',
    style: styleUrl,
    center: [-73.98, 40.75],
    zoom: 10,
    pitch: 0,
    bearing: 0,
    attributionControl: true,
  });
  mapRef = map;

  // Initialise deck.gl overlay (empty layer list until volume is enabled)
  deckOverlay = new deck.MapboxOverlay({ layers: [] });
  map.addControl(deckOverlay);

  map.addControl(new maplibregl.NavigationControl(), 'top-left');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

  map.on('load', async () => {
    // 3D terrain
    map.addSource('terrain', {
      type: 'raster-dem',
      tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
      encoding: 'terrarium',
      tileSize: 256,
      maxzoom: 15,
    });
    map.setTerrain({ source: 'terrain', exaggeration: 1.5 });

    const ok = await fetchFrames();
    if (ok && frames.length >= 2) {
      play();
    }

    refreshIntervalId = setInterval(async () => {
      const wasPlaying = playing;
      if (wasPlaying) pause();
      await fetchFrames();
      if (wasPlaying && frames.length >= 2) play();
      if (viewMode === '3d') await fetchVolumeFrames();
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
  scrubber.addEventListener('input', () => {
    const wasPlaying = playing;
    if (wasPlaying) pause();
    currentIndex = parseInt(scrubber.value, 10);
    showFrame(currentIndex);
    updateFrameDisplay();
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
    if (viewMode === '3d') showVolumeFrame(currentIndex);
  });

  // ── Refresh button ──────────────────────────────────────────────────────

  document.getElementById('btn-refresh').addEventListener('click', async () => {
    const wasPlaying = playing;
    if (wasPlaying) pause();
    await fetchFrames();
    if (wasPlaying && frames.length >= 2) play();
    if (viewMode === '3d') await fetchVolumeFrames();
  });

  // ── Reset view ──────────────────────────────────────────────────────────

  document.getElementById('btn-reset-view').addEventListener('click', () => {
    const pitch = viewMode === '3d' ? 50 : 0;
    map.flyTo({ center: [-73.98, 40.75], zoom: 10, pitch, bearing: 0, duration: 800 });
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
