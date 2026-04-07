/**
 * RadarEngine — framework-independent radar animation and data engine.
 *
 * Manages timestamps, animation loop, frame interpolation, presets, and
 * auto-refresh. Has zero DOM dependencies — communicates state changes
 * via EventTarget so any UI layer (full, embed, or RN bridge) can subscribe.
 */

import { RadarLayer } from './radar-layer.js';

export const RADAR_PRESETS = {
  all:    { dbzMin: 5,  dbzMax: 75, intensity: 0.8 },
  precip: { dbzMin: 15, dbzMax: 75, intensity: 0.85 },
  severe: { dbzMin: 40, dbzMax: 75, intensity: 0.95 },
  custom: null,
};

const MAX_503_RETRIES = 24;
const REFRESH_INTERVAL_MS = 120_000;
const VIEWPORT_DEBOUNCE_MS = 300;
const PREFETCH_AHEAD = 5;
const PREFETCH_BURST = 10;

export function formatTimestamp(iso) {
  if (!iso) return '--:--';
  return new Date(iso).toLocaleTimeString('en-US', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatTimestampFull(iso) {
  if (!iso) return '--';
  return new Date(iso).toLocaleString('en-US', {
    hour12: false,
  });
}

export class RadarEngine extends EventTarget {
  constructor({ apiBase = '' } = {}) {
    super();
    this._apiBase = apiBase;

    this.timestamps = [];
    this.currentAnimationTime = 0;
    this.playing = false;
    this.frameInterval = 500;
    this.viewMode = 'composite';
    this.activePreset = 'all';
    this.opacity = 0.8;
    this.verticalExaggeration = 1.0;
    this.dbzMin = 5;
    this.dbzMax = 75;

    this._radarLayer = null;
    this._map = null;
    this._animationId = null;
    this._lastAnimTime = 0;
    this._fetchAbort = null;
    this._timestampFetchRetries = 0;
    this._refreshIntervalId = null;
    this._viewportDebounceTimer = null;
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────

  initLayer(map) {
    this._map = map;
    this._radarLayer = new RadarLayer();
    map.addLayer(this._radarLayer);
    if (this.viewMode !== 'composite') {
      this._radarLayer.setMode(this.viewMode);
    }
    return this._radarLayer;
  }

  async start() {
    const ok = await this.fetchTimestamps();
    if (ok && this.timestamps.length > 0) {
      this._radarLayer.updateVisibleTiles();
      await this.loadAndShowFrame();
      if (this.timestamps.length >= 2) this.play();
    }
    this.startAutoRefresh();
  }

  startAutoRefresh() {
    this.stopAutoRefresh();
    this._refreshIntervalId = setInterval(() => this.refresh(), REFRESH_INTERVAL_MS);
  }

  stopAutoRefresh() {
    if (this._refreshIntervalId) {
      clearInterval(this._refreshIntervalId);
      this._refreshIntervalId = null;
    }
  }

  // ── Timestamps ─────────────────────────────────────────────────────────

  async fetchTimestamps() {
    if (this._fetchAbort) this._fetchAbort.abort();
    this._fetchAbort = new AbortController();

    this._emit('status', { state: 'loading', message: 'Loading timestamps…' });
    try {
      const resp = await fetch(
        `${this._apiBase}/api/radar/timestamps`,
        { cache: 'no-store', signal: this._fetchAbort.signal },
      );
      if (resp.status === 503) {
        this._timestampFetchRetries++;
        if (this._timestampFetchRetries < MAX_503_RETRIES) {
          this._emit('status', { state: 'loading', message: 'Server seeding cache…' });
          setTimeout(() => this.fetchTimestamps(), 5_000);
        } else {
          this._emit('status', { state: 'error', message: 'Server unavailable — try refreshing' });
        }
        return false;
      }
      this._timestampFetchRetries = 0;
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();

      this.timestamps = data.timestamps;
      this.currentAnimationTime = this.timestamps.length - 1;

      if (this._radarLayer) {
        this._radarLayer.setTimestamps(this.timestamps);
        if (data.motion && this.timestamps.length > 0) {
          const b = this.timestamps[0].bounds;
          this._radarLayer.setMotionConfig(b, data.motion.max_disp_deg);
        }
      }

      this._emitFrameUpdate();

      const newest = this.timestamps[this.timestamps.length - 1];
      this._emit('status', {
        state: '',
        message: `Latest: ${formatTimestampFull(newest?.timestamp)} ET`,
      });
      this._emit('timestamps', { count: this.timestamps.length, timestamps: this.timestamps });

      return true;
    } catch (err) {
      if (err.name === 'AbortError') return false;
      console.error('Timestamp fetch error:', err);
      this._emit('status', { state: 'error', message: `Error: ${err.message}` });
      return false;
    }
  }

  // ── Frame display ──────────────────────────────────────────────────────

  getCurrentFrameIndex() {
    return Math.floor(this.currentAnimationTime) % Math.max(1, this.timestamps.length);
  }

  showFrame() {
    if (!this._radarLayer || this.timestamps.length === 0) return;

    const len = this.timestamps.length;
    const t = ((this.currentAnimationTime % len) + len) % len;
    const frameA = Math.floor(t);
    const frameB = (frameA + 1) % len;
    const mix = t - frameA;

    this._radarLayer.setAnimation(frameA, frameB, mix);
    this._radarLayer.prefetchFrames((frameB + 1) % len, PREFETCH_AHEAD);
    this._radarLayer.prefetchMotion(frameA, PREFETCH_AHEAD);
  }

  async loadAndShowFrame() {
    if (!this._radarLayer || this.timestamps.length === 0) return;

    const len = this.timestamps.length;
    const t = ((this.currentAnimationTime % len) + len) % len;
    const frameA = Math.floor(t);
    const frameB = (frameA + 1) % len;

    await Promise.all([
      this._radarLayer.ensureTextures(frameA, frameB),
      this._radarLayer.ensureMotion(frameA),
    ]);
    this.showFrame();
  }

  // ── Animation ──────────────────────────────────────────────────────────

  play() {
    if (this.timestamps.length < 2) return;
    this.playing = true;
    this._lastAnimTime = 0;

    if (this._radarLayer) {
      const len = this.timestamps.length;
      const startFrame = Math.floor(((this.currentAnimationTime % len) + len) % len);
      this._radarLayer.prefetchFrames(startFrame, PREFETCH_BURST);
    }

    this._animationId = requestAnimationFrame(now => this._animationTick(now));
    this._emit('playstate', { playing: true });
  }

  pause() {
    this.playing = false;
    this._lastAnimTime = 0;
    if (this._animationId) {
      cancelAnimationFrame(this._animationId);
      this._animationId = null;
    }
    this.showFrame();
    this._emit('playstate', { playing: false });
  }

  togglePlay() {
    this.playing ? this.pause() : this.play();
  }

  setFrameIndex(idx) {
    if (this.playing) this.pause();
    this.currentAnimationTime = idx;
    this._emitFrameUpdate();
    this.showFrame();
    this.loadAndShowFrame();
  }

  setSpeed(ms) {
    this.frameInterval = ms;
  }

  _animationTick(now) {
    if (!this.playing) return;

    if (this._lastAnimTime > 0 && this._radarLayer) {
      const dt = now - this._lastAnimTime;
      const step = dt / this.frameInterval;
      const len = this.timestamps.length;

      let nextTime = this.currentAnimationTime + step;
      if (nextTime >= len) nextTime -= len;

      const nextFrameA = Math.floor(nextTime) % len;
      const nextFrameB = (nextFrameA + 1) % len;

      if (this._radarLayer.hasTexturesForFrame(nextFrameA) &&
          this._radarLayer.hasTexturesForFrame(nextFrameB)) {
        this.currentAnimationTime = nextTime;
        this.showFrame();
        this._emitFrameUpdate();
      } else {
        this._radarLayer.prefetchFrames(nextFrameA, PREFETCH_AHEAD);
      }
    }
    this._lastAnimTime = now;
    this._animationId = requestAnimationFrame(now2 => this._animationTick(now2));
  }

  // ── Settings ───────────────────────────────────────────────────────────

  setViewMode(mode) {
    if (mode === this.viewMode) return;
    this.viewMode = mode;
    if (this._radarLayer) this._radarLayer.setMode(mode);
    this.showFrame();
    this._emit('viewmode', { mode });
  }

  setOpacity(val) {
    this.opacity = val;
    if (this._radarLayer) this._radarLayer.setOpacity(val);
  }

  setVerticalExaggeration(val) {
    this.verticalExaggeration = val;
    if (this._radarLayer) this._radarLayer.setVerticalExaggeration(val);
  }

  setDbzRange(min, max) {
    if (min > max) [min, max] = [max, min];
    this.dbzMin = min;
    this.dbzMax = max;
    if (this._radarLayer) this._radarLayer.setDbzRange(min, max);
  }

  switchPreset(key) {
    this.activePreset = key;
    const preset = RADAR_PRESETS[key];
    if (preset) {
      this.setDbzRange(preset.dbzMin, preset.dbzMax);
      this.setOpacity(preset.intensity);
      this.opacity = preset.intensity;
    }
    this._emit('preset', { preset: key, values: preset });
  }

  // ── Viewport ───────────────────────────────────────────────────────────

  onViewportChange() {
    if (this._viewportDebounceTimer) clearTimeout(this._viewportDebounceTimer);
    this._viewportDebounceTimer = setTimeout(async () => {
      if (!this._radarLayer || !this._map) return;
      this._radarLayer.updateVisibleTiles();
      await this.loadAndShowFrame();
    }, VIEWPORT_DEBOUNCE_MS);
  }

  // ── Refresh ────────────────────────────────────────────────────────────

  async refresh() {
    const wasPlaying = this.playing;
    if (wasPlaying) this.pause();
    await this.fetchTimestamps();
    if (this.timestamps.length > 0 && this._radarLayer) {
      this._radarLayer.updateVisibleTiles();
      await this.loadAndShowFrame();
    }
    if (wasPlaying && this.timestamps.length >= 2) this.play();
  }

  // ── Events ─────────────────────────────────────────────────────────────

  _emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }

  _emitFrameUpdate() {
    const idx = this.getCurrentFrameIndex();
    const ts = this.timestamps[idx];
    this._emit('frame', {
      index: idx,
      total: this.timestamps.length,
      timestamp: ts?.timestamp || null,
      formattedTime: formatTimestamp(ts?.timestamp),
    });
  }
}
