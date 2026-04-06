/**
 * RadarLayer — MapLibre CustomLayerInterface for rendering atlas radar tiles.
 *
 * Uses WebGL to render 256×2048 grayscale PNG atlas tiles (8 tilt bands)
 * with GPU-side dBZ decoding, NWS color ramp lookup, temporal interpolation,
 * and spatial smoothing.
 *
 * Modes:
 *   - 'composite': single ground-level plane per tile, shader takes fmax across 8 bands
 *   - '3d': 8 stacked planes per tile, one per tilt altitude
 */

const RADAR_TILE_MIN_ZOOM = 3;
const RADAR_TILE_MAX_ZOOM = 8;
const RADAR_NUM_BANDS = 8;
const MAX_TEXTURES = 300;

const TILT_HEIGHTS_M = [1000, 2000, 3500, 5000, 7000, 9000, 12000, 15000];

// ── GLSL Shaders ────────────────────────────────────────────────────────────

const VERT_SHADER = `
  precision highp float;
  uniform mat4 u_matrix;
  attribute vec3 a_pos;
  attribute vec2 a_uv;
  varying vec2 v_uv;

  void main() {
    gl_Position = u_matrix * vec4(a_pos, 1.0);
    v_uv = a_uv;
  }
`;

const FRAG_SHADER = `
  precision highp float;

  uniform sampler2D u_tex0;
  uniform sampler2D u_tex1;
  uniform sampler2D u_colorRamp;
  uniform float u_timeMix;
  uniform float u_opacity;
  uniform int u_tiltIndex;     // 0-7 for single band, -1 for composite (fmax)
  uniform float u_smoothRadius; // spatial smoothing radius in UV units
  uniform float u_dbzMin;
  uniform float u_dbzMax;

  varying vec2 v_uv;

  float sampleBand(sampler2D tex, vec2 uv, int band) {
    float bandF = float(band);
    vec2 atlasUV = vec2(uv.x, uv.y / 8.0 + bandF / 8.0);
    return texture2D(tex, atlasUV).r;
  }

  float sampleInterp(vec2 uv, int band) {
    float a = sampleBand(u_tex0, uv, band);
    float b = sampleBand(u_tex1, uv, band);
    return mix(a, b, u_timeMix);
  }

  float getComposite(vec2 uv) {
    float maxVal = 0.0;
    for (int i = 0; i < 8; i++) {
      float val = sampleInterp(uv, i);
      maxVal = max(maxVal, val);
    }
    return maxVal;
  }

  float smoothSample(vec2 uv) {
    float raw;
    if (u_tiltIndex < 0) {
      raw = getComposite(uv);
    } else {
      raw = sampleInterp(uv, u_tiltIndex);
    }

    if (u_smoothRadius <= 0.0) return raw;

    // Circular 8-sample spatial smoothing.
    // Offsets are clamped to [0,1] UV so CLAMP_TO_EDGE handles tile edges
    // naturally — no hard cutoff that would create a visible seam.
    float sum = raw;
    float count = 1.0;
    float r = u_smoothRadius;
    for (int i = 0; i < 8; i++) {
      float angle = float(i) * 0.7854; // PI/4
      vec2 sampleUV = clamp(uv + vec2(cos(angle), sin(angle)) * r, 0.0, 1.0);
      float s;
      if (u_tiltIndex < 0) {
        s = getComposite(sampleUV);
      } else {
        s = sampleInterp(sampleUV, u_tiltIndex);
      }
      float w = 0.5;
      sum += s * w;
      count += w;
    }
    return sum / count;
  }

  void main() {
    float encoded = smoothSample(v_uv);

    if (encoded < 0.004) { // ~1/255
      discard;
    }

    // dBZ decode: pixel_byte = encoded * 255, dBZ = pixel_byte / 2 - 30
    float dbz = encoded * 127.5 - 30.0;

    if (dbz < u_dbzMin || dbz > u_dbzMax) {
      discard;
    }

    vec4 color = texture2D(u_colorRamp, vec2(encoded, 0.5));

    if (color.a < 0.01) {
      discard;
    }

    color.a *= u_opacity;
    gl_FragColor = color;
  }
`;

// ── Tile texture manager ────────────────────────────────────────────────────

class TileTextureCache {
  constructor() {
    this._textures = new Map(); // key -> { texture, lastUsed }
    this._loading = new Map();  // key -> Promise
    this._gl = null;
  }

  init(gl) {
    this._gl = gl;
  }

  _key(timestamp, z, x, y) {
    return `${timestamp}/${z}/${x}/${y}`;
  }

  get(timestamp, z, x, y) {
    const key = this._key(timestamp, z, x, y);
    const entry = this._textures.get(key);
    if (entry) {
      entry.lastUsed = performance.now();
      return entry.texture;
    }
    return null;
  }

  async load(timestamp, z, x, y) {
    const key = this._key(timestamp, z, x, y);
    if (this._textures.has(key)) return this._textures.get(key).texture;
    if (this._loading.has(key)) return this._loading.get(key);

    const promise = this._doLoad(key, timestamp, z, x, y);
    this._loading.set(key, promise);
    try {
      const tex = await promise;
      return tex;
    } finally {
      this._loading.delete(key);
    }
  }

  async _doLoad(key, timestamp, z, x, y) {
    const gl = this._gl;
    if (!gl) return null;

    const url = `/api/radar/atlas/${encodeURIComponent(timestamp)}/${z}/${x}/${y}.png`;
    try {
      const resp = await fetch(url, { cache: 'force-cache' });
      if (!resp.ok) return null;
      const blob = await resp.blob();
      const bmp = await createImageBitmap(blob);

      const texture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, gl.LUMINANCE, gl.UNSIGNED_BYTE, bmp);
      bmp.close();

      this._textures.set(key, { texture, lastUsed: performance.now() });
      this._evict();
      return texture;
    } catch (err) {
      if (err.name !== 'AbortError') console.warn('Tile load failed:', key, err);
      return null;
    }
  }

  _evict() {
    if (this._textures.size <= MAX_TEXTURES) return;
    const entries = [...this._textures.entries()].sort((a, b) => a[1].lastUsed - b[1].lastUsed);
    const toRemove = entries.slice(0, entries.length - MAX_TEXTURES);
    for (const [key, entry] of toRemove) {
      if (this._gl) this._gl.deleteTexture(entry.texture);
      this._textures.delete(key);
    }
  }

  clear() {
    for (const [, entry] of this._textures) {
      if (this._gl) this._gl.deleteTexture(entry.texture);
    }
    this._textures.clear();
  }
}

// ── Radar layer ─────────────────────────────────────────────────────────────

class RadarLayer {
  constructor() {
    this.id = 'radar-atlas';
    this.type = 'custom';
    this.renderingMode = '3d';

    this._mode = 'composite';
    this._opacity = 0.8;
    this._vertExag = 3.0;
    this._smoothRadius = 0.003;
    this._dbzMin = -30.0;
    this._dbzMax = 100.0;

    this._frameA = 0;
    this._frameB = 0;
    this._timeMix = 0;
    this._timestamps = [];

    this._program = null;
    this._locations = {};
    this._colorRampTex = null;
    this._tileCache = new TileTextureCache();
    this._quadBuffer = null;

    this._map = null;
    this._gl = null;
    this._visibleTiles = [];
    this._tileQuads = new Map(); // "z/x/y" -> { buffer, vertCount }
  }

  // ── Public API ──────────────────────────────────────────────────────────

  setTimestamps(ts) {
    this._timestamps = ts;
  }

  setAnimation(frameA, frameB, mix) {
    this._frameA = frameA;
    this._frameB = frameB;
    this._timeMix = mix;
    if (this._map) this._map.triggerRepaint();
  }

  setMode(mode) {
    this._mode = mode;
    this._rebuildQuads();
    if (this._map) this._map.triggerRepaint();
  }

  setOpacity(val) {
    this._opacity = val;
    if (this._map) this._map.triggerRepaint();
  }

  setVerticalExaggeration(val) {
    this._vertExag = val;
    this._rebuildQuads();
    if (this._map) this._map.triggerRepaint();
  }

  setSmoothRadius(val) {
    this._smoothRadius = val;
    if (this._map) this._map.triggerRepaint();
  }

  setDbzRange(min, max) {
    this._dbzMin = min;
    this._dbzMax = max;
    if (this._map) this._map.triggerRepaint();
  }

  updateVisibleTiles() {
    if (!this._map) return;
    const bounds = this._map.getBounds();
    const zoom = Math.round(this._map.getZoom());
    const z = Math.max(RADAR_TILE_MIN_ZOOM, Math.min(RADAR_TILE_MAX_ZOOM, zoom));
    const n = 2 ** z;
    const xMin = Math.max(0, Math.floor((bounds.getWest() + 180) / 360 * n));
    const xMax = Math.min(n - 1, Math.floor((bounds.getEast() + 180) / 360 * n));
    const latToY = lat => {
      const rad = lat * Math.PI / 180;
      return Math.floor((1 - Math.log(Math.tan(rad) + 1 / Math.cos(rad)) / Math.PI) / 2 * n);
    };
    const yMin = Math.max(0, latToY(bounds.getNorth()));
    const yMax = Math.min(n - 1, latToY(bounds.getSouth()));
    const tiles = [];
    for (let x = xMin; x <= xMax; x++) {
      for (let y = yMin; y <= yMax; y++) {
        tiles.push({ z, x, y });
      }
    }
    this._visibleTiles = tiles;
    this._rebuildQuads();
  }

  async ensureTextures(frameA, frameB) {
    if (!this._timestamps.length) return;
    const tsA = this._timestamps[frameA]?.timestamp;
    const tsB = this._timestamps[frameB]?.timestamp;
    if (!tsA) return;

    const promises = [];
    for (const { z, x, y } of this._visibleTiles) {
      promises.push(this._tileCache.load(tsA, z, x, y));
      if (tsB && tsB !== tsA) {
        promises.push(this._tileCache.load(tsB, z, x, y));
      }
    }
    await Promise.all(promises);
    if (this._map) this._map.triggerRepaint();
  }

  prefetchFrame(frameIdx) {
    if (frameIdx < 0 || frameIdx >= this._timestamps.length) return;
    const ts = this._timestamps[frameIdx]?.timestamp;
    if (!ts) return;
    for (const { z, x, y } of this._visibleTiles) {
      this._tileCache.load(ts, z, x, y);
    }
  }

  // ── CustomLayerInterface ────────────────────────────────────────────────

  onAdd(map, gl) {
    this._map = map;
    this._gl = gl;
    this._tileCache.init(gl);

    this._program = this._createProgram(gl, VERT_SHADER, FRAG_SHADER);
    const p = this._program;
    this._locations = {
      aPos:         gl.getAttribLocation(p, 'a_pos'),
      aUv:          gl.getAttribLocation(p, 'a_uv'),
      uMatrix:      gl.getUniformLocation(p, 'u_matrix'),
      uTex0:        gl.getUniformLocation(p, 'u_tex0'),
      uTex1:        gl.getUniformLocation(p, 'u_tex1'),
      uColorRamp:   gl.getUniformLocation(p, 'u_colorRamp'),
      uTimeMix:     gl.getUniformLocation(p, 'u_timeMix'),
      uOpacity:     gl.getUniformLocation(p, 'u_opacity'),
      uTiltIndex:   gl.getUniformLocation(p, 'u_tiltIndex'),
      uSmoothRadius: gl.getUniformLocation(p, 'u_smoothRadius'),
      uDbzMin:      gl.getUniformLocation(p, 'u_dbzMin'),
      uDbzMax:      gl.getUniformLocation(p, 'u_dbzMax'),
    };

    this._buildColorRamp(gl);
  }

  onRemove(_map, gl) {
    this._tileCache.clear();
    if (this._program) gl.deleteProgram(this._program);
    if (this._colorRampTex) gl.deleteTexture(this._colorRampTex);
    for (const [, quad] of this._tileQuads) {
      gl.deleteBuffer(quad.buffer);
    }
    this._tileQuads.clear();
  }

  render(gl, matrix) {
    if (!this._program || !this._timestamps.length) return;

    const tsA = this._timestamps[this._frameA]?.timestamp;
    const tsB = this._timestamps[this._frameB]?.timestamp;
    if (!tsA) return;

    gl.useProgram(this._program);
    gl.uniformMatrix4fv(this._locations.uMatrix, false, matrix);
    gl.uniform1f(this._locations.uTimeMix, this._timeMix);
    gl.uniform1f(this._locations.uOpacity, this._opacity);
    gl.uniform1f(this._locations.uSmoothRadius, this._smoothRadius);
    gl.uniform1f(this._locations.uDbzMin, this._dbzMin);
    gl.uniform1f(this._locations.uDbzMax, this._dbzMax);

    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, this._colorRampTex);
    gl.uniform1i(this._locations.uColorRamp, 2);

    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    if (this._mode === '3d') {
      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LEQUAL);
    } else {
      gl.disable(gl.DEPTH_TEST);
    }

    // Stencil: each pixel is drawn by exactly one tile.
    // With the small epsilon overlap on quad geometry, the stencil
    // prevents double-blending at tile boundaries.
    gl.enable(gl.STENCIL_TEST);
    gl.clearStencil(0);
    gl.clear(gl.STENCIL_BUFFER_BIT);
    gl.stencilMask(0xFF);
    gl.stencilFunc(gl.EQUAL, 0, 0xFF);
    gl.stencilOp(gl.KEEP, gl.KEEP, gl.INCR);

    for (const { z, x, y } of this._visibleTiles) {
      const texA = this._tileCache.get(tsA, z, x, y);
      if (!texA) continue;
      const texB = (tsB && tsB !== tsA) ? this._tileCache.get(tsB, z, x, y) : texA;

      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texA);
      gl.uniform1i(this._locations.uTex0, 0);

      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, texB || texA);
      gl.uniform1i(this._locations.uTex1, 1);

      if (this._mode === 'composite') {
        this._drawTileQuad(gl, z, x, y, -1, 0);
      } else {
        for (let band = 0; band < RADAR_NUM_BANDS; band++) {
          this._drawTileQuad(gl, z, x, y, band, TILT_HEIGHTS_M[band] * this._vertExag);
        }
      }
    }

    gl.disable(gl.STENCIL_TEST);
    gl.disable(gl.BLEND);
    gl.disable(gl.DEPTH_TEST);
  }

  // ── Internal helpers ────────────────────────────────────────────────────

  _drawTileQuad(gl, z, x, y, tiltIndex, altitudeM) {
    const key = `${z}/${x}/${y}/${tiltIndex}`;
    let quad = this._tileQuads.get(key);

    if (!quad) {
      quad = this._buildQuad(gl, z, x, y, altitudeM);
      this._tileQuads.set(key, quad);
    }

    gl.uniform1i(this._locations.uTiltIndex, tiltIndex);

    gl.bindBuffer(gl.ARRAY_BUFFER, quad.buffer);

    gl.enableVertexAttribArray(this._locations.aPos);
    gl.vertexAttribPointer(this._locations.aPos, 3, gl.FLOAT, false, 20, 0);

    gl.enableVertexAttribArray(this._locations.aUv);
    gl.vertexAttribPointer(this._locations.aUv, 2, gl.FLOAT, false, 20, 12);

    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  _buildQuad(gl, z, x, y, altitudeM) {
    // Compute in Mercator space with a sub-pixel overlap (~0.5px) to
    // prevent GPU rasterization gaps at tile boundaries.
    const n = 2 ** z;
    const eps = 1 / n / 512;
    const mx0 = x / n - eps;
    const mx1 = (x + 1) / n + eps;
    const my0 = y / n - eps;
    const my1 = (y + 1) / n + eps;

    // Altitude: convert meters to Mercator z units using scale at tile center
    let mz = 0;
    if (altitudeM > 0) {
      const centerLat = Math.atan(Math.sinh(Math.PI * (1 - (my0 + my1)))) * 180 / Math.PI;
      const ref = maplibregl.MercatorCoordinate.fromLngLat([0, centerLat], altitudeM);
      mz = ref.z;
    }

    // Triangle strip: NW, SW, NE, SE
    // Each vertex: x, y, z, u, v (5 floats, stride=20 bytes)
    const data = new Float32Array([
      mx0, my0, mz, 0, 0,
      mx0, my1, mz, 0, 1,
      mx1, my0, mz, 1, 0,
      mx1, my1, mz, 1, 1,
    ]);

    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);

    return { buffer, vertCount: 4 };
  }

  _rebuildQuads() {
    if (!this._gl) return;
    const gl = this._gl;
    for (const [, quad] of this._tileQuads) {
      gl.deleteBuffer(quad.buffer);
    }
    this._tileQuads.clear();
  }

  _buildColorRamp(gl) {
    const data = createColorRampData();
    this._colorRampTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this._colorRampTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, data);
  }

  _createProgram(gl, vsrc, fsrc) {
    const vs = gl.createShader(gl.VERTEX_SHADER);
    gl.shaderSource(vs, vsrc);
    gl.compileShader(vs);
    if (!gl.getShaderParameter(vs, gl.COMPILE_STATUS)) {
      console.error('Vertex shader error:', gl.getShaderInfoLog(vs));
    }

    const fs = gl.createShader(gl.FRAGMENT_SHADER);
    gl.shaderSource(fs, fsrc);
    gl.compileShader(fs);
    if (!gl.getShaderParameter(fs, gl.COMPILE_STATUS)) {
      console.error('Fragment shader error:', gl.getShaderInfoLog(fs));
    }

    const program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error('Program link error:', gl.getProgramInfoLog(program));
    }

    gl.deleteShader(vs);
    gl.deleteShader(fs);
    return program;
  }
}
