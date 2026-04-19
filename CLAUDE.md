# CLAUDE.md — Weather Radar Prototype

## What we're building

A web-based weather radar viewer that pulls live MRMS (Multi-Radar Multi-Sensor) data from NOAA's public AWS bucket and renders it over a map of the continental US (CONUS). The goal is to show real radar data the way a meteorologist would see it — full reflectivity at multiple vertical levels, with smooth animation — on top of a base map that gives geographic context (terrain, water, urban areas).

This is a working prototype, not a production app. Prioritise getting real data on screen over polish.

## Current status

All planned phases are complete. The frontend has been refactored into ES modules with a responsive layout (mobile + desktop) and automatic WebView/iframe detection for React Native integration.

The app fetches tilt-level reflectivity data across 8 vertical levels, stores them as sparse matrices (scipy.sparse CSR). The backend serves 256×2048 grayscale PNG atlas tiles — 8 tilt levels stacked vertically — via a standard TMS endpoint, plus per-frame-pair motion vector PNGs computed via FFT block matching. The frontend uses a three.js overlay renderer synced to MapLibre's projection matrix, with GLSL shaders for GPU-side dBZ decoding, NWS color ramp lookup, motion-compensated semi-Lagrangian advection between frames, and spatial smoothing. Three view modes use the same atlas tile data: Composite (fmax across bands on one ground plane), 3D (8 stacked planes at tilt altitudes), and Volume (ray-marched volumetric rendering). Animation uses continuous float time with motion-compensated interpolation so storms slide smoothly between 2-minute keyframes. The map uses MapLibre GL JS starting at a CONUS-wide view. 3D terrain is currently disabled pending a tile seam fix.

The frontend is structured as ES modules with a clean separation between rendering (`radar-layer.js`), engine logic (`radar-engine.js`), and UI wiring (`app.js`). The layout is fully responsive — on desktop the control panel sits top-right; on mobile (≤600px) a hamburger button in the animation bar opens the control panel as a popup above the toolbar. WebView/iframe context is auto-detected (`window.ReactNativeWebView` or `window.parent !== window`), which activates the postMessage bridge (`radar-bridge.js`) and shows an expand button — no special URL parameter needed. A `?controls=` parameter selects the UI chrome level: `full` (default, all controls), `minimal` (animation bar only), or `none` (no UI chrome — host app controls everything via postMessage). An esbuild pipeline bundles and minifies the JS/CSS for production deployment. See `INTEGRATION.md` for the React Native integration spec.

## Architecture

```
┌──────────────────┐      ┌───────────────────────┐      ┌──────────────────────┐
│  NOAA S3 Bucket  │─────▶│  Python Backend       │─────▶│  Browser Frontend    │
│  noaa-mrms-pds   │      │  (FastAPI)            │      │  (MapLibre + WebGL)  │
│  Public, no auth │      │  Fetch, decode, atlas │      │  Map + radar overlay │
└──────────────────┘      └───────────────────────┘      └──────────────────────┘
```

### Backend (Python / FastAPI)

Responsibilities:
- Fetch MRMS tilt-level GRIB2 files from S3 across 8 elevation angles
- Decode with custom minimal GRIB2 decoder (no eccodes dependency)
- Convert to scipy.sparse CSR matrices (95-99% of grid is NaN/sentinel → 20x memory reduction)
- Serve atlas tiles (`/api/radar/atlas/{timestamp}/{z}/{x}/{y}.png`) — 256×2048 grayscale PNG with 8 tilt bands, dBZ encoded as uint8
- Compute motion fields between consecutive frames via FFT block matching (`motion.py`) and serve as RGB PNG (`/api/radar/motion/{timestamp}.png`)
- Two-tier caching: ConusTiltCache (sparse LRU, ~30 entries, ~1.2 GB) + atlas tile LRU (2000 entries)
- GZip middleware for API responses
- Background seeding on startup (60 frames across all tilts) + atlas tile pre-rendering + motion field computation for all consecutive pairs

### Frontend (ES modules, esbuild)

The frontend is vanilla JS structured as ES modules, bundled for production by esbuild. MapLibre and three.js are loaded from CDN (not bundled). No framework, no JSX.

**Module structure:**
- `radar-layer.js` — three.js overlay renderer (`RadarLayer` as MapLibre `CustomLayerInterface`), all GLSL shaders, tile/motion texture caches, mesh management. Imports only `createColorRampData` from `colors.js`. This is the heaviest module (~1060 lines) and has zero DOM dependencies.
- `radar-engine.js` — `RadarEngine` class (extends `EventTarget`): animation state machine, timestamp fetching with 503 retry, frame interpolation, preset logic, auto-refresh timer. Zero DOM dependencies — communicates via CustomEvent dispatch.
- `radar-bridge.js` — `RadarBridge` class: postMessage bridge for React Native WebView / iframe communication. Translates inbound commands to engine calls, forwards engine events outbound. Auto-started when an embedded context is detected.
- `colors.js` — NWS color scale data, GPU ramp texture builder, DOM legend builder.
- `app.js` — entry point. Initializes the map, creates RadarEngine, wires the responsive UI. Auto-detects embedded context to start the bridge and show the expand button.

**Responsive layout:**
- Desktop (>600px): control panel top-right, animation bar bottom-center, legend toggle bottom-right, MapLibre nav controls.
- Mobile (≤600px): animation bar stretches full-width with a square play/pause button and a hamburger button on the right end. The hamburger opens the control panel as a popup floating above the toolbar. Frame time hidden to save space. Tapping the map dismisses the popup.
- `?controls=minimal`: hides all chrome except the animation bar. No nav controls, no attribution.
- `?controls=none`: hides all UI chrome including the animation bar. The host app controls everything via postMessage. Expand button is also hidden.

**Embedded context detection:**
- Auto-detected via `window.ReactNativeWebView` (RN WebView) or `window.parent !== window` (iframe).
- When embedded: postMessage bridge starts, expand button appears (sends `requestFullScreen` to host), URL hash sync is disabled.

**Rendering responsibilities:**
- Render base map with MapLibre GL JS (Stadia/MapTiler/OpenFreeMap cascade)
- 3D terrain via AWS elevation tiles (currently disabled — tile seam issue)
- three.js overlay renderer with camera projection synced from MapLibre's render callback matrix
- GLSL shaders for GPU-side dBZ decoding, NWS color ramp lookup, motion-compensated semi-Lagrangian advection, spatial smoothing, tile edge blending
- Motion texture cache loads per-frame-pair CONUS-wide motion vectors; shader converts tile UV → geographic coordinates → motion texture UV, traces pixels backward/forward along displacement field, blends with crossfade weighted by confidence
- Composite mode: single ground-level quad per tile, shader takes fmax across 8 tilt bands
- 3D mode: 8 stacked quads per tile at tilt altitudes, one band per quad
- Volume mode: single box mesh per viewport, ray-marched through a stitched tile atlas texture with trilinear interpolation between tilt levels
- Continuous float animation time with motion-compensated interpolation between keyframes
- Viewport-based tile loading with browser HTTP cache (`force-cache` + `immutable`)

**Build pipeline:**
- `frontend/package.json` — esbuild as sole dev dependency
- `npm run build` — bundles `app.js` (+ all imports) into `frontend/dist/radar.min.js` (~37 KB), minifies CSS into `style.min.css`, copies `index.html` with rewritten script/link tags
- `npm run watch` — rebuild on save during development
- In dev without a build step, browsers load the ES modules directly via `<script type="module">`
- FastAPI serves from `frontend/dist/` if it exists, otherwise falls back to `frontend/` source

## Data Source: MRMS on AWS

**Bucket:** `noaa-mrms-pds` (public, no credentials needed)
**Region:** `us-east-1`
**Docs:** https://registry.opendata.aws/noaa-mrms-pds/

### Data path (what we actually use)

We fetch **tilt-level reflectivity** (`MergedReflectivityQC`) as the single data source, not the pre-computed composite. This gives us both 2D and 3D from one fetch path.

```
s3://noaa-mrms-pds/CONUS/MergedReflectivityQC_{tilt}/{YYYYMMDD}/MRMS_...grib2.gz
```

**Important:** The bucket prefix is `CONUS/` (uppercase), and files are organized by date subdirectory.

We fetch 8 tilt levels per timestamp:
```
00.50°, 01.00°, 01.50°, 02.50°, 04.00°, 07.00°, 10.00°, 19.00°
```

These are mapped to approximate physical heights for 3D rendering:
```python
TILT_TO_HEIGHT_KM = {
 "00.50": 1.0, "01.00": 1.5, "01.50": 2.0, "02.50": 3.5,
 "04.00": 5.5, "07.00": 9.0, "10.00": 12.0, "19.00": 19.0,
}
```

The 2D composite is derived via `np.fmax` across all tilt grids at each grid point — equivalent to the pre-computed `MergedReflectivityQCComposite` product, but we get 3D for free.

### File naming convention
```
MRMS_MergedReflectivityQC_{tilt}_{YYYYMMDD}-{HHmmSS}.grib2.gz
```

### How to list available files

The pipeline scans today + yesterday + 2 days ago to span UTC day boundaries:

```python
import boto3
from botocore import UNSIGNED
from botocore.config import Config

s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))
paginator = s3.get_paginator('list_objects_v2')

prefix = f"CONUS/MergedReflectivityQC_00.50/{date_str}/"
for page in paginator.paginate(Bucket='noaa-mrms-pds', Prefix=prefix):
    for obj in page.get('Contents', []):
        print(obj['Key'])
```

## Base Map

**MapLibre GL JS** with cascading tile source selection:

1. **Stadia Maps — Stamen Terrain** (if `STADIA_API_KEY` is set)
2. **MapTiler Outdoor** (if `MAPTILER_API_KEY` is set)
3. **OpenFreeMap Liberty** (zero-config fallback, no key needed)

The frontend fetches tile config from `GET /api/config` at startup and falls back gracefully. Store API keys in `.env` (see `.env.example`).

3D terrain is currently disabled (commented out in `app.js`) due to a tile seam issue with the radar overlay. When re-enabled, it uses AWS Terrain Tiles:
```javascript
map.addSource('terrain', {
    type: 'raster-dem',
    tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
    encoding: 'terrarium',
    tileSize: 256,
    maxzoom: 15,
});
map.setTerrain({ source: 'terrain', exaggeration: 1.5 });
```

### Map initial view
```javascript
center: [-98.5, 39.8],    // Center of CONUS
zoom: 4,                   // Shows full continental US
pitch: 0,                  // Flat in composite mode; 50° in 3D mode
bearing: 0
```

## Tech Stack

### Backend
- **Python 3.11+**
- **FastAPI** — API server
- **numpy** — array operations
- **scipy** — sparse matrix storage (CSR format, 20x memory reduction for MRMS data)
- **boto3** — S3 access (unsigned requests, no credentials needed)
- **Pillow** — JPEG2000/PNG decoding for GRIB2 packing templates (ships with openjpeg)
- **uvicorn** — ASGI server
- **python-dotenv** — load `.env` for API keys

No eccodes, no cfgrib, no system-level C dependencies.

### Frontend
- **MapLibre GL JS** (~v4) — base map (3D terrain disabled pending seam fix)
- **three.js** (~v0.160) — overlay WebGL renderer, synced to MapLibre's projection matrix
- **GLSL shaders** via `THREE.ShaderMaterial` — dBZ decoding, color ramp, motion advection, volume ray marching
- **Vanilla JS** — ES modules, no framework
- **esbuild** — bundler + minifier (dev dependency only)

MapLibre and three.js loaded from CDN (not bundled):
```html
<script src="https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.css" rel="stylesheet" />
<script src="https://unpkg.com/three@0.160.0/build/three.min.js"></script>
```

Application JS loaded as ES module:
```html
<script type="module" src="app.js"></script>
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/radar/atlas/{timestamp}/{z}/{x}/{y}.png` | GET | Atlas tile — 256×2048 grayscale PNG, 8 tilt bands. z=3–8. |
| `/api/radar/motion/{timestamp}.png` | GET | Motion vector field — RGB PNG (U, V, confidence). Keyed by source frame. |
| `/api/radar/timestamps` | GET | Available timestamps with bounds, `has_motion` flags, and motion config. |
| `/api/radar/refresh` | GET | Force cache invalidation + re-seed. |
| `/api/config` | GET | Frontend configuration (map tile API keys). |
| `/health` | GET | Cache status (in-memory + on-disk counts). |

The `/timestamps` endpoint returns 503 while the server is still seeding. The frontend retries automatically (up to ~2 minutes). Each timestamp entry includes `has_motion: true/false` indicating whether a motion field to the next frame is available, plus `gap_before_s` (seconds since previous frame) and `is_gap` (true when the gap exceeds 1.5× the median cadence). The response also includes `motion.max_disp_deg` for the shader's displacement decoding and `gap_info` with `expected_cadence_s`, `gap_count`, and `max_gap_s`.

Atlas tiles and motion PNGs use `Cache-Control: public, max-age=3600, immutable` — radar data for a given timestamp never changes, so the browser caches aggressively. Combined with `cache: 'force-cache'` on the frontend fetch, animation scrubbing through previously visited frames is nearly instant.

### Atlas tile data format

Each atlas tile is a **256×2048 grayscale PNG** — 8 vertical bands of 256×256, one per tilt level:

- Row 0–255: tilt 00.50° (1 km)
- Row 256–511: tilt 01.00° (1.5 km)
- Row 512–767: tilt 01.50° (2 km)
- ...
- Row 1792–2047: tilt 19.00° (19 km)

Pixel encoding: `uint8 = round((dBZ + 30) * 2)`, mapping dBZ range [-30, +97.5] to [0, 255]. Value 0 = no echo. The GPU shader reverses this: `dBZ = pixel / 2.0 - 30.0`.

## Data Pipeline (`pipeline.py`)

The pipeline is the core orchestration layer. It operates on a single principle: **fetch tilt-level data once, store as sparse matrices, derive atlas tiles on demand.**

### Seed flow (startup)

1. List the 60 most recent `00.50` tilt keys from S3
2. For each timestamp, derive S3 keys for all 8 tilt levels
3. Fetch each tilt in parallel (ThreadPoolExecutor, 8 workers)
4. Decode GRIB2 → mask sentinels (< -30 → NaN) → convert to scipy.sparse CSR (NaN → implicit zero)
5. Save sparse tilt grids to `data/tilt_grids/{YYYYMMDD-HHMMSS}/` (8 `.npz` + `meta.json`)
6. Populate in-memory `ConusTiltCache` LRU
7. Pre-render atlas PNG tiles for the most recent 30 frames at z=4 (CONUS default viewport)
8. Compute motion fields for all consecutive frame pairs via FFT block matching, save to disk (`motion.npz` + `motion.png`)

In `DEV_MODE`, steps 1–6 are replaced by loading from disk cache (no S3 fetching). Steps 7–8 still run (skipping pairs that already have motion on disk).

### Periodic refresh (every 60 s)

After the initial seed, a background asyncio task runs every 60 seconds:

1. **Incremental fetch** — lists the 10 most recent S3 keys, skips any already on disk, fetches/decodes/caches only truly new frames
2. **Atlas pre-render** — renders z=4 and z=5 atlas tiles for newly fetched frames
3. **Motion computation** — computes motion fields for any new consecutive frame pairs
4. **Purge** — removes disk entries older than 3 hours and trims to a max of 60 frames; invalidates the in-memory ts_list and atlas tile caches when entries are removed

The periodic refresh runs in both normal and `DEV_MODE`. In `DEV_MODE` the incremental fetch finds nothing new (no S3 keys beyond what's on disk), so it's effectively a no-op.

### Caching architecture

**ConusTiltCache (`cache.py`):**
- Thread-safe LRU for sparse tilt grid sets
- Key: timestamp string → Value: dict of 8 sparse CSR matrices + metadata
- Max 30 entries (~1.2 GB: 30 timestamps × 39 MB sparse per timestamp)
- Falls back to `disk_cache.get_tilt_grids()` on miss (33ms disk load)

**Atlas tile cache (`tiles.py`):**
- `atlas_tile_cache` — thread-safe LRU, max 2000, keyed by `(timestamp, z, x, y)` → PNG bytes
- Pre-populated at startup for z=4 CONUS tiles; cache misses at other zoom levels are rendered on demand

**On-disk (`disk_cache.py`):**
- `data/raw/{tilt}/` — raw `.grib2.gz` bytes from S3 (~0.5 MB each)
- `data/tilt_grids/{YYYYMMDD-HHMMSS}/` — sparse CSR `.npz` per tilt + `meta.json` (~5.4 MB per timestamp) + `motion.npz` / `motion.png` (~50-100 KB per pair)
- 24-hour eviction on startup; 3-hour rolling eviction every 60 s via periodic refresh

### Sparse storage rationale

MRMS data is extremely sparse: 95-99.3% of grid cells are NaN/sentinel after masking. scipy.sparse CSR exploits this:
- Memory: 39 MB per timestamp (vs 784 MB dense) — **20x reduction**
- Disk: 5.4 MB per timestamp (60 frames = 324 MB)
- Disk load: 33ms (vs 295ms for dense compressed npz)
- Disk save: 1.2s per timestamp during seeding

This allows the LRU to hold 30 timestamps (~1.2 GB) rather than just 3 (which would be 2.4 GB dense).

### Atlas tile rendering

Atlas tiles are rendered on demand from sparse tilt grids. For each tile:

1. Compute geographic overlap between the tile bounds and the MRMS grid
2. For each of the 8 tilt levels, extract the sparse subgrid, convert to dense, resample to 256×256 (nearest-neighbor)
3. Encode dBZ to uint8: `round((dBZ + 30) * 2)`, NaN/zero → 0
4. Stack 8 bands vertically into a 256×2048 array
5. Encode as grayscale PNG via Pillow

The resulting PNG is typically 2-10 KB per tile (much smaller than raw binary voxel data). All colorisation, compositing, and smoothing happens on the GPU via GLSL shaders.

## Custom GRIB2 Decoder

**Do not use eccodes, cfgrib, pygrib, or wgrib2.** These all depend on the eccodes C library, which is ~100MB+, has painful cross-platform installation, and is massive overkill for our use case. We only need to decode one product family (MRMS reflectivity) which uses a narrow, predictable subset of the GRIB2 spec.

### GRIB2 structure (what we parse)

| Section | Name | What we extract |
|---------|------|-----------------|
| 0 | Indicator | Magic bytes `GRIB`, edition (must be 2), total message length |
| 1 | Identification | Reference time (year, month, day, hour, minute, second) |
| 2 | Local Use | Skip (optional, MRMS may not include it) |
| 3 | Grid Definition | Grid template, Ni (cols), Nj (rows), lat/lon of first and last grid point, resolution |
| 4 | Product Definition | Parameter category, parameter number, level type, level value (tilt angle) |
| 5 | Data Representation | Packing template number, reference value, binary scale, decimal scale, bits per value |
| 6 | Bitmap | Bitmap presence indicator — if present, a bitmask of valid data points |
| 7 | Data | Packed data values |
| 8 | End | Magic bytes `7777` |

All multi-byte integers are big-endian. Use Python's `struct` module.

### Supported templates

- **Grid Template 3.0** — regular lat/lon grid (the only one MRMS uses)
- **Data Representation Template 5.0** — simple packing (N-bit unsigned integers)
- **Data Representation Template 5.40** — JPEG2000 packing (decoded via Pillow)
- **Data Representation Template 5.41** — PNG packing (decoded via Pillow)

All three data templates use the same physical formula:
```
value = (R + packed_int * 2^E) / 10^D
```

### GRIB2 signed integer encoding

GRIB2 uses an explicit sign bit (MSB), **not** two's-complement, for signed 16-bit fields like the binary scale factor (E) and decimal scale factor (D). This tripped us up initially:
```python
def _signed16(raw: int) -> int:
    sign = (raw >> 15) & 1
    magnitude = raw & 0x7FFF
    return -magnitude if sign else magnitude
```

### Longitude encoding

MRMS longitude values can use the high bit (0x80000000) to represent negative values — not the GRIB2 sign-bit convention but a straightforward unsigned-wrapping pattern. Handle it like:
```python
def microdeg_lon(raw: int) -> float:
    if raw & 0x80000000:
        return (raw - 0x100000000) * 1e-6
    return raw * 1e-6
```

### Bitmap handling

Bitmap indicator byte 5 of Section 6:
- 255 = no bitmap, all grid points have data
- 0 = bitmap follows (1 bit per grid point, 1 = data present, 0 = missing)

When a bitmap is present, the packed values in Section 7 are sparse — only grid points where the bitmap bit is 1 have corresponding values. Expand back to the full grid with NaN fill.

### Section 3 byte offsets (Template 3.0)

All offsets from section start, 0-based:

| Offset | Field | Description |
|--------|-------|-------------|
| +30..33 | Ni | Columns (number of points along parallel) |
| +34..37 | Nj | Rows (number of points along meridian) |
| +46..49 | La1 | Latitude of first grid point (signed int, microdegrees) |
| +50..53 | Lo1 | Longitude of first grid point (unsigned, microdegrees) |
| +55..58 | La2 | Latitude of last grid point |
| +59..62 | Lo2 | Longitude of last grid point |
| +63..66 | Di | i-direction increment (microdegrees) |
| +67..70 | Dj | j-direction increment (microdegrees) |
| +71 | Scanning mode | Bit flags for scan direction |

**Note:** La2/Lo2 start at offset +55/+59, not +56/+60 as the GRIB2 spec's 1-based numbering might suggest. This is because the resolution flags byte at +54 is a single byte, not a 4-byte field.

### Section 5 byte offsets

| Offset | Field | Description |
|--------|-------|-------------|
| +11..14 | R | Reference value (IEEE 754 float) |
| +15..16 | E | Binary scale factor (GRIB2 signed 16-bit) |
| +17..18 | D | Decimal scale factor (GRIB2 signed 16-bit) |
| +19 | bits | Number of bits per packed value |

### What we do NOT need to handle
- Grid templates other than 3.0
- Packing templates other than 5.0, 5.40, 5.41
- Multiple messages per file
- Complex/second-order packing (Template 5.2, 5.3)
- Spectral data, ensemble metadata, or any exotic GRIB2 features

If we encounter an unsupported template, fail loudly with a clear error message identifying the template number.

## NWS Reflectivity Color Scale

Standard palette for mapping dBZ to RGBA. Alpha is 0 below 5 dBZ (no echo).

```python
NWS_DBZ_COLORS = [
    # (min_dbz, max_dbz, r, g, b)
    (5,   10,  64, 192, 64),    # light green
    (10,  15,  48, 160, 48),
    (15,  20,  0,  144, 0),     # green
    (20,  25,  0,  120, 0),
    (25,  30,  255, 255, 0),    # yellow
    (30,  35,  230, 180, 0),    # gold
    (35,  40,  255, 100, 0),    # orange
    (40,  45,  255, 0,   0),    # red
    (45,  50,  200, 0,   0),    # dark red
    (50,  55,  180, 0,  120),   # magenta
    (55,  60,  150, 0,  200),   # purple
    (60,  65,  255, 255, 255),  # white
    (65,  75,  200, 200, 255),  # light blue-white
]
```

The GLSL shader uses a dBZ-proportional alpha curve shaped by the opacity slider, giving broad visibility at high opacity and showing only strong echoes at low opacity.

## Sentinel Value Handling

MRMS uses large negative values (e.g., -999, -99) as sentinels for missing/no-data. These are masked to NaN after decoding:
```python
data[data < -30.0] = np.nan
```

The -30 dBZ threshold preserves legitimate weak reflectivity values while catching all known sentinel patterns.

## Key Constraints & Gotchas

- **MRMS files are gzipped.** Decompress in memory with `gzip.decompress()` before passing to the decoder.
- **Bucket prefix is uppercase.** Use `CONUS/MergedReflectivityQC_{tilt}/` not `conus/`.
- **Files are organized by date.** `CONUS/MergedReflectivityQC_00.50/20260405/MRMS_...grib2.gz`
- **Day boundary spanning.** List files from today, yesterday, and 2 days ago to handle UTC boundaries.
- **Coordinate system.** MRMS uses lat/lon on a regular 0.01° grid. MapLibre handles EPSG:4326.
- **Data freshness.** Files appear in S3 with ~2-3 min latency. The latest file may be 2-5 min old.
- **File size.** A single CONUS tilt frame is ~0.5 MB compressed (.grib2.gz). The full CONUS grid is 3500×7000 at 0.01° resolution.
- **Data sparsity.** 95-99% of CONUS grid cells are NaN/sentinel. This is why scipy.sparse is so effective.
- **CORS.** S3 bucket does not serve CORS headers. All data flows through the backend.
- **No credentials.** S3 access uses unsigned requests (`botocore.UNSIGNED`).
- **Terrain tiles.** AWS `elevation-tiles-prod` is public and free.
- **Scanning direction.** MRMS grids scan left→right, top→bottom (NW corner first). The decoder checks scanning mode flags and flips the array if needed.
- **Startup time.** Seeding 60 frames across 8 tilt levels (480 files) takes 1-2 minutes. The server starts accepting requests immediately; the frontend retries on 503.
- **Thread safety.** `ConusTiltCache` uses `threading.Lock`. Seeding runs in a daemon thread. Pipeline uses `ThreadPoolExecutor` for concurrent S3 fetches.
- **Sparse zero = NaN.** When converting to CSR, NaN becomes implicit zero. When reading back for rendering, explicit zeros are treated as NaN (no echo). This is valid because dBZ < -30 is already masked.

## File Structure

```
weather_radar/
├── CLAUDE.md              ← this file
├── INTEGRATION.md         ← React Native integration spec (WebView + postMessage API)
├── .env                   ← API keys (not committed)
├── .env.example           ← template for .env
├── .gitignore
├── backend/
│   ├── requirements.txt   ← production dependencies (includes scipy)
│   ├── requirements-dev.txt ← dev/test dependencies
│   ├── main.py            ← FastAPI app, atlas/motion tile endpoints, timestamps, config
│   ├── pipeline.py        ← data pipeline: fetch tilts → sparse CSR → disk + LRU + atlas + motion
│   ├── motion.py          ← FFT block matching, motion field computation + PNG encoding
│   ├── tiles.py           ← TMS tile math, atlas tile rendering (PNG), tile cache
│   ├── mrms.py            ← S3 client, file listing, download
│   ├── cache.py           ← ConusTiltCache (sparse LRU with disk fallback)
│   ├── disk_cache.py      ← on-disk raw + sparse tilt grid + motion field cache
│   ├── render.py          ← NWS reflectivity color scale constants
│   └── grib2/
│       ├── __init__.py
│       ├── decoder.py     ← entry point: bytes → (metadata, numpy array)
│       ├── sections.py    ← section-level parsing (0–7)
│       ├── packing.py     ← simple, J2K, PNG unpackers
│       └── bitstream.py   ← N-bit integer reader utility
├── frontend/
│   ├── package.json       ← esbuild dev dependency, build/watch scripts
│   ├── package-lock.json  ← locked dependency versions (committed)
│   ├── build.js           ← esbuild config: bundle, minify, copy HTML
│   ├── index.html         ← single-page app (responsive layout)
│   ├── style.css          ← dark theme, glassmorphism panels, responsive mobile styles
│   ├── app.js             ← entry point: map init, responsive UI, WebView auto-detect
│   ├── radar-engine.js    ← RadarEngine class: animation, timestamps, presets (no DOM)
│   ├── radar-layer.js     ← RadarLayer: three.js overlay, GLSL shaders, tile/motion caches
│   ├── radar-bridge.js    ← RadarBridge: postMessage API for RN WebView communication
│   ├── colors.js          ← NWS color scale, legend builder, GPU color ramp data
│   └── dist/              ← production build output (gitignored)
│       ├── index.html
│       ├── radar.min.js   ← bundled + minified (~37 KB)
│       ├── radar.min.js.map
│       └── style.min.css
├── tests/
│   ├── __init__.py
│   ├── fixtures/          ← real MRMS .grib2.gz file (gitignored)
│   └── test_decoder.py    ← shape, bounds, value range tests
├── scripts/
│   └── test_fetch.py      ← standalone download + decode diagnostic
└── data/                  ← runtime cache (gitignored)
    ├── raw/{tilt}/        ← raw .grib2.gz files from S3
    └── tilt_grids/        ← sparse CSR per tilt + metadata + motion per timestamp
        └── {YYYYMMDD-HHMMSS}/
            ├── meta.json
            ├── 00.50.npz
            ├── 01.50.npz
            ├── ...
            ├── motion.npz   ← U, V, confidence arrays (raw motion field)
            └── motion.png   ← pre-rendered RGB PNG served to frontend
```

## Running

```bash
# Install backend dependencies
pip install -r backend/requirements.txt

# (Optional) Build minified frontend for production
cd frontend && npm install && npm run build && cd ..

# Set up API keys (optional — app works without them)
cp .env.example .env
# Edit .env to add STADIA_API_KEY or MAPTILER_API_KEY

# Start the server (from project root) — serves both API and frontend
uvicorn backend.main:app

# Dev mode (skip S3 fetching, load from disk cache):
DEV_MODE=1 uvicorn backend.main:app

# Open in browser (responsive — works on desktop and mobile)
open http://localhost:8000

# Minimal controls mode (animation bar only):
open http://localhost:8000/?controls=minimal

# No controls mode (bare map + radar, for embedded use with postMessage):
open http://localhost:8000/?controls=none
```

The frontend build step is optional for development — browsers load the ES modules directly via `<script type="module">`. If `frontend/dist/` exists (from `npm run build`), FastAPI serves the minified bundle; otherwise it serves the raw source files.

The server seeds 60 frames on startup in a background thread, then pre-renders atlas tiles for the default CONUS viewport and computes motion fields for all consecutive pairs. After seeding, a background task polls S3 every 60 seconds to pick up new frames and purge data older than 3 hours. The frontend auto-retries until frames are available (~1-2 minutes). Once loaded, the radar starts paused on the latest (newest) frame — the user taps play to animate. In `DEV_MODE`, frames load from disk cache in ~1 second; motion fields are computed for any new pairs.

## Running Tests

```bash
# Download a test fixture first (if tests/fixtures/ is empty)
python scripts/test_fetch.py

# Run decoder tests
pip install -r backend/requirements-dev.txt
python -m pytest tests/test_decoder.py -v
```

## Motion-Compensated Interpolation (`motion.py`)

Between consecutive 2-minute radar frames, a simple crossfade (`mix()`) causes storms to ghost/pulse — sharp at keyframes, blurry between them. Motion-compensated interpolation fixes this by sliding storm cells along their displacement vectors.

### Backend: FFT block matching

`compute_motion_field()` in `motion.py`:
1. Build 2D composite via `fmax` across 8 tilt levels (same as the shader's composite mode)
2. Downsample 8x → ~438×875 for efficiency
3. For each 32×32 block (stride 16), find best match in next frame via `scipy.signal.fftconvolve` within a ±12 pixel search window
4. Record displacement (U east-west, V north-south) in degrees and NCC confidence
5. Median-filter 3×3 to remove outlier vectors
6. Output: ~26×53 motion field arrays

### Motion PNG encoding

`encode_motion_png()` encodes U, V, confidence into an RGB PNG:
- **R** = `clamp(U_deg / 0.5 * 127.5 + 128, 0, 255)` — east-west displacement, ±0.5° range
- **G** = `clamp(V_deg / 0.5 * 127.5 + 128, 0, 255)` — north-south displacement
- **B** = `clamp(confidence * 255, 0, 255)` — NCC match quality

Value 128 in R/G = zero displacement. One global CONUS-wide PNG per frame pair (~2-5 KB). All tile meshes reference the same GPU texture.

### Frontend: Semi-Lagrangian advection shader

The GLSL fragment shader's `getMotion()` function:
1. Converts tile UV → geographic lon/lat (via Mercator inverse)
2. Maps lon/lat → motion texture UV using the CONUS grid bounds
3. Samples the motion PNG to get (U, V, confidence)
4. Converts displacement from degrees to tile UV space (with latitude-dependent Mercator correction: `uvDispY = disp_deg * n / (360 * cos(lat))`)

The `sampleInterp()` function then performs semi-Lagrangian advection:
- Traces backward from the current position by `α * displacement` to sample frame A
- Traces forward by `(1-α) * displacement` to sample frame B
- Blends via `mix(sA, sB, α)` for the advected result
- Computes a plain crossfade at the original UV position
- Returns `mix(crossfade, advected, confidence)` — confidence-weighted blend

Fallback: if motion data is unavailable (`u_hasMotion < 0.5`), the shader reverts to plain crossfade. The animation never stalls waiting for motion data.

## Design Decisions & Learnings

### Why tilt-level data instead of the pre-computed composite

The original plan was to start with `MergedReflectivityQCComposite` (2D) and add `MergedReflectivityQC` (3D) later. We skipped straight to tilt-level data because:
- One fetch path gives both 2D and 3D output — `fmax` across tilts produces an equivalent composite
- Avoids maintaining two separate S3 listing/download paths
- 3D volume data was the eventual goal anyway

### Why scipy.sparse for MRMS data

MRMS reflectivity grids are extremely sparse: after sentinel masking, 95-99.3% of grid cells are NaN (no echo). Profiling showed:
- Dense float32: 784 MB per timestamp (8 tilts × 98 MB) — LRU of 3 = 2.4 GB
- Sparse CSR: 39 MB per timestamp — LRU of 20 = 780 MB
- Disk save: 1.2s / 5.4 MB (sparse) vs 2.8s / 7.1 MB (dense compressed)
- Disk load: 33ms (sparse) vs 295ms (dense compressed)
- Composite derivation from sparse: 80ms (sparse→dense→fmax, cached)

Sparse is better on every axis: memory, disk, I/O speed, and allows caching 30 frames in memory instead of 3.

### Why atlas tiles with GPU-side rendering (not PointCloudLayer)

The earlier approach used deck.gl `PointCloudLayer` with `DataFilterExtension` for GPU-driven frame switching. While scrubbing was fast once data was loaded, it had several visual and architectural limitations:
- At CONUS zoom, the 2D composite showed a "dot grid" rather than a continuous raster field
- Bulk binary responses were large (~5-20 MB per tile coordinate for all frames)
- No temporal interpolation between frames (discrete jumps)
- deck.gl added ~500 KB of JavaScript overhead for a single layer type

The atlas tile approach, inspired by [MapTiler's 3D weather demo](https://www.maptiler.com/tools/weather/3d/), achieves MapTiler-level visual polish:
1. **Atlas tiles** — 256×2048 grayscale PNG per tile, 8 tilt bands stacked vertically (~2-10 KB each)
2. **three.js overlay** — separate `WebGLRenderer` canvas over MapLibre, camera projection synced per frame from MapLibre's matrix. Registered as a MapLibre `CustomLayerInterface`.
3. **GLSL shaders** — GPU-side dBZ decoding, NWS color ramp lookup, motion-compensated semi-Lagrangian advection, spatial smoothing, tile edge blending
4. **Continuous animation** — float `currentAnimationTime` with motion-compensated interpolation so storms slide smoothly between keyframes
5. **Three view modes from same data** — composite takes `fmax` across 8 bands in the shader; 3D renders 8 stacked quads; volume ray-marches through a stitched tile atlas
6. **Browser HTTP cache** — `Cache-Control: immutable` + `force-cache` makes revisited frames instant

### Why FFT block matching for motion (not optical flow or MRMS products)

We evaluated three approaches for computing inter-frame displacement:
1. **MRMS storm motion products** — NSSL publishes velocity/direction tables, but no gridded displacement field suitable for pixel-level advection is available in the public S3 bucket.
2. **Dense optical flow** (e.g. Farnebäck) — accurate but slow on CONUS-scale grids (~3500×7000) and requires OpenCV as a new dependency.
3. **FFT block matching** — fast (~0.5-1s per pair), uses existing scipy/numpy, produces a low-resolution motion field (~26×53) that's naturally smooth and lightweight to serve as a single PNG.

Block matching on 8x-downsampled composites hits the right trade-off: fast enough for background computation during seeding, accurate enough for the 2-minute inter-frame intervals (storms move ~0.1-0.3° in that time), and the confidence output enables graceful fallback to crossfade where motion estimation is unreliable (cell initiation, dissipation, merging).

### Why one global motion texture per pair (not per-tile)

The motion field is ~26×53 pixels covering all of CONUS — far too small to justify splitting per tile. A single RGB PNG (~2-5 KB) is loaded once per frame pair and bound to all tile meshes as a shared uniform. The GPU's bilinear texture filter interpolates between the coarse motion vectors, which is appropriate given that storm motion is spatially coherent over hundreds of km.

### Background seeding

The server starts accepting HTTP requests immediately. Frame seeding (480 S3 fetches for 60 timestamps × 8 tilts) runs in a daemon thread, followed by atlas tile pre-rendering for the default z=4 CONUS viewport. The frontend handles the 503→retry loop transparently. This avoids a long startup delay while still serving a full frame history once warmed.

After the seed (or in parallel with it), a periodic refresh task polls S3 every 60 seconds to pick up new 2-minute MRMS frames as they appear. It also purges disk/memory entries older than 3 hours and caps the total frame count at 60, keeping the data directory bounded.

### On-disk cache layout

The tilt grid cache uses per-timestamp directories containing 8 sparse `.npz` files (one per tilt), a `meta.json`, and optional motion field files:
```
data/tilt_grids/20260405-120000/
    meta.json
    00.50.npz
    01.50.npz
    ...
    19.00.npz
    motion.npz      ← U, V, confidence arrays for this frame → next frame
    motion.png       ← pre-rendered RGB PNG (served directly to frontend)
```

This makes eviction simple (delete entire directory), atomic checks easy (test for `meta.json` or `motion.png`), and keeps the raw S3 cache (`data/raw/{tilt}/`) separate.

Legacy caches (`data/decoded/`, `data/composites/`, `data/grib2_cache/`) are automatically removed on startup by the migration logic in `disk_cache.py`.

### Deferred tile rebuild to prevent zoom flicker

When the user zooms, the tile set changes (e.g., z=4 → z=5). Rebuilding tile meshes immediately causes flicker: new meshes start invisible (no textures yet) and old "stale" meshes get purged as soon as the first new tile loads, leaving gaps where the rest haven't arrived. The base map doesn't have this problem because MapLibre manages its own tile transitions internally.

The fix uses **deferred mesh rebuild with atomic swap**:
1. `updateVisibleTiles()` stores the new tile set as `_pendingTiles` instead of rebuilding immediately
2. Old meshes keep rendering (they use world-space Mercator coordinates, so they stretch/shrink correctly during zoom)
3. Textures for the pending tile set are prefetched in the background
4. Once ALL pending tile textures are cached, `_applyPendingTiles()` swaps atomically — old meshes out, new fully-textured meshes in, single frame
5. A 3-second safety timeout force-applies if some tiles fail to load

As a belt-and-suspenders measure, `_updateMaterials` only purges stale fallback meshes when **all** new meshes have textures (`allTextured`), not when the first one arrives (`anyTextured`). Never use `anyTextured` for the stale purge condition — it causes partial tile visibility.

### Gap-aware animation

MRMS data has inherent gaps — the ~2-minute cadence is nominal, not guaranteed. Upstream causes include radar scan cycle timing, ingestion latency from the ~180 WSR-88D sites, product generation failures, and individual radar outages. The `/timestamps` endpoint computes the median inter-frame cadence and annotates each entry with `gap_before_s` and `is_gap` (flagged when the gap exceeds 1.5× the median cadence).

The frontend uses this to:
1. **Weight animation timing** — `_computeFrameWeights()` in `RadarEngine` builds a per-frame weight array proportional to the gap duration (capped at 3×). The animation tick divides the base frame interval by this weight, so transitions spanning larger real-time gaps play proportionally slower instead of producing a visual "jump."
2. **Show gap markers on the scrubber** — amber tick marks appear at positions where `is_gap` is true, with a tooltip showing the gap duration.
3. **Annotate the frame time display** — when scrubbing over a gap frame, the time readout shows e.g. "14:08 (+4m)" to indicate the gap.

The pipeline also retries once (3s delay) on `S3KeyNotFound` during tilt fetching, catching files that are still propagating in S3. The periodic refresh checks the 20 most recent S3 keys (covering ~40 minutes) to reduce the chance of missing late-arriving frames.
