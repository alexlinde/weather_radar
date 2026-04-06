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
 *   - 'volume': ray-marched volumetric rendering through the atlas cube
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

// Shared colorize: dBZ-proportional alpha with opacity-shaped curve.
// At high opacity the exponent is low (sqrt — broad visibility),
// at low opacity it steepens (quadratic — only strong echoes survive).
// Each mode applies the linear u_opacity scale separately (per-pixel
// for flat rendering, post-accumulation for volume ray marching).
const COLORIZE_GLSL = `
  uniform sampler2D u_colorRamp;
  uniform float u_opacity;
  uniform float u_dbzMin;
  uniform float u_dbzMax;

  vec4 colorize(float encoded) {
    if (encoded < 0.004) return vec4(0.0);
    float dbz = encoded * 127.5 - 30.0;
    if (dbz < u_dbzMin || dbz > u_dbzMax) return vec4(0.0);
    vec3 color = texture2D(u_colorRamp, vec2(encoded, 0.5)).rgb;
    if (dot(color, color) < 0.001) return vec4(0.0);
    float t = clamp((dbz - u_dbzMin) / (u_dbzMax - u_dbzMin), 0.0, 1.0);
    float alpha = pow(t, mix(2.0, 0.5, u_opacity));
    return vec4(color, alpha);
  }
`;

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
  uniform float u_timeMix;
  uniform int u_tiltIndex;

  varying vec2 v_uv;

  ${COLORIZE_GLSL}

  float sampleBand(sampler2D tex, vec2 uv, int band) {
    float bandF = float(band);
    float bandStart = bandF / 8.0;
    float halfTexel = 0.5 / 2048.0;
    float v = clamp((1.0 - uv.y) / 8.0 + bandStart, bandStart + halfTexel, bandStart + 0.125 - halfTexel);
    return texture2D(tex, vec2(uv.x, v)).r;
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
    vec4 col = colorize(encoded);
    if (col.a < 0.01) discard;
    col.a *= u_opacity;
    gl_FragColor = vec4(col.rgb * col.a, col.a);
  }
`;

// ── Volume shaders — single-box ray marching with tile atlas ────────────────

const VOLUME_VERT = `
  uniform vec3 u_cameraPos;
  varying vec3 v_origin;
  varying vec3 v_direction;
  void main() {
    v_origin = u_cameraPos;
    v_direction = position - u_cameraPos;
    gl_Position = projectionMatrix * vec4(position, 1.0);
  }
`;

const VOLUME_FRAG = `
  precision highp float;

  uniform sampler2D u_tex0;
  uniform sampler2D u_tex1;
  uniform float u_timeMix;
  uniform vec3  u_boxMin;
  uniform vec3  u_boxMax;
  uniform float u_steps;
  uniform float u_tileZoom;
  uniform vec2  u_tileOrigin;
  uniform vec2  u_tileCount;
  uniform float u_numAtlasCols;

  varying vec3 v_origin;
  varying vec3 v_direction;

  ${COLORIZE_GLSL}

  float hash12(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
  }

  float sampleAtlasBand(sampler2D tex, vec2 worldXY, float band) {
    float n = pow(2.0, u_tileZoom);
    vec2 tileCoord = worldXY * n;
    vec2 tileIdx = floor(tileCoord);
    vec2 localUV = fract(tileCoord);
    float col = (tileIdx.x - u_tileOrigin.x)
              + (tileIdx.y - u_tileOrigin.y) * u_tileCount.x;
    if (col < 0.0 || col >= u_numAtlasCols) return 0.0;

    float u = (col + localUV.x) / u_numAtlasCols;
    float bandStart = band / 8.0;
    float halfTexel = 0.5 / 2048.0;
    float v = clamp(localUV.y / 8.0 + bandStart,
                    bandStart + halfTexel, bandStart + 0.125 - halfTexel);
    return texture2D(tex, vec2(u, v)).r;
  }

  float sampleInterp(vec2 worldXY, float band) {
    return mix(sampleAtlasBand(u_tex0, worldXY, band),
               sampleAtlasBand(u_tex1, worldXY, band), u_timeMix);
  }

  float sampleVolume(vec3 worldPos) {
    vec3 boxSize = u_boxMax - u_boxMin;
    vec3 tc = (worldPos - u_boxMin) / boxSize;
    if (any(lessThan(tc, vec3(0.0))) || any(greaterThan(tc, vec3(1.0)))) return 0.0;
    float zSlice = tc.z * 7.0;
    float lower = floor(zSlice);
    float upper = min(7.0, lower + 1.0);
    return mix(sampleInterp(worldPos.xy, lower),
               sampleInterp(worldPos.xy, upper), fract(zSlice));
  }

  void main() {
    vec3 rayDir = normalize(v_direction);
    vec3 invDir = 1.0 / rayDir;
    vec3 t1 = (u_boxMin - v_origin) * invDir;
    vec3 t2 = (u_boxMax - v_origin) * invDir;
    vec3 tSmall = min(t1, t2);
    vec3 tBig   = max(t1, t2);
    float tNear = max(max(tSmall.x, tSmall.y), tSmall.z);
    float tFar  = min(min(tBig.x, tBig.y), tBig.z);
    tNear = max(tNear, 0.0);
    if (tNear >= tFar) discard;

    float rayLen  = tFar - tNear;
    float stepLen = rayLen / u_steps;
    vec3  marchStep = rayDir * stepLen;
    vec3  pos = v_origin + rayDir * tNear;
    pos += marchStep * hash12(gl_FragCoord.xy);

    vec4 acc = vec4(0.0);
    for (int i = 0; i < 64; i++) {
      if (float(i) >= u_steps) break;
      float val = sampleVolume(pos);
      vec4 col = colorize(val);
      if (col.a > 0.0) {
        col.a = min(1.0, col.a * 10.0 / u_steps);
        float f = col.a * (1.0 - acc.a);
        acc.rgb = (acc.a * acc.rgb + f * col.rgb) / max(acc.a + f, 0.001);
        acc.a  += f;
      }
      pos += marchStep;
      if (acc.a >= 0.99) break;
    }

    if (acc.a < 0.01) discard;
    acc.a *= u_opacity;
    gl_FragColor = vec4(acc.rgb * acc.a, acc.a);
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
      t.magFilter = THREE.LinearFilter;
      t.minFilter = THREE.LinearFilter;
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

    this._tileLoadGen = 0;
    this._tileCache = new TileTextureCache(() => {
      this._tileLoadGen++;
      this._requestRepaint();
    });
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

    this._volCanvasA = null;
    this._volCanvasB = null;
    this._volAtlasA = null;
    this._volAtlasB = null;
    this._volLayout = null;
    this._volLastKeyA = '';
    this._volLastKeyB = '';
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
    if (this._volAtlasA) { this._volAtlasA.dispose(); this._volAtlasA = null; }
    if (this._volAtlasB) { this._volAtlasB.dispose(); this._volAtlasB = null; }
    this._volCanvasA = null;
    this._volCanvasB = null;
    this._volLayout = null;
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

    if (this._mode === 'volume') {
      this._buildVolumeMesh();
    } else {
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

  _buildVolumeMesh() {
    const tiles = this._visibleTiles;
    if (tiles.length === 0) return;

    const z = tiles[0].z;
    const n = 2 ** z;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const t of tiles) {
      minX = Math.min(minX, t.x);
      maxX = Math.max(maxX, t.x);
      minY = Math.min(minY, t.y);
      maxY = Math.max(maxY, t.y);
    }
    const numX = maxX - minX + 1;
    const numY = maxY - minY + 1;
    const totalTiles = numX * numY;

    this._volLayout = { z, n, minX, minY, numX, numY, totalTiles };
    this._volLastKeyA = '';
    this._volLastKeyB = '';

    const aw = totalTiles * 256;
    const ah = 2048;
    const needResize = !this._volCanvasA
      || this._volCanvasA.width !== aw || this._volCanvasA.height !== ah;

    if (needResize) {
      this._volCanvasA = document.createElement('canvas');
      this._volCanvasA.width = aw;
      this._volCanvasA.height = ah;
      this._volCanvasB = document.createElement('canvas');
      this._volCanvasB.width = aw;
      this._volCanvasB.height = ah;

      if (this._volAtlasA) this._volAtlasA.dispose();
      if (this._volAtlasB) this._volAtlasB.dispose();

      this._volAtlasA = new THREE.CanvasTexture(this._volCanvasA);
      this._volAtlasA.flipY = false;
      this._volAtlasA.magFilter = THREE.LinearFilter;
      this._volAtlasA.minFilter = THREE.LinearFilter;
      this._volAtlasA.wrapS = THREE.ClampToEdgeWrapping;
      this._volAtlasA.wrapT = THREE.ClampToEdgeWrapping;
      this._volAtlasA.generateMipmaps = false;

      this._volAtlasB = new THREE.CanvasTexture(this._volCanvasB);
      this._volAtlasB.flipY = false;
      this._volAtlasB.magFilter = THREE.LinearFilter;
      this._volAtlasB.minFilter = THREE.LinearFilter;
      this._volAtlasB.wrapS = THREE.ClampToEdgeWrapping;
      this._volAtlasB.wrapT = THREE.ClampToEdgeWrapping;
      this._volAtlasB.generateMipmaps = false;
    }

    const mzMax = mercToAlt(TILT_HEIGHTS_M[RADAR_NUM_BANDS - 1] * this._vertExag);
    const mx0 = minX / n;
    const my0 = minY / n;
    const mx1 = (maxX + 1) / n;
    const my1 = (maxY + 1) / n;

    const geo = new THREE.BoxGeometry(mx1 - mx0, my1 - my0, mzMax);
    geo.translate((mx0 + mx1) / 2, (my0 + my1) / 2, mzMax / 2);

    const mat = new THREE.ShaderMaterial({
      vertexShader: VOLUME_VERT,
      fragmentShader: VOLUME_FRAG,
      transparent: true,
      depthTest: false,
      depthWrite: false,
      side: THREE.BackSide,
      blending: THREE.CustomBlending,
      blendSrc: THREE.OneFactor,
      blendDst: THREE.OneMinusSrcAlphaFactor,
      uniforms: {
        u_cameraPos:     { value: new THREE.Vector3() },
        u_tex0:          { value: this._volAtlasA },
        u_tex1:          { value: this._volAtlasB },
        u_colorRamp:     { value: this._colorRampTex },
        u_timeMix:       { value: 0 },
        u_opacity:       { value: this._opacity },
        u_dbzMin:        { value: this._dbzMin },
        u_dbzMax:        { value: this._dbzMax },
        u_boxMin:        { value: new THREE.Vector3(mx0, my0, 0) },
        u_boxMax:        { value: new THREE.Vector3(mx1, my1, mzMax) },
        u_steps:         { value: 48.0 },
        u_tileZoom:      { value: z },
        u_tileOrigin:    { value: new THREE.Vector2(minX, minY) },
        u_tileCount:     { value: new THREE.Vector2(numX, numY) },
        u_numAtlasCols:  { value: totalTiles },
      },
    });

    const mesh = new THREE.Mesh(geo, mat);
    mesh.matrixAutoUpdate = false;
    mesh.frustumCulled = false;
    mesh.userData = { isVolume: true };
    this._scene.add(mesh);
    this._tileMeshes.set('volume', mesh);
  }

  _updateMaterials() {
    if (!this._timestamps.length) return;
    const tsA = this._timestamps[this._frameA]?.timestamp;
    const tsB = this._timestamps[this._frameB]?.timestamp;
    if (!tsA) return;

    if (this._mode === 'volume') {
      this._updateVolumeMaterial(tsA, tsB);
      return;
    }

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

  _updateVolumeMaterial(tsA, tsB) {
    const volMesh = this._tileMeshes.get('volume');
    if (!volMesh || !this._volLayout) return;

    const u = volMesh.material.uniforms;

    const cam = this._extractCameraPosition();
    if (cam) u.u_cameraPos.value.copy(cam);

    u.u_opacity.value = this._opacity;
    u.u_dbzMin.value = this._dbzMin;
    u.u_dbzMax.value = this._dbzMax;
    u.u_timeMix.value = (tsB && tsB !== tsA) ? this._timeMix : 0;

    const gen = this._tileLoadGen;
    const keyA = `${tsA}|${gen}`;
    if (this._volLastKeyA !== keyA) {
      const any = this._drawVolAtlas(this._volCanvasA, tsA);
      this._volAtlasA.needsUpdate = true;
      this._volLastKeyA = keyA;
      volMesh.visible = any;
    }

    if (tsB && tsB !== tsA) {
      const keyB = `${tsB}|${gen}`;
      if (this._volLastKeyB !== keyB) {
        this._drawVolAtlas(this._volCanvasB, tsB);
        this._volAtlasB.needsUpdate = true;
        this._volLastKeyB = keyB;
      }
    }

    if (volMesh.visible && this._staleMeshes.length > 0) {
      this._purgeStale();
    }
  }

  _drawVolAtlas(canvas, timestamp) {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const layout = this._volLayout;
    let anyDrawn = false;
    for (const { z, x, y } of this._visibleTiles) {
      const tex = this._tileCache.get(timestamp, z, x, y);
      if (!tex?.image) continue;
      const col = (x - layout.minX) + (y - layout.minY) * layout.numX;
      ctx.drawImage(tex.image, col * 256, 0);
      anyDrawn = true;
    }
    return anyDrawn;
  }

  _extractCameraPosition() {
    if (!this._currentMatrix) return null;
    const m = this._currentMatrix;
    // Solve for the eye point: the world-space point where w_clip = 0.
    // Use rows 0, 1, 3 of the MVP matrix: M * [cx, cy, cz, 1]^T maps to
    // (0, 0, *, 0) in clip space — cx/cy give screen-center, w=0 is at-eye.
    // System: A * [cx, cy, cz]^T = b
    const a00 = m[0], a01 = m[4], a02 = m[8],  b0 = -m[12];
    const a10 = m[1], a11 = m[5], a12 = m[9],  b1 = -m[13];
    const a20 = m[3], a21 = m[7], a22 = m[11], b2 = -m[15];
    const det = a00*(a11*a22 - a12*a21) - a01*(a10*a22 - a12*a20) + a02*(a10*a21 - a11*a20);
    if (Math.abs(det) < 1e-20) return null;
    const invDet = 1 / det;
    const cx = ((b0*(a11*a22-a12*a21) - a01*(b1*a22-a12*b2) + a02*(b1*a21-a11*b2))) * invDet;
    const cy = ((a00*(b1*a22-a12*b2) - b0*(a10*a22-a12*a20) + a02*(a10*b2-b1*a20))) * invDet;
    const cz = ((a00*(a11*b2-b1*a21) - a01*(a10*b2-b1*a20) + b0*(a10*a21-a11*a20))) * invDet;
    return new THREE.Vector3(cx, cy, cz);
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
