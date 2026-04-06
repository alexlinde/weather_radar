/**
 * NYC Weather Radar — Main application with frame animation + 3D volume.
 *
 * Phase 2: fetches 60 recent radar frames and animates them on a MapLibre raster layer.
 * Phase 3: adds 3D terrain and a deck.gl ColumnLayer for volumetric radar columns.
 */

const API_BASE = 'http://127.0.0.1:8000';
const FRAME_COUNT = 60;
const REFRESH_INTERVAL_MS = 120_000;

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
  } catch (_) { /* ignore */ }
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
  setStatus('loading', 'Loading frames…');
  try {
    const resp = await fetch(`${API_BASE}/api/radar/frames?count=${FRAME_COUNT}`, { cache: 'no-store' });
    if (resp.status === 503) {
      setStatus('loading', 'Server seeding cache…');
      setTimeout(fetchFrames, 5_000);
      return false;
    }
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
    console.error('Frame fetch error:', err);
    setStatus('error', `Error: ${err.message}`);
    return false;
  }
}

// ── Radar layer rendering ─────────────────────────────────────────────────────

const SOURCE_ID = 'radar-image';
const LAYER_ID  = 'radar-layer';

function boundsToCoordinates(b) {
  return [
    [b.west, b.north],   // NW
    [b.east, b.north],   // NE
    [b.east, b.south],   // SE
    [b.west, b.south],   // SW
  ];
}

function ensureRadarLayer(map) {
  if (map.getSource(SOURCE_ID)) return;
  const placeholder = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';
  map.addSource(SOURCE_ID, {
    type: 'image',
    url: placeholder,
    coordinates: [[-75.23, 42.0], [-72.67, 42.0], [-72.67, 39.44], [-75.23, 39.44]],
  });
  map.addLayer({
    id: LAYER_ID,
    type: 'raster',
    source: SOURCE_ID,
    paint: {
      'raster-opacity': userOpacity,
      'raster-fade-duration': 0,
      'raster-resampling': 'linear',
    },
  });
}

function showFrame(index) {
  if (!mapRef || frames.length === 0) return;
  const map = mapRef;
  const frame = frames[index];
  if (!frame) return;

  const coords = boundsToCoordinates(frame.bounds);
  const src = map.getSource(SOURCE_ID);
  if (src) {
    src.updateImage({ url: frame.imageUrl, coordinates: coords });
  }

  updateBbox(map, frame.bounds);
  showVolumeFrame(index);
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
let volumeEnabled = false;
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
    pickable: true,
    material: false,
    parameters: { depthTest: true },
  });
}

function showVolumeFrame(index) {
  if (!volumeEnabled || !deckOverlay || volumeFrames.length === 0) return;
  const vf = volumeFrames[Math.min(index, volumeFrames.length - 1)];
  if (!vf) return;
  deckOverlay.setProps({ layers: [buildPointCloudLayer(vf.voxels, verticalExaggeration)] });
}

async function fetchVolumeFrames() {
  if (!volumeEnabled || !deckOverlay) return;
  document.getElementById('volume-status').textContent = 'Loading…';
  try {
    const resp = await fetch(`${API_BASE}/api/radar/volume/frames?count=${FRAME_COUNT}`, { cache: 'no-store' });
    if (resp.status === 503) {
      document.getElementById('volume-status').textContent = 'Seeding…';
      setTimeout(fetchVolumeFrames, 10_000);
      return;
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    volumeFrames = data.frames;
    const totalVoxels = data.frames.reduce((s, f) => s + f.voxels.length, 0);
    document.getElementById('volume-status').textContent =
      `On · ${data.count} frames · ${(totalVoxels / 1000).toFixed(0)}K pts`;
    showVolumeFrame(currentIndex);
  } catch (err) {
    console.error('Volume frames fetch error:', err);
    document.getElementById('volume-status').textContent = 'Error';
  }
}

function clearVolumeLayer() {
  volumeFrames = [];
  if (deckOverlay) deckOverlay.setProps({ layers: [] });
  document.getElementById('volume-status').textContent = 'Off';
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  const styleUrl = await resolveMapStyle();

  const map = new maplibregl.Map({
    container: 'map',
    style: styleUrl,
    center: [-73.98, 40.75],
    zoom: 10,
    pitch: 45,
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
    });
    map.setTerrain({ source: 'terrain', exaggeration: 1.5 });

    ensureRadarLayer(map);
    const ok = await fetchFrames();
    if (ok && frames.length >= 2) {
      play();
    }

    setInterval(async () => {
      const wasPlaying = playing;
      if (wasPlaying) pause();
      await fetchFrames();
      if (wasPlaying && frames.length >= 2) play();
      if (volumeEnabled) await fetchVolumeFrames();
    }, REFRESH_INTERVAL_MS);
  });

  // ── Opacity slider ──────────────────────────────────────────────────────

  const opacitySlider = document.getElementById('opacity-slider');
  const opacityValue  = document.getElementById('opacity-value');

  opacitySlider.addEventListener('input', () => {
    userOpacity = parseFloat(opacitySlider.value);
    opacityValue.textContent = Math.round(userOpacity * 100) + '%';
    if (map.getLayer(LAYER_ID)) {
      map.setPaintProperty(LAYER_ID, 'raster-opacity', userOpacity);
    }
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

  // ── 3D Volume toggle ────────────────────────────────────────────────────

  document.getElementById('toggle-volume').addEventListener('change', async (e) => {
    volumeEnabled = e.target.checked;
    const rowExag = document.getElementById('row-exaggeration');
    if (volumeEnabled) {
      rowExag.style.display = '';
      await fetchVolumeFrames();
    } else {
      rowExag.style.display = 'none';
      clearVolumeLayer();
    }
  });

  // ── Vertical exaggeration ───────────────────────────────────────────────

  document.getElementById('exag-slider').addEventListener('input', (e) => {
    verticalExaggeration = parseFloat(e.target.value);
    document.getElementById('exag-value').textContent = `${verticalExaggeration}x`;
    // Re-render current frame at new scale — no network request needed
    if (volumeEnabled) showVolumeFrame(currentIndex);
  });

  // ── Refresh button ──────────────────────────────────────────────────────

  document.getElementById('btn-refresh').addEventListener('click', async () => {
    const wasPlaying = playing;
    if (wasPlaying) pause();
    await fetchFrames();
    if (wasPlaying && frames.length >= 2) play();
    if (volumeEnabled) await fetchVolumeFrames();
  });

  // ── Reset view ──────────────────────────────────────────────────────────

  document.getElementById('btn-reset-view').addEventListener('click', () => {
    map.flyTo({ center: [-73.98, 40.75], zoom: 10, pitch: 45, bearing: 0, duration: 800 });
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
