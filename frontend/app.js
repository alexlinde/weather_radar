/**
 * NYC Weather Radar — Main application.
 *
 * Initialises a MapLibre GL map, fetches the latest MRMS radar image from the
 * backend API, and renders it as a georeferenced raster overlay.
 *
 * Auto-refreshes every 2 minutes. Manual refresh via button.
 */

const API_BASE = 'http://127.0.0.1:8000';
const REFRESH_INTERVAL_MS = 120_000; // 2 minutes

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
  // Fallback: OpenFreeMap (no key needed)
  return 'https://tiles.openfreemap.org/styles/liberty';
}

// ── Radar overlay ─────────────────────────────────────────────────────────────

const SOURCE_ID = 'radar-image';
const LAYER_ID  = 'radar-layer';

let currentTimestamp = null;
let isLoading = false;

function setStatus(state, message) {
  const dot = document.getElementById('status-dot');
  const ts  = document.getElementById('timestamp');
  dot.className = state; // '', 'loading', 'error'
  ts.textContent = message || '';
}

async function fetchAndUpdateRadar(map) {
  if (isLoading) return;
  isLoading = true;
  setStatus('loading', 'Fetching…');

  try {
    const resp = await fetch(`${API_BASE}/api/radar/image`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const north     = parseFloat(resp.headers.get('X-Radar-North'));
    const south     = parseFloat(resp.headers.get('X-Radar-South'));
    const west      = parseFloat(resp.headers.get('X-Radar-West'));
    const east      = parseFloat(resp.headers.get('X-Radar-East'));
    const timestamp = resp.headers.get('X-Radar-Timestamp');

    const blob = await resp.blob();
    const imageUrl = URL.createObjectURL(blob);

    // MapLibre image source expects: top-left (NW), top-right (NE), bottom-right (SE), bottom-left (SW)
    const coordinates = [
      [west,  north],  // NW — top-left
      [east,  north],  // NE — top-right
      [east,  south],  // SE — bottom-right
      [west,  south],  // SW — bottom-left
    ];

    if (map.getSource(SOURCE_ID)) {
      map.getSource(SOURCE_ID).updateImage({ url: imageUrl, coordinates });
    } else {
      map.addSource(SOURCE_ID, {
        type: 'image',
        url: imageUrl,
        coordinates,
      });
      map.addLayer({
        id: LAYER_ID,
        type: 'raster',
        source: SOURCE_ID,
        paint: {
          'raster-opacity': parseFloat(document.getElementById('opacity-slider').value),
          'raster-fade-duration': 0,
          'raster-resampling': 'linear',
        },
      });
    }

    // Draw / update bounding box outline around the radar coverage area
    const bboxGeoJSON = {
      type: 'Feature',
      geometry: {
        type: 'Polygon',
        coordinates: [[
          [west,  south],
          [east,  south],
          [east,  north],
          [west,  north],
          [west,  south],
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

    // Revoke the old object URL after the image has loaded
    map.once('idle', () => URL.revokeObjectURL(imageUrl));

    currentTimestamp = timestamp;
    const formatted = timestamp
      ? new Date(timestamp).toLocaleString('en-US', { timeZone: 'America/New_York', hour12: false })
      : '—';
    setStatus('', `Valid: ${formatted} ET`);

  } catch (err) {
    console.error('Radar fetch error:', err);
    setStatus('error', `Error: ${err.message}`);
  } finally {
    isLoading = false;
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

  map.addControl(new maplibregl.NavigationControl(), 'top-left');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');

  map.on('load', async () => {
    await fetchAndUpdateRadar(map);

    // Auto-refresh
    setInterval(() => fetchAndUpdateRadar(map), REFRESH_INTERVAL_MS);
  });

  // ── Controls ────────────────────────────────────────────────────────────────

  const opacitySlider = document.getElementById('opacity-slider');
  const opacityValue  = document.getElementById('opacity-value');

  opacitySlider.addEventListener('input', () => {
    const val = parseFloat(opacitySlider.value);
    opacityValue.textContent = Math.round(val * 100) + '%';
    if (map.getLayer(LAYER_ID)) {
      map.setPaintProperty(LAYER_ID, 'raster-opacity', val);
    }
  });

  document.getElementById('btn-refresh').addEventListener('click', () => {
    fetchAndUpdateRadar(map);
  });

  document.getElementById('btn-reset-view').addEventListener('click', () => {
    map.flyTo({ center: [-73.98, 40.75], zoom: 10, pitch: 0, bearing: 0, duration: 800 });
  });

  // ── Legend ──────────────────────────────────────────────────────────────────

  const legendEl    = document.getElementById('legend');
  const legendToggle = document.getElementById('btn-legend');

  buildLegend(legendEl);

  legendToggle.addEventListener('click', () => {
    legendEl.classList.toggle('visible');
    legendToggle.textContent = legendEl.classList.contains('visible') ? 'Hide legend' : 'Show legend';
  });
}

init();
