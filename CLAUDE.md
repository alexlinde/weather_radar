# CLAUDE.md — Weather Radar Prototype

## What we're building

A web-based weather radar viewer that pulls live MRMS (Multi-Radar Multi-Sensor) data from NOAA's public AWS bucket and renders it over a map of the continental US (CONUS). The goal is to show real radar data the way a meteorologist would see it — full reflectivity at multiple vertical levels, with smooth animation — on top of a base map that gives geographic context (terrain, water, urban areas).

This is a working prototype, not a production app. Prioritise getting real data on screen over polish.

## Current status

**Phase 1 (data on screen): COMPLETE**
**Phase 2 (time animation): COMPLETE**
**Phase 3 (3D terrain + vertical levels): COMPLETE**
**Phase 3.5 (unified voxel architecture): COMPLETE**
**Phase 4 (motion-compensated interpolation): NOT STARTED**

The app fetches tilt-level reflectivity data across 8 vertical levels, stores them as sparse matrices (scipy.sparse CSR). The backend serves all 20 frames of voxel data for each tile coordinate in a single bulk binary response. The frontend loads all voxels for visible tiles into GPU buffers once, then uses deck.gl `DataFilterExtension` to switch frames by updating a single GPU uniform — achieving zero-cost scrubbing with no per-frame network requests or buffer re-uploads. Both 2D composite and 3D volumetric views use the same `PointCloudLayer`, differing only in pitch, point size, and depth testing. The map uses MapLibre GL JS with 3D terrain, starting at a CONUS-wide view.

## Architecture

```
┌─────────────────┐      ┌──────────────────────┐      ┌────────────────────┐
│  NOAA S3 Bucket  │─────▶│  Python Backend       │─────▶│  Browser Frontend   │
│  noaa-mrms-pds   │      │  (FastAPI)             │      │  (MapLibre + deck.gl)│
│  Public, no auth │      │  Fetch, decode, sparse │      │  Map + radar overlay │
└─────────────────┘      └──────────────────────┘      └────────────────────┘
```

### Backend (Python / FastAPI)

Responsibilities:
- Fetch MRMS tilt-level GRIB2 files from S3 across 8 elevation angles
- Decode with custom minimal GRIB2 decoder (no eccodes dependency)
- Convert to scipy.sparse CSR matrices (95-99% of grid is NaN/sentinel → 20x memory reduction)
- Serve bulk voxel tiles (`/api/radar/volume/bulk/{z}/{x}/{y}.bin`) — all 20 frames packed in one binary response per tile coordinate
- Zoom-based spatial downsampling keeps voxel counts manageable
- Two-tier caching: ConusTiltCache (sparse LRU, ~20 entries, ~780 MB) + binary voxel tile LRU (2000 entries)
- GZip middleware compresses binary voxel responses (~80-90% reduction)
- Background seeding on startup (60 frames across all tilts) + voxel tile pre-rendering for default viewport

### Frontend (HTML + JS)

Responsibilities:
- Render base map with MapLibre GL JS (Stadia/MapTiler/OpenFreeMap cascade)
- 3D terrain via AWS elevation tiles (always enabled)
- Bulk-fetch all frames' voxels for visible tiles on load → build combined typed arrays
- Single `deck.PointCloudLayer` with `DataFilterExtension` renders both 2D and 3D views
- Frame scrubbing updates a single GPU `filterRange` uniform — zero-cost, no network or buffer work
- 2D/3D toggle changes pitch, point size, and depth testing on the same layer
- Re-fetches bulk voxel data when zoom level changes (debounced)
- Default CONUS view at z=4, user zooms in to area of interest
- Time animation with 20 frames, play/pause, scrubber, speed control
- Opacity slider, vertical exaggeration slider (3D mode)
- NWS reflectivity color scale legend
- Auto-refresh every 2 minutes

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
00.50°, 01.50°, 02.50°, 03.50°, 05.00°, 07.00°, 10.00°, 14.00°
```

These are mapped to approximate physical heights for 3D rendering:
```python
TILT_TO_HEIGHT_KM = {
    "00.50": 1.0, "01.50": 2.0, "02.50": 3.5, "03.50": 5.0,
    "05.00": 7.0, "07.00": 9.0, "10.00": 12.0, "14.00": 15.0,
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

3D terrain is always enabled using AWS Terrain Tiles:
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
- **MapLibre GL JS** (~v4) — base map + 3D terrain
- **deck.gl** (~v9) — PointCloudLayer, DataFilterExtension, MapboxOverlay
- **Vanilla JS** — no framework, no build step

Loaded from CDN:
```html
<script src="https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.css" rel="stylesheet" />
<script src="https://unpkg.com/deck.gl@9/dist.min.js"></script>
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/radar/volume/bulk/{z}/{x}/{y}.bin` | GET | All frames' voxels for a tile coordinate in one binary response. z=3–8. |
| `/api/radar/timestamps` | GET | Available timestamps with bounds. |
| `/api/radar/refresh` | GET | Force cache invalidation + re-seed. |
| `/api/config` | GET | Frontend configuration (map tile API keys). |
| `/health` | GET | Cache status (in-memory + on-disk counts). |

The `/timestamps` endpoint returns 503 while the server is still seeding. The frontend retries automatically (up to ~2 minutes).

The bulk endpoint packs all 20 frames into one response: `[uint16 frame_count][per-frame: [uint32 count][float32 positions[3*N]][uint8 colors[4*N]]]`. GZip middleware compresses the binary data ~80-90%. The response uses `Cache-Control: public, max-age=120` since new frames appear every 2 minutes.

## Data Pipeline (`pipeline.py`)

The pipeline is the core orchestration layer. It operates on a single principle: **fetch tilt-level data once, store as sparse matrices, pre-render voxel tiles for the default viewport.**

### Seed flow (startup)

1. List the 60 most recent `00.50` tilt keys from S3
2. For each timestamp, derive S3 keys for all 8 tilt levels
3. Fetch each tilt in parallel (ThreadPoolExecutor, 8 workers)
4. Decode GRIB2 → mask sentinels (< -30 → NaN) → convert to scipy.sparse CSR (NaN → implicit zero)
5. Save sparse tilt grids to `data/tilt_grids/{YYYYMMDD-HHMMSS}/` (8 `.npz` + `meta.json`)
6. Populate in-memory `ConusTiltCache` LRU
7. Pre-render binary voxel tiles for the most recent 20 frames at z=4 (CONUS default viewport)

In `DEV_MODE`, steps 1–6 are replaced by loading from disk cache (no S3 fetching). Step 7 still runs.

### Caching architecture

**ConusTiltCache (`cache.py`):**
- Thread-safe LRU for sparse tilt grid sets
- Key: timestamp string → Value: dict of 8 sparse CSR matrices + metadata
- Max 20 entries (~780 MB: 20 timestamps × 39 MB sparse per timestamp)
- Falls back to `disk_cache.get_tilt_grids()` on miss (33ms disk load)

**Binary tile cache (`tiles.py`):**
- `bin_tile_cache` — thread-safe LRU, max 2000, keyed by `(timestamp, z, x, y)` → binary voxel bytes
- Pre-populated at startup for z=4 CONUS tiles; cache misses at other zoom levels are computed on demand

**On-disk (`disk_cache.py`):**
- `data/raw/{tilt}/` — raw `.grib2.gz` bytes from S3 (~0.5 MB each)
- `data/tilt_grids/{YYYYMMDD-HHMMSS}/` — sparse CSR `.npz` per tilt + `meta.json` (~5.4 MB per timestamp)
- 24-hour eviction on startup

### Sparse storage rationale

MRMS data is extremely sparse: 95-99.3% of grid cells are NaN/sentinel after masking. scipy.sparse CSR exploits this:
- Memory: 39 MB per timestamp (vs 784 MB dense) — **20x reduction**
- Disk: 5.4 MB per timestamp (60 frames = 324 MB)
- Disk load: 33ms (vs 295ms for dense compressed npz)
- Disk save: 1.2s per timestamp during seeding

This allows the LRU to hold 20 timestamps (~780 MB) rather than just 3 (which would be 2.4 GB dense).

### 3D voxel tile zoom-based downsampling

At CONUS zoom (z=3-4), full-resolution voxel tiles would contain millions of points. Several strategies keep voxel counts manageable:

1. **Spatial downsampling:** `max(1, 2^(7-z))` — more aggressive step at each zoom level
2. **Tilt reduction:** At z≤5, only 4 of 8 tilts are used (00.50, 02.50, 05.00, 10.00)
3. **dBZ threshold:** At z≤5, min_dbz=15 instead of 10 to filter weak echoes
4. **Hard cap:** 40K voxels per tile maximum

| Zoom | Step | Tilts | Min dBZ | Approx voxels/tile |
|------|------|-------|---------|--------------------|
| z=3 | 16 | 4 | 15 | ~500 |
| z=4 | 8 | 4 | 15 | ~2,000 |
| z=5 | 4 | 4 | 15 | ~8,000 |
| z=6 | 2 | 8 | 10 | ~15,000 |
| z=7 | 1 | 8 | 10 | ~20,000 |
| z=8 | 1 | 8 | 10 | ~1,000 |

GZip middleware compresses the binary voxel data ~80-90% over the wire.

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

The PointCloudLayer uses a dynamic alpha ramp from 100–255 based on dBZ value (`colorize_voxels` in `tiles.py`).

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
├── .env                   ← API keys (not committed)
├── .env.example           ← template for .env
├── .gitignore
├── backend/
│   ├── requirements.txt   ← production dependencies (includes scipy)
│   ├── requirements-dev.txt ← dev/test dependencies
│   ├── main.py            ← FastAPI app, bulk voxel endpoint, timestamps, config
│   ├── pipeline.py        ← data pipeline: fetch tilts → sparse CSR → disk + LRU + voxel pre-render
│   ├── tiles.py           ← TMS tile math, voxel extraction + binary packing, tile cache
│   ├── mrms.py            ← S3 client, file listing, download
│   ├── cache.py           ← ConusTiltCache (sparse LRU with disk fallback)
│   ├── disk_cache.py      ← on-disk raw + sparse tilt grid cache
│   ├── render.py          ← NWS reflectivity color scale constants
│   └── grib2/
│       ├── __init__.py
│       ├── decoder.py     ← entry point: bytes → (metadata, numpy array)
│       ├── sections.py    ← section-level parsing (0–7)
│       ├── packing.py     ← simple, J2K, PNG unpackers
│       └── bitstream.py   ← N-bit integer reader utility
├── frontend/
│   ├── index.html         ← single-page app
│   ├── style.css          ← dark theme, glassmorphism panels
│   ├── app.js             ← map init, bulk voxel loading, GPU-filtered animation
│   └── colors.js          ← NWS color scale + legend builder
├── tests/
│   ├── __init__.py
│   ├── fixtures/          ← real MRMS .grib2.gz file (gitignored)
│   └── test_decoder.py    ← shape, bounds, value range tests
├── scripts/
│   └── test_fetch.py      ← standalone download + decode diagnostic
└── data/                  ← runtime cache (gitignored)
    ├── raw/{tilt}/        ← raw .grib2.gz files from S3
    └── tilt_grids/        ← sparse CSR per tilt + metadata per timestamp
        └── {YYYYMMDD-HHMMSS}/
            ├── meta.json
            ├── 00.50.npz
            ├── 01.50.npz
            └── ...
```

## Running

```bash
# Install dependencies
pip install -r backend/requirements.txt

# Set up API keys (optional — app works without them)
cp .env.example .env
# Edit .env to add STADIA_API_KEY or MAPTILER_API_KEY

# Start the server (from project root) — serves both API and frontend
uvicorn backend.main:app

# Dev mode (skip S3 fetching, load from disk cache):
DEV_MODE=1 uvicorn backend.main:app

# Open in browser
open http://localhost:8000
```

The server seeds 60 frames on startup in a background thread, then pre-renders voxel tiles for the default CONUS viewport. The frontend auto-retries until frames are available (~1-2 minutes). In `DEV_MODE`, frames load from disk cache in ~1 second.

## Running Tests

```bash
# Download a test fixture first (if tests/fixtures/ is empty)
python scripts/test_fetch.py

# Run decoder tests
pip install -r backend/requirements-dev.txt
python -m pytest tests/test_decoder.py -v
```

## Build Plan (remaining)

### Phase 4: Motion-compensated frame interpolation

9. **Backend: Compute motion fields**
   - Between consecutive cached frames, compute a 2D displacement field (block matching or optical flow)
   - MRMS also publishes derived storm motion vectors — evaluate whether these are usable directly
   - Serve motion vectors alongside tile data

10. **Frontend: Semi-Lagrangian advection**
    - For each intermediate frame at time t between keyframes t₀ and t₁:
      - Trace each pixel backward along the motion vector by α to sample from frame t₀
      - Trace forward from t₁ by (1 - α) to sample from frame t₁
      - Blend based on temporal proximity
    - Storms should slide smoothly across the map instead of ghosting/doubling
    - Fall back to crossfade locally where the motion field has high residual error (cell splits, new development, decay)

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

Sparse is better on every axis: memory, disk, I/O speed, and allows caching 20 frames in memory instead of 3.

### Why unified voxel architecture (not per-frame tile fetching)

The earlier approach used deck.gl `TileLayer` to fetch PNG (2D) or binary (3D) tiles per frame per visible tile coordinate. This had poor scrubbing performance because every frame change triggered:
- HTTP requests for each visible tile × the new frame
- Decoding/uploading new GPU textures or buffers
- Layer re-creation or prop invalidation in deck.gl

The unified approach, inspired by the [deck.gl animated flight demo](https://deck.gl/examples/maplibre), loads all data up front and animates entirely on the GPU:
1. **Bulk binary endpoint** — one request per tile coordinate returns all 20 frames' voxels
2. **Combined typed arrays** — positions, colors, and frame indices concatenated into single buffers
3. **`DataFilterExtension`** — each voxel has a `frameIndex` attribute; `filterRange` selects the visible frame
4. **Frame scrubbing** — updates one GPU uniform, no buffer re-uploads, no network requests
5. **Both 2D and 3D** — same `PointCloudLayer`, different pitch/pointSize/depthTest settings

Trade-off: at CONUS zoom (z=4), the 2D composite view shows a "dot grid" rather than continuous raster. Acceptable for a prototype; could be addressed with custom shaders or screen-space splatting later.

### Why PointCloudLayer instead of ColumnLayer

deck.gl `PointCloudLayer` turned out to work better than `ColumnLayer` for volumetric radar:
- Each voxel is a single point with a color — simpler data format (just `[lon, lat, alt, dbz]`)
- Handles irregular sampling (not all cells are active at all levels)
- Performs well with 100K+ points
- Vertical exaggeration is just a multiplier on the altitude
- `DataFilterExtension` integrates cleanly for frame filtering

### Background seeding

The server starts accepting HTTP requests immediately. Frame seeding (480 S3 fetches for 60 timestamps × 8 tilts) runs in a daemon thread, followed by voxel pre-rendering for the default z=4 CONUS viewport. The frontend handles the 503→retry loop transparently. This avoids a long startup delay while still serving a full frame history once warmed.

### On-disk cache layout

The tilt grid cache uses per-timestamp directories containing 8 sparse `.npz` files (one per tilt) plus a `meta.json`:
```
data/tilt_grids/20260405-120000/
    meta.json
    00.50.npz
    01.50.npz
    ...
    14.00.npz
```

This makes eviction simple (delete entire directory), atomic checks easy (test for `meta.json`), and keeps the raw S3 cache (`data/raw/{tilt}/`) separate.

Legacy caches (`data/decoded/`, `data/composites/`, `data/grib2_cache/`) are automatically removed on startup by the migration logic in `disk_cache.py`.
