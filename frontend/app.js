/**
 * Weather Radar — Unified voxel architecture with GPU-based frame filtering.
 *
 * All radar data (both 2D and 3D views) is loaded once into GPU buffers.
 * Frame animation uses deck.gl DataFilterExtension — changing the visible
 * frame only updates a single GPU uniform (filterRange), achieving the same
 * zero-cost scrubbing pattern as the deck.gl animated arc demo.
 */

const API_BASE = '';
const REFRESH_INTERVAL_MS = 120_000;
const MAX_503_RETRIES = 24;
const TILE_MIN_ZOOM = 3;
const TILE_MAX_ZOOM = 8;
const POINT_SIZE_2D = 6;
const POINT_SIZE_3D = 4;
const VIEWPORT_REFETCH_DEBOUNCE_MS = 400;

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

// ── Voxel data state ──────────────────────────────────────────────────────────

let deckOverlay = null;
let viewMode = 'composite';
let verticalExaggeration = 3.0;

// The combined GPU buffer data — set once after bulk fetch, stable reference
let voxelLayerData = null;
// Raw positions before vertical exaggeration (for re-scaling)
let rawPositions = null;
let currentBulkZoom = null;
let bulkFetchController = null;

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

// ── Tile coordinate computation ───────────────────────────────────────────────

function getVisibleTileCoords() {
  if (!mapRef) return [];
  const bounds = mapRef.getBounds();
  const zoom = Math.round(mapRef.getZoom());
  const z = Math.max(TILE_MIN_ZOOM, Math.min(TILE_MAX_ZOOM, zoom));
  const n = 2 ** z;
  const xMin = Math.max(0, Math.floor((bounds.getWest() + 180) / 360 * n));
  const xMax = Math.min(n - 1, Math.floor((bounds.getEast() + 180) / 360 * n));
  const latToY = lat => {
    const rad = lat * Math.PI / 180;
    return Math.floor((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * n);
  };
  const yMin = Math.max(0, latToY(bounds.getNorth()));
  const yMax = Math.min(n - 1, latToY(bounds.getSouth()));
  const coords = [];
  for (let x = xMin; x <= xMax; x++) {
    for (let y = yMin; y <= yMax; y++) {
      coords.push({ z, x, y });
    }
  }
  return coords;
}

// ── Bulk voxel fetch + combined buffer builder ────────────────────────────────

async function bulkFetchVoxels() {
  const tiles = getVisibleTileCoords();
  if (tiles.length === 0 || timestamps.length === 0) return;

  const z = tiles[0].z;
  if (z === currentBulkZoom && voxelLayerData) return;

  if (bulkFetchController) bulkFetchController.abort();
  bulkFetchController = new AbortController();
  const signal = bulkFetchController.signal;

  setStatus('loading', 'Loading radar data…');

  try {
    const fetches = tiles.map(({ z, x, y }) =>
      fetch(`${API_BASE}/api/radar/volume/bulk/${z}/${x}/${y}.bin`, { signal })
        .then(r => r.ok ? r.arrayBuffer() : null)
        .catch(err => {
          if (err.name === 'AbortError') throw err;
          console.warn('Bulk fetch error:', err);
          return null;
        })
    );

    const buffers = await Promise.all(fetches);

    const allPositions = [];
    const allColors = [];
    const allFrameIndices = [];
    let totalVoxels = 0;

    for (const buffer of buffers) {
      if (!buffer) continue;
      const view = new DataView(buffer);
      const frameCount = view.getUint16(0, true);
      let offset = 2;

      for (let fi = 0; fi < frameCount; fi++) {
        if (offset + 4 > buffer.byteLength) break;
        const count = view.getUint32(offset, true);
        offset += 4;
        if (count === 0) continue;
        if (count === 0xFFFFFFFF) continue;

        const posBytes = count * 3 * 4;
        const colorBytes = count * 4;
        if (offset + posBytes + colorBytes > buffer.byteLength) break;

        const positions = new Float32Array(buffer.slice(offset, offset + posBytes));
        offset += posBytes;
        const colors = new Uint8ClampedArray(buffer.slice(offset, offset + colorBytes));
        offset += colorBytes;

        allPositions.push(positions);
        allColors.push(colors);
        allFrameIndices.push(new Float32Array(count).fill(fi));
        totalVoxels += count;
      }
    }

    if (totalVoxels === 0) {
      voxelLayerData = null;
      rawPositions = null;
      currentBulkZoom = z;
      showFrame(currentIndex);
      setStatus('', 'No radar data in view');
      return;
    }

    const combinedPositions = new Float32Array(totalVoxels * 3);
    const combinedColors = new Uint8ClampedArray(totalVoxels * 4);
    const combinedFrameIndices = new Float32Array(totalVoxels);

    let posOff = 0, colOff = 0, idxOff = 0;
    for (let i = 0; i < allPositions.length; i++) {
      combinedPositions.set(allPositions[i], posOff);
      posOff += allPositions[i].length;
      combinedColors.set(allColors[i], colOff);
      colOff += allColors[i].length;
      combinedFrameIndices.set(allFrameIndices[i], idxOff);
      idxOff += allFrameIndices[i].length;
    }

    rawPositions = new Float32Array(combinedPositions);
    applyVerticalExaggeration(combinedPositions, verticalExaggeration);

    voxelLayerData = {
      length: totalVoxels,
      attributes: {
        getPosition: { value: combinedPositions, size: 3 },
        getColor: { value: combinedColors, size: 4 },
        getFilterValue: { value: combinedFrameIndices, size: 1 },
      },
    };

    currentBulkZoom = z;
    console.log(`Loaded ${totalVoxels.toLocaleString()} voxels across ${timestamps.length} frames (${(totalVoxels * 17 / 1e6).toFixed(1)} MB)`);

    showFrame(currentIndex);

    const newest = timestamps[timestamps.length - 1];
    setStatus('', `Latest: ${formatTimestampFull(newest?.timestamp)} ET`);

  } catch (err) {
    if (err.name === 'AbortError') return;
    console.error('Bulk voxel fetch failed:', err);
    setStatus('error', 'Failed to load radar data');
  }
}

function applyVerticalExaggeration(positions, exag) {
  for (let i = 2; i < positions.length; i += 3) {
    positions[i] = rawPositions[i] * exag;
  }
}

// ── Radar layer builder ───────────────────────────────────────────────────────

const dataFilterExt = new deck.DataFilterExtension({ filterSize: 1 });

function buildRadarLayer(frameIndex) {
  if (!voxelLayerData) return null;

  const is2D = viewMode === 'composite';
  return new deck.PointCloudLayer({
    id: 'radar-voxels',
    data: voxelLayerData,
    pointSize: is2D ? POINT_SIZE_2D : POINT_SIZE_3D,
    sizeUnits: 'pixels',
    opacity: userOpacity,
    material: false,
    pickable: false,
    parameters: { depthTest: !is2D },
    extensions: [dataFilterExt],
    filterRange: [frameIndex - 0.5, frameIndex + 0.5],
    updateTriggers: {
      getPosition: verticalExaggeration,
    },
  });
}

// ── Frame display ─────────────────────────────────────────────────────────────

function showFrame(index) {
  if (!mapRef || !deckOverlay) return;
  const layer = buildRadarLayer(index);
  deckOverlay.setProps({ layers: layer ? [layer] : [] });
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
  showFrame(currentIndex);
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

// ── Viewport change handler ───────────────────────────────────────────────────

let viewportDebounceTimer = null;

function onViewportChange() {
  if (viewportDebounceTimer) clearTimeout(viewportDebounceTimer);
  viewportDebounceTimer = setTimeout(() => {
    if (!mapRef) return;
    const zoom = Math.round(mapRef.getZoom());
    const z = Math.max(TILE_MIN_ZOOM, Math.min(TILE_MAX_ZOOM, zoom));
    if (z !== currentBulkZoom) {
      bulkFetchVoxels();
    }
  }, VIEWPORT_REFETCH_DEBOUNCE_MS);
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
    if (ok && timestamps.length > 0) {
      await bulkFetchVoxels();
      if (timestamps.length >= 2) {
        play();
      }
    }

    refreshIntervalId = setInterval(async () => {
      const wasPlaying = playing;
      if (wasPlaying) pause();
      currentBulkZoom = null;
      voxelLayerData = null;
      await fetchTimestamps();
      if (timestamps.length > 0) {
        await bulkFetchVoxels();
      }
      if (wasPlaying && timestamps.length >= 2) play();
    }, REFRESH_INTERVAL_MS);
  });

  map.on('moveend', onViewportChange);

  // ── Opacity slider ──────────────────────────────────────────────────────

  const opacitySlider = document.getElementById('opacity-slider');
  const opacityValue  = document.getElementById('opacity-value');

  opacitySlider.addEventListener('input', () => {
    userOpacity = parseFloat(opacitySlider.value);
    opacityValue.textContent = Math.round(userOpacity * 100) + '%';
    showFrame(currentIndex);
  });

  // ── Play / Pause ────────────────────────────────────────────────────────

  document.getElementById('btn-play').addEventListener('click', togglePlay);

  // ── Frame scrubber ──────────────────────────────────────────────────────

  const scrubber = document.getElementById('frame-scrubber');
  scrubber.addEventListener('input', () => {
    if (playing) pause();
    currentIndex = parseInt(scrubber.value, 10);
    updateFrameDisplay();
    showFrame(currentIndex);
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
    if (voxelLayerData && rawPositions) {
      applyVerticalExaggeration(voxelLayerData.attributes.getPosition.value, verticalExaggeration);
      showFrame(currentIndex);
    }
  });

  // ── Refresh button ──────────────────────────────────────────────────────

  document.getElementById('btn-refresh').addEventListener('click', async () => {
    const wasPlaying = playing;
    if (wasPlaying) pause();
    currentBulkZoom = null;
    voxelLayerData = null;
    await fetchTimestamps();
    if (timestamps.length > 0) {
      await bulkFetchVoxels();
    }
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
