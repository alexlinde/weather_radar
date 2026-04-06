/**
 * Weather Radar — Atlas tile architecture with custom WebGL radar layer.
 *
 * Uses RadarLayer (CustomLayerInterface) for GPU-rendered radar overlays.
 * Atlas tiles (256×2048 grayscale PNG, 8 tilt bands) are loaded per-tile
 * and rendered with GLSL shaders for dBZ colorisation, temporal interpolation,
 * and spatial smoothing.
 */

const API_BASE = '';
const REFRESH_INTERVAL_MS = 120_000;
const MAX_503_RETRIES = 24;
const VIEWPORT_DEBOUNCE_MS = 300;

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
let currentAnimationTime = 0; // float index — e.g. 3.7 = 70% between frame 3 and 4
let playing = false;
let frameInterval = 500;      // ms per frame
let animationId = null;
let mapRef = null;
let userOpacity = 0.8;
let fetchAbort = null;
let timestampFetchRetries = 0;
let refreshIntervalId = null;

// ── Radar layer ──────────────────────────────────────────────────────────────

let radarLayer = null;
let viewMode = 'composite';
let verticalExaggeration = 3.0;
let activePreset = 'all';

const RADAR_PRESETS = {
  all:    { dbzMin: 5,  dbzMax: 75, intensity: 0.8 },
  precip: { dbzMin: 15, dbzMax: 75, intensity: 0.85 },
  severe: { dbzMin: 40, dbzMax: 75, intensity: 0.95 },
  custom: null,
};

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
    currentAnimationTime = timestamps.length - 1;

    if (radarLayer) radarLayer.setTimestamps(timestamps);

    updateScrubber();
    updateFrameDisplay();

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

// ── Frame display ─────────────────────────────────────────────────────────────

function getCurrentFrameIndex() {
  return Math.floor(currentAnimationTime) % Math.max(1, timestamps.length);
}

function showFrame() {
  if (!radarLayer || timestamps.length === 0) return;

  const len = timestamps.length;
  const t = ((currentAnimationTime % len) + len) % len;
  const frameA = Math.floor(t);
  const frameB = (frameA + 1) % len;
  const mix = t - frameA;

  radarLayer.setAnimation(frameA, frameB, mix);

  // Prefetch adjacent frames
  radarLayer.prefetchFrame((frameB + 1) % len);
}

async function loadAndShowFrame() {
  if (!radarLayer || timestamps.length === 0) return;

  const len = timestamps.length;
  const t = ((currentAnimationTime % len) + len) % len;
  const frameA = Math.floor(t);
  const frameB = (frameA + 1) % len;

  await radarLayer.ensureTextures(frameA, frameB);
  showFrame();
}

// ── Animation loop ────────────────────────────────────────────────────────────

let lastAnimTime = 0;

function animationTick(now) {
  if (!playing) return;

  if (lastAnimTime > 0) {
    const dt = now - lastAnimTime;
    const step = dt / frameInterval;
    currentAnimationTime += step;
    if (currentAnimationTime >= timestamps.length) {
      currentAnimationTime -= timestamps.length;
    }
    showFrame();
    updateFrameDisplay();
  }
  lastAnimTime = now;

  animationId = requestAnimationFrame(animationTick);
}

function play() {
  if (timestamps.length < 2) return;
  playing = true;
  lastAnimTime = 0;
  document.getElementById('icon-play').style.display = 'none';
  document.getElementById('icon-pause').style.display = '';
  animationId = requestAnimationFrame(animationTick);
}

function pause() {
  playing = false;
  lastAnimTime = 0;
  document.getElementById('icon-play').style.display = '';
  document.getElementById('icon-pause').style.display = 'none';
  if (animationId) {
    cancelAnimationFrame(animationId);
    animationId = null;
  }
  showFrame();
}

function togglePlay() {
  playing ? pause() : play();
}

// ── UI sync ───────────────────────────────────────────────────────────────────

function updateScrubber() {
  const scrubber = document.getElementById('frame-scrubber');
  scrubber.max = Math.max(0, timestamps.length - 1);
  scrubber.value = getCurrentFrameIndex();
}

function updateFrameDisplay() {
  const idx = getCurrentFrameIndex();
  const scrubber = document.getElementById('frame-scrubber');
  scrubber.value = idx;

  const timeEl = document.getElementById('frame-time');
  const counterEl = document.getElementById('frame-counter');
  const ts = timestamps[idx];

  timeEl.textContent = ts ? formatTimestamp(ts.timestamp) : '--:--';
  counterEl.textContent = timestamps.length > 0 ? `${idx + 1}/${timestamps.length}` : '0/0';
}

// ── View mode toggle ──────────────────────────────────────────────────────────

function setViewMode(mode, { animateCamera = true } = {}) {
  if (mode === viewMode) return;
  viewMode = mode;

  const map = mapRef;
  if (!map) return;

  document.querySelectorAll('#view-mode-seg .seg-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });

  const rowExag = document.getElementById('row-exaggeration');

  if (mode === 'composite') {
    if (animateCamera) map.easeTo({ pitch: 0, bearing: 0, duration: 600 });
    rowExag.style.display = 'none';
  } else {
    if (animateCamera) map.easeTo({ pitch: 50, duration: 600 });
    rowExag.style.display = '';
  }

  if (radarLayer) radarLayer.setMode(mode);
  showFrame();
}

// ── Viewport change handler ───────────────────────────────────────────────────

let viewportDebounceTimer = null;

function onViewportChange() {
  if (viewportDebounceTimer) clearTimeout(viewportDebounceTimer);
  viewportDebounceTimer = setTimeout(async () => {
    if (!radarLayer || !mapRef) return;
    radarLayer.updateVisibleTiles();
    await loadAndShowFrame();
  }, VIEWPORT_DEBOUNCE_MS);
}

// ── URL hash ↔ map view sync ─────────────────────────────────────────────────

function parseHash() {
  const h = window.location.hash.replace('#', '');
  if (!h) return null;
  const parts = h.split('/').map(Number);
  if (parts.length >= 3 && parts.every(n => !isNaN(n))) {
    const [zoom, lat, lng, bearing, pitch] = parts;
    return { center: [lng, lat], zoom, bearing: bearing || 0, pitch: pitch || 0 };
  }
  return null;
}

function updateHash(map) {
  const c = map.getCenter();
  const z = map.getZoom().toFixed(2);
  const lat = c.lat.toFixed(4);
  const lng = c.lng.toFixed(4);
  const b = map.getBearing().toFixed(1);
  const p = map.getPitch().toFixed(1);
  history.replaceState(null, '', `#${z}/${lat}/${lng}/${b}/${p}`);
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  const styleUrl = await resolveMapStyle();

  const saved = parseHash();
  if (saved && saved.pitch > 0) viewMode = '3d';

  const map = new maplibregl.Map({
    container: 'map',
    style: styleUrl,
    center: saved?.center || [-98.5, 39.8],
    zoom: saved?.zoom ?? 4,
    pitch: saved?.pitch ?? 0,
    bearing: saved?.bearing ?? 0,
    attributionControl: true,
  });
  mapRef = map;

  // Sync button active states with restored view mode
  document.querySelectorAll('#view-mode-seg .seg-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === viewMode);
  });
  if (viewMode === '3d') {
    document.getElementById('row-exaggeration').style.display = '';
  }

  map.on('moveend', () => updateHash(map));
  map.on('zoomend', () => updateHash(map));
  map.on('pitchend', () => {
    updateHash(map);
    if (map.getPitch() > 0 && viewMode === 'composite') {
      setViewMode('3d', { animateCamera: false });
    }
  });
  map.on('rotateend', () => updateHash(map));
  updateHash(map);

  map.addControl(new maplibregl.NavigationControl(), 'top-left');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

  map.on('load', async () => {
    // TODO: re-enable terrain once tile seam issue is resolved
    // map.addSource('terrain', {
    //   type: 'raster-dem',
    //   tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
    //   encoding: 'terrarium',
    //   tileSize: 256,
    //   maxzoom: 15,
    // });
    // map.setTerrain({ source: 'terrain', exaggeration: 1.5 });

    radarLayer = new RadarLayer();
    map.addLayer(radarLayer);
    if (viewMode !== 'composite') radarLayer.setMode(viewMode);

    const ok = await fetchTimestamps();
    if (ok && timestamps.length > 0) {
      radarLayer.updateVisibleTiles();
      await loadAndShowFrame();
      if (timestamps.length >= 2) {
        play();
      }
    }

    refreshIntervalId = setInterval(async () => {
      const wasPlaying = playing;
      if (wasPlaying) pause();
      await fetchTimestamps();
      if (timestamps.length > 0) {
        radarLayer.updateVisibleTiles();
        await loadAndShowFrame();
      }
      if (wasPlaying && timestamps.length >= 2) play();
    }, REFRESH_INTERVAL_MS);
  });

  map.on('moveend', onViewportChange);

  // ── Intensity slider ─────────────────────────────────────────────────────

  const opacitySlider = document.getElementById('opacity-slider');
  const opacityValue  = document.getElementById('opacity-value');

  opacitySlider.addEventListener('input', () => {
    userOpacity = parseFloat(opacitySlider.value);
    opacityValue.textContent = Math.round(userOpacity * 100) + '%';
    if (radarLayer) radarLayer.setOpacity(userOpacity);
    if (activePreset !== 'custom') switchPreset('custom');
  });

  // ── Play / Pause ────────────────────────────────────────────────────────

  document.getElementById('btn-play').addEventListener('click', togglePlay);

  // ── Frame scrubber ──────────────────────────────────────────────────────

  const scrubber = document.getElementById('frame-scrubber');
  scrubber.addEventListener('input', () => {
    if (playing) pause();
    currentAnimationTime = parseInt(scrubber.value, 10);
    updateFrameDisplay();
    showFrame();
    loadAndShowFrame();
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
    if (radarLayer) radarLayer.setVerticalExaggeration(verticalExaggeration);
  });

  // ── dBZ cutoff sliders ──────────────────────────────────────────────────

  const dbzMinSlider = document.getElementById('dbz-min-slider');
  const dbzMaxSlider = document.getElementById('dbz-max-slider');
  const dbzRangeValue = document.getElementById('dbz-range-value');

  function updateDbzRange() {
    let lo = parseInt(dbzMinSlider.value, 10);
    let hi = parseInt(dbzMaxSlider.value, 10);
    if (lo > hi) { [lo, hi] = [hi, lo]; }
    dbzRangeValue.textContent = `${lo} – ${hi}`;
    if (radarLayer) radarLayer.setDbzRange(lo, hi);
  }

  dbzMinSlider.addEventListener('input', updateDbzRange);
  dbzMaxSlider.addEventListener('input', updateDbzRange);

  // ── Radar presets ──────────────────────────────────────────────────────

  const presetSeg = document.getElementById('preset-seg');
  const dbzCutoffRow = document.getElementById('row-dbz-cutoff');

  function switchPreset(key) {
    activePreset = key;
    presetSeg.querySelectorAll('.seg-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.preset === key);
    });

    dbzCutoffRow.style.display = key === 'custom' ? '' : 'none';

    const preset = RADAR_PRESETS[key];
    if (!preset) return;

    dbzMinSlider.value = preset.dbzMin;
    dbzMaxSlider.value = preset.dbzMax;
    dbzRangeValue.textContent = `${preset.dbzMin} – ${preset.dbzMax}`;
    if (radarLayer) radarLayer.setDbzRange(preset.dbzMin, preset.dbzMax);

    userOpacity = preset.intensity;
    opacitySlider.value = preset.intensity;
    opacityValue.textContent = Math.round(preset.intensity * 100) + '%';
    if (radarLayer) radarLayer.setOpacity(preset.intensity);
  }

  presetSeg.querySelectorAll('.seg-btn').forEach(btn => {
    btn.addEventListener('click', () => switchPreset(btn.dataset.preset));
  });

  // ── Refresh button ──────────────────────────────────────────────────────

  document.getElementById('btn-refresh').addEventListener('click', async () => {
    const wasPlaying = playing;
    if (wasPlaying) pause();
    await fetchTimestamps();
    if (timestamps.length > 0 && radarLayer) {
      radarLayer.updateVisibleTiles();
      await loadAndShowFrame();
    }
    if (wasPlaying && timestamps.length >= 2) play();
  });

  // ── Reset view ──────────────────────────────────────────────────────────

  document.getElementById('btn-reset-view').addEventListener('click', () => {
    const pitch = (viewMode === '3d' || viewMode === 'volume') ? 50 : 0;
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
