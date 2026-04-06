/**
 * RadarLayer — three.js overlay renderer for atlas radar tiles.
 *
 * Renders to a separate canvas using three.js, positioned over the MapLibre
 * map. Camera projection is synced from MapLibre's render callback matrix.
 *
 * Tile meshes use world-space Mercator vertices (no per-mesh transforms)
 * so that shared tile edges produce bit-identical clip-space positions,
 * preventing seams.
 *
 * Modes:
 *   - 'composite': shader takes fmax across 8 bands
 *   - '3d': 8 stacked planes per tile, one per tilt altitude
 */

const RADAR_TILE_MIN_ZOOM = 3;
const RADAR_TILE_MAX_ZOOM = 8;
const RADAR_NUM_BANDS = 8;
const MAX_TEXTURES = 300;

const TILT_HEIGHTS_M = [1000, 2000, 3500, 5000, 7000, 9000, 12000, 15000];

function mercToAlt(altMeters) {
  return altMeters / 40075016.686;
}

// ── GLSL Shaders ────────────────────────────────────────────────────────────

const RADAR_VERT = `
  varying vec2 v_uv;
  void main() {
    v_uv = uv;
    gl_Position = projectionMatrix * vec4(position, 1.0);
  }
`;

const RADAR_FRAG = `
  precision highp float;

  uniform sampler2D u_tex0;
  uniform sampler2D u_tex1;
  uniform sampler2D u_colorRamp;
  uniform float u_timeMix;
  uniform float u_opacity;
  uniform int u_tiltIndex;
  uniform float u_dbzMin;
  uniform float u_dbzMax;

  varying vec2 v_uv;

  float sampleBand(sampler2D tex, vec2 uv, int band) {
    float bandF = float(band);
    vec2 atlasUV = vec2(uv.x, (1.0 - uv.y) / 8.0 + bandF / 8.0);
    return texture2D(tex, atlasUV).r;
  }

  float sampleInterp(vec2 uv, int band) {
    return mix(sampleBand(u_tex0, uv, band), sampleBand(u_tex1, uv, band), u_timeMix);
  }

  float getComposite(vec2 uv) {
    float maxVal = 0.0;
    for (int i = 0; i < 8; i++) {
      maxVal = max(maxVal, sampleInterp(uv, i));
    }
    return maxVal;
  }

  void main() {
    float encoded = u_tiltIndex < 0 ? getComposite(v_uv) : sampleInterp(v_uv, u_tiltIndex);

    if (encoded < 0.004) discard;
    float dbz = encoded * 127.5 - 30.0;
    if (dbz < u_dbzMin || dbz > u_dbzMax) discard;

    vec4 color = texture2D(u_colorRamp, vec2(encoded, 0.5));
    if (color.a < 0.01) discard;

    color.a *= u_opacity;
    gl_FragColor = vec4(color.rgb * color.a, color.a);
  }
`;

// ── Tile texture cache ──────────────────────────────────────────────────────

class TileTextureCache {
  constructor(onLoad) {
    this._textures = new Map();
    this._loading = new Map();
    this._onLoad = onLoad;
  }

  _key(ts, z, x, y) { return `${ts}/${z}/${x}/${y}`; }

  get(ts, z, x, y) {
    const e = this._textures.get(this._key(ts, z, x, y));
    if (e) { e.lu = performance.now(); return e.t; }
    return null;
  }

  async load(ts, z, x, y) {
    const k = this._key(ts, z, x, y);
    if (this._textures.has(k)) return this._textures.get(k).t;
    if (this._loading.has(k)) return this._loading.get(k);
    const p = this._doLoad(k, ts, z, x, y);
    this._loading.set(k, p);
    try { return await p; } finally { this._loading.delete(k); }
  }

  async _doLoad(k, ts, z, x, y) {
    const url = `/api/radar/atlas/${encodeURIComponent(ts)}/${z}/${x}/${y}.png`;
    try {
      const r = await fetch(url, { cache: 'force-cache' });
      if (!r.ok) return null;
      const bmp = await createImageBitmap(await r.blob());
      const t = new THREE.Texture(bmp);
      t.magFilter = THREE.NearestFilter;
      t.minFilter = THREE.NearestFilter;
      t.wrapS = THREE.ClampToEdgeWrapping;
      t.wrapT = THREE.ClampToEdgeWrapping;
      t.generateMipmaps = false;
      t.needsUpdate = true;
      this._textures.set(k, { t, lu: performance.now() });
      this._evict();
      this._onLoad?.();
      return t;
    } catch (e) {
      if (e.name !== 'AbortError') console.warn('Tile load failed:', k, e);
      return null;
    }
  }

  _evict() {
    if (this._textures.size <= MAX_TEXTURES) return;
    const arr = [...this._textures.entries()].sort((a, b) => a[1].lu - b[1].lu);
    for (const [k, e] of arr.slice(0, arr.length - MAX_TEXTURES)) {
      e.t.dispose();
      this._textures.delete(k);
    }
  }

  clear() {
    for (const [, e] of this._textures) e.t.dispose();
    this._textures.clear();
  }
}

// ── Color ramp texture ──────────────────────────────────────────────────────

function buildColorRampTexture() {
  const data = createColorRampData();
  const tex = new THREE.DataTexture(data, 256, 1, THREE.RGBAFormat);
  tex.magFilter = THREE.LinearFilter;
  tex.minFilter = THREE.LinearFilter;
  tex.wrapS = THREE.ClampToEdgeWrapping;
  tex.needsUpdate = true;
  return tex;
}

// ── Build world-space tile geometry ─────────────────────────────────────────

function buildTileGeometry(z, x, y, mz) {
  const n = 2 ** z;
  const mx0 = x / n;
  const mx1 = (x + 1) / n;
  const my0 = y / n;
  const my1 = (y + 1) / n;

  const positions = new Float32Array([
    mx0, my0, mz,
    mx1, my0, mz,
    mx0, my1, mz,
    mx1, my1, mz,
  ]);
  const uvs = new Float32Array([
    0, 1,
    1, 1,
    0, 0,
    1, 0,
  ]);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geo.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
  geo.setIndex([0, 2, 1, 1, 2, 3]);
  return geo;
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
    this._dbzMin = -30.0;
    this._dbzMax = 100.0;

    this._frameA = 0;
    this._frameB = 0;
    this._timeMix = 0;
    this._timestamps = [];

    this._tileCache = new TileTextureCache(() => this._requestRepaint());
    this._map = null;
    this._visibleTiles = [];

    this._renderer = null;
    this._scene = null;
    this._camera = null;
    this._colorRampTex = null;
    this._tileMeshes = new Map();
    this._staleMeshes = [];
    this._stalePurgeTimer = null;
    this._dummyTex = null;
    this._currentMatrix = null;
  }

  // ── Public API ──────────────────────────────────────────────────────────

  setTimestamps(ts) { this._timestamps = ts; }

  setAnimation(frameA, frameB, mix) {
    this._frameA = frameA;
    this._frameB = frameB;
    this._timeMix = mix;
    this._requestRepaint();
  }

  setMode(mode) {
    this._mode = mode;
    this._rebuildMeshes();
    this._requestRepaint();
  }

  setOpacity(val) {
    this._opacity = val;
    this._requestRepaint();
  }

  setVerticalExaggeration(val) {
    this._vertExag = val;
    this._rebuildMeshes();
    this._requestRepaint();
  }

  setDbzRange(min, max) {
    this._dbzMin = min;
    this._dbzMax = max;
    this._requestRepaint();
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
    this._rebuildMeshes();
  }

  async ensureTextures(frameA, frameB) {
    if (!this._timestamps.length) return;
    const tsA = this._timestamps[frameA]?.timestamp;
    const tsB = this._timestamps[frameB]?.timestamp;
    if (!tsA) return;
    const promises = [];
    for (const { z, x, y } of this._visibleTiles) {
      promises.push(this._tileCache.load(tsA, z, x, y));
      if (tsB && tsB !== tsA) promises.push(this._tileCache.load(tsB, z, x, y));
    }
    await Promise.all(promises);
  }

  prefetchFrame(frameIdx) {
    if (frameIdx < 0 || frameIdx >= this._timestamps.length) return;
    const ts = this._timestamps[frameIdx]?.timestamp;
    if (!ts) return;
    for (const { z, x, y } of this._visibleTiles) this._tileCache.load(ts, z, x, y);
  }

  // ── CustomLayerInterface ───────────────────────────────────────────────

  onAdd(map, _gl) {
    this._map = map;
    this._initThree(map);
    map.on('move', () => this._requestRepaint());
    map.on('resize', () => this._resizeRenderer());
  }

  render(_gl, matrix) {
    if (!this._renderer || !this._timestamps.length) return;
    this._currentMatrix = matrix;

    // Sync camera: set projectionMatrix from MapLibre's matrix.
    // modelViewMatrix is identity because meshes use world-space vertices.
    this._camera.projectionMatrix.fromArray(new Float32Array(matrix));
    this._camera.projectionMatrixInverse.copy(this._camera.projectionMatrix).invert();

    this._updateMaterials();
    this._renderer.resetState();
    this._renderer.render(this._scene, this._camera);
  }

  onRemove() {
    this._tileCache.clear();
    this._clearMeshes();
    if (this._renderer) {
      this._renderer.dispose();
      this._renderer.domElement.remove();
    }
  }

  // ── three.js setup ────────────────────────────────────────────────────

  _initThree(map) {
    const container = map.getCanvasContainer();

    this._renderer = new THREE.WebGLRenderer({
      alpha: true,
      premultipliedAlpha: true,
      antialias: false,
      stencil: false,
      depth: false,
    });
    const canvas = this._renderer.domElement;
    canvas.style.position = 'absolute';
    canvas.style.top = '0';
    canvas.style.left = '0';
    canvas.style.pointerEvents = 'none';
    container.appendChild(canvas);

    this._resizeRenderer();

    this._scene = new THREE.Scene();

    // Camera with identity view — projection set per frame from MapLibre
    this._camera = new THREE.Camera();
    this._camera.matrixWorldInverse.identity();
    this._camera.matrixWorldNeedsUpdate = false;

    this._colorRampTex = buildColorRampTexture();
    this._dummyTex = new THREE.DataTexture(new Uint8Array(4), 1, 1, THREE.RGBAFormat);
    this._dummyTex.needsUpdate = true;
  }

  _resizeRenderer() {
    if (!this._renderer || !this._map) return;
    const mc = this._map.getCanvas();
    this._renderer.setSize(mc.width, mc.height, false);
    this._renderer.domElement.style.width = mc.style.width;
    this._renderer.domElement.style.height = mc.style.height;
  }

  // ── Mesh management ────────────────────────────────────────────────────

  _rebuildMeshes() {
    if (!this._scene) return;

    for (const [, mesh] of this._tileMeshes) {
      this._staleMeshes.push(mesh);
    }
    this._tileMeshes.clear();

    for (const { z, x, y } of this._visibleTiles) {
      if (this._mode === 'composite') {
        this._addTileMesh(z, x, y, 0, -1);
      } else {
        for (let band = 0; band < RADAR_NUM_BANDS; band++) {
          const mz = mercToAlt(TILT_HEIGHTS_M[band] * this._vertExag);
          this._addTileMesh(z, x, y, mz, band);
        }
      }
    }

    clearTimeout(this._stalePurgeTimer);
    this._stalePurgeTimer = setTimeout(() => this._purgeStale(), 3000);
  }

  _addTileMesh(z, x, y, mz, tiltIndex) {
    const geo = buildTileGeometry(z, x, y, mz);
    const mat = new THREE.ShaderMaterial({
      vertexShader: RADAR_VERT,
      fragmentShader: RADAR_FRAG,
      transparent: true,
      depthTest: false,
      depthWrite: false,
      blending: THREE.CustomBlending,
      blendSrc: THREE.OneFactor,
      blendDst: THREE.OneMinusSrcAlphaFactor,
      uniforms: {
        u_tex0: { value: this._dummyTex },
        u_tex1: { value: this._dummyTex },
        u_colorRamp: { value: this._colorRampTex },
        u_timeMix: { value: 0 },
        u_opacity: { value: this._opacity },
        u_tiltIndex: { value: tiltIndex },
        u_dbzMin: { value: this._dbzMin },
        u_dbzMax: { value: this._dbzMax },
      },
    });

    const mesh = new THREE.Mesh(geo, mat);
    mesh.matrixAutoUpdate = false;
    mesh.frustumCulled = false;
    mesh.userData = { z, x, y };
    this._scene.add(mesh);
    this._tileMeshes.set(`${z}/${x}/${y}/${tiltIndex}`, mesh);
  }

  _updateMaterials() {
    if (!this._timestamps.length) return;
    const tsA = this._timestamps[this._frameA]?.timestamp;
    const tsB = this._timestamps[this._frameB]?.timestamp;
    if (!tsA) return;

    let anyTextured = false;
    for (const [, mesh] of this._tileMeshes) {
      const { z, x, y } = mesh.userData;
      const texA = this._tileCache.get(tsA, z, x, y);
      const u = mesh.material.uniforms;

      if (texA) {
        anyTextured = true;
        const texB = (tsB && tsB !== tsA) ? this._tileCache.get(tsB, z, x, y) : null;
        u.u_tex0.value = texA;
        u.u_tex1.value = texB || texA;
        u.u_timeMix.value = texB ? this._timeMix : 0;
        mesh.visible = true;
      } else if (u.u_tex0.value === this._dummyTex) {
        mesh.visible = false;
      }

      u.u_opacity.value = this._opacity;
      u.u_dbzMin.value = this._dbzMin;
      u.u_dbzMax.value = this._dbzMax;
    }

    if (anyTextured && this._staleMeshes.length > 0) {
      this._purgeStale();
    }
  }

  _purgeStale() {
    for (const mesh of this._staleMeshes) {
      mesh.geometry.dispose();
      mesh.material.dispose();
      this._scene?.remove(mesh);
    }
    this._staleMeshes.length = 0;
    clearTimeout(this._stalePurgeTimer);
  }

  _clearMeshes() {
    this._purgeStale();
    for (const [, mesh] of this._tileMeshes) {
      mesh.geometry.dispose();
      mesh.material.dispose();
      this._scene?.remove(mesh);
    }
    this._tileMeshes.clear();
  }

  _requestRepaint() {
    if (this._map) this._map.triggerRepaint();
  }
}
