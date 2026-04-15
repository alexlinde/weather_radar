/**
 * Weather Radar — application entry point.
 *
 * Initializes the map, creates a RadarEngine, and wires the UI.
 *
 * Layout is fully responsive — adapts to mobile/desktop via CSS media queries.
 * On small screens the control panel pops up from the bottom animation toolbar
 * via a hamburger button; on desktop it's always visible in the top-right corner.
 *
 * Two special behaviors are detected automatically:
 *   - WebView context (window.ReactNativeWebView or window !== parent):
 *     Starts the postMessage bridge for RN / iframe communication.
 *   - ?controls= parameter selects UI chrome level:
 *       full (default) — all controls visible
 *       minimal — animation bar only
 *       none — no UI chrome; host app controls via postMessage
 */

import { buildLegend } from './colors.js';
import { RadarEngine, formatTimestampFull } from './radar-engine.js';
import { RadarBridge } from './radar-bridge.js';

const API_BASE = '';

function getControlsMode() {
  const val = new URLSearchParams(window.location.search).get('controls');
  if (val === 'none') return 'none';
  if (val === 'minimal') return 'minimal';
  return 'full';
}

function isEmbeddedContext() {
  return !!(window.ReactNativeWebView || window.parent !== window);
}

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

// ── UI wiring ─────────────────────────────────────────────────────────────────

function wireUI(engine, map, { minimal, embedded, controlsMode }) {
  // ── Status ──────────────────────────────────────────────────────────────
  const statusDot = document.getElementById('status-dot');
  const statusText = document.getElementById('timestamp');
  engine.addEventListener('status', e => {
    statusDot.className = e.detail.state;
    statusText.textContent = e.detail.message || '';
  });

  // ── Frame display ───────────────────────────────────────────────────────
  const scrubber = document.getElementById('frame-scrubber');
  const frameTime = document.getElementById('frame-time');
  const frameCounter = document.getElementById('frame-counter');

  engine.addEventListener('frame', e => {
    scrubber.value = e.detail.index;
    frameTime.textContent = e.detail.formattedTime;
    frameCounter.textContent = e.detail.total > 0
      ? `${e.detail.index + 1}/${e.detail.total}`
      : '0/0';
  });

  engine.addEventListener('timestamps', () => {
    scrubber.max = Math.max(0, engine.timestamps.length - 1);
    scrubber.value = engine.getCurrentFrameIndex();
  });

  // ── Play/Pause ──────────────────────────────────────────────────────────
  const iconPlay = document.getElementById('icon-play');
  const iconPause = document.getElementById('icon-pause');
  document.getElementById('btn-play').addEventListener('click', () => engine.togglePlay());
  engine.addEventListener('playstate', e => {
    iconPlay.style.display = e.detail.playing ? 'none' : '';
    iconPause.style.display = e.detail.playing ? '' : 'none';
  });

  // ── Frame scrubber ──────────────────────────────────────────────────────
  scrubber.addEventListener('input', () => {
    engine.setFrameIndex(parseInt(scrubber.value, 10));
  });

  // ── Speed ───────────────────────────────────────────────────────────────
  document.getElementById('speed-select').addEventListener('change', e => {
    engine.setSpeed(parseInt(e.target.value, 10));
  });

  // ── Intensity ───────────────────────────────────────────────────────────
  const opacitySlider = document.getElementById('opacity-slider');
  const opacityValue = document.getElementById('opacity-value');
  opacitySlider.addEventListener('input', () => {
    const val = parseFloat(opacitySlider.value);
    engine.setOpacity(val);
    opacityValue.textContent = Math.round(val * 100) + '%';
    if (engine.activePreset !== 'custom') engine.switchPreset('custom');
  });

  // ── View mode ───────────────────────────────────────────────────────────
  const viewModeBtns = document.querySelectorAll('#view-mode-seg .seg-btn');
  viewModeBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      engine.setViewMode(btn.dataset.mode);
    });
  });

  const rowExag = document.getElementById('row-exaggeration');
  engine.addEventListener('viewmode', e => {
    viewModeBtns.forEach(btn => {
      btn.classList.toggle('active', btn.dataset.mode === e.detail.mode);
    });
    if (e.detail.mode === 'composite') {
      map.easeTo({ pitch: 0, bearing: 0, duration: 600 });
      rowExag.style.display = 'none';
    } else {
      map.easeTo({ pitch: 50, duration: 600 });
      rowExag.style.display = '';
    }
  });

  // ── Vertical exaggeration ──────────────────────────────────────────────
  const exagSlider = document.getElementById('exag-slider');
  const exagValue = document.getElementById('exag-value');
  exagSlider.addEventListener('input', () => {
    const val = parseFloat(exagSlider.value);
    engine.setVerticalExaggeration(val);
    exagValue.textContent = `${val}x`;
  });

  // ── dBZ range ──────────────────────────────────────────────────────────
  const dbzMinSlider = document.getElementById('dbz-min-slider');
  const dbzMaxSlider = document.getElementById('dbz-max-slider');
  const dbzRangeValue = document.getElementById('dbz-range-value');

  function updateDbzRange() {
    let lo = parseInt(dbzMinSlider.value, 10);
    let hi = parseInt(dbzMaxSlider.value, 10);
    if (lo > hi) [lo, hi] = [hi, lo];
    dbzRangeValue.textContent = `${lo} – ${hi}`;
    engine.setDbzRange(lo, hi);
  }
  dbzMinSlider.addEventListener('input', updateDbzRange);
  dbzMaxSlider.addEventListener('input', updateDbzRange);

  // ── Presets ────────────────────────────────────────────────────────────
  const presetSeg = document.getElementById('preset-seg');
  const dbzCutoffRow = document.getElementById('row-dbz-cutoff');

  presetSeg.querySelectorAll('.seg-btn').forEach(btn => {
    btn.addEventListener('click', () => engine.switchPreset(btn.dataset.preset));
  });

  engine.addEventListener('preset', e => {
    presetSeg.querySelectorAll('.seg-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.preset === e.detail.preset);
    });
    dbzCutoffRow.style.display = e.detail.preset === 'custom' ? '' : 'none';

    const vals = e.detail.values;
    if (vals) {
      dbzMinSlider.value = vals.dbzMin;
      dbzMaxSlider.value = vals.dbzMax;
      dbzRangeValue.textContent = `${vals.dbzMin} – ${vals.dbzMax}`;
      opacitySlider.value = vals.intensity;
      opacityValue.textContent = Math.round(vals.intensity * 100) + '%';
    }
  });

  // ── Refresh / Reset ───────────────────────────────────────────────────
  document.getElementById('btn-refresh').addEventListener('click', () => engine.refresh());

  document.getElementById('btn-reset-view').addEventListener('click', () => {
    const pitch = (engine.viewMode === '3d' || engine.viewMode === 'volume') ? 50 : 0;
    map.flyTo({ center: [-98.5, 39.8], zoom: 4, pitch, bearing: 0, duration: 800 });
  });

  // ── Legend ─────────────────────────────────────────────────────────────
  const legendEl = document.getElementById('legend');
  const legendToggle = document.getElementById('btn-legend');
  buildLegend(legendEl);
  legendToggle.addEventListener('click', () => {
    legendEl.classList.toggle('visible');
    legendToggle.textContent = legendEl.classList.contains('visible') ? 'Hide legend' : 'Show legend';
  });

  // ── Mobile controls toggle (hamburger inside animation bar) ────────────
  const mobileToggle = document.getElementById('btn-mobile-toggle');
  const controlsPanel = document.getElementById('controls');
  mobileToggle.addEventListener('click', (e) => {
    e.stopPropagation();
    controlsPanel.classList.toggle('mobile-open');
  });

  // Close the popup when tapping the map
  map.on('click', () => {
    controlsPanel.classList.remove('mobile-open');
  });

  // ── Hash sync (skip in embedded contexts — host controls position) ────
  if (!embedded) {
    map.on('moveend', () => updateHash(map));
    map.on('zoomend', () => updateHash(map));
    map.on('pitchend', () => {
      updateHash(map);
      if (map.getPitch() > 0 && engine.viewMode === 'composite') {
        engine.setViewMode('3d');
      }
    });
    map.on('rotateend', () => updateHash(map));
    updateHash(map);
  }

  // ── Expand button (visible only in embedded contexts with some UI) ───
  const expandBtn = document.getElementById('btn-expand');
  if (embedded && controlsMode !== 'none') {
    expandBtn.style.display = 'flex';
  }

  return { expandBtn };
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  const controlsMode = getControlsMode();
  const minimal = controlsMode !== 'full';
  const embedded = isEmbeddedContext();
  const engine = new RadarEngine({ apiBase: API_BASE });

  if (controlsMode === 'minimal') {
    document.body.classList.add('controls-minimal');
  } else if (controlsMode === 'none') {
    document.body.classList.add('controls-none');
  }

  const styleUrl = await resolveMapStyle();

  const saved = !embedded ? parseHash() : null;
  if (saved && saved.pitch > 0) engine.viewMode = '3d';

  const map = new maplibregl.Map({
    container: 'map',
    style: styleUrl,
    center: saved?.center || [-98.5, 39.8],
    zoom: saved?.zoom ?? 4,
    pitch: saved?.pitch ?? 0,
    bearing: saved?.bearing ?? 0,
    attributionControl: !minimal,
  });

  if (!minimal) {
    map.addControl(new maplibregl.NavigationControl(), 'top-left');
    map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');
  }

  // Sync view mode buttons with restored state
  document.querySelectorAll('#view-mode-seg .seg-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === engine.viewMode);
  });
  if (engine.viewMode === '3d') {
    document.getElementById('row-exaggeration').style.display = '';
  }

  const { expandBtn } = wireUI(engine, map, { minimal, embedded, controlsMode });

  // Start the postMessage bridge when inside a WebView or iframe
  const bridge = new RadarBridge(engine, map);
  if (embedded) {
    bridge.start();
    expandBtn.addEventListener('click', () => bridge.requestFullScreen());
  }

  map.on('load', async () => {
    engine.initLayer(map);
    await engine.start();
  });

  map.on('moveend', () => engine.onViewportChange());
}

init();
