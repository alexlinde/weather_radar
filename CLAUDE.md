# CLAUDE.md — NYC Weather Radar Prototype

## What we're building

A web-based weather radar viewer that pulls live MRMS (Multi-Radar Multi-Sensor) data from NOAA's public AWS bucket and renders it over a map of New York City. The goal is to show real radar data the way a meteorologist would see it — full reflectivity at multiple vertical levels, with smooth animation — on top of a base map that gives geographic context (terrain, water, urban areas).

This is a working prototype, not a production app. Prioritise getting real data on screen over polish.

## Current status

**Phase 1 (data on screen): COMPLETE**
**Phase 2 (time animation): COMPLETE**
**Phase 3 (3D terrain + vertical levels): COMPLETE**
**Phase 4 (motion-compensated interpolation): NOT STARTED**

The app fetches 60 frames of tilt-level reflectivity data across 8 vertical levels, derives both 2D composites and 3D voxel volumes, and renders them over a MapLibre base map with 3D terrain. Animation plays through ~2 hours of history with play/pause, scrubber, and speed controls. A toggle switches between 2D composite and 3D point cloud views.

## Architecture

```
┌─────────────────┐      ┌──────────────────────┐      ┌────────────────────┐
│  NOAA S3 Bucket  │─────▶│  Python Backend       │─────▶│  Browser Frontend   │
│  noaa-mrms-pds   │      │  (FastAPI)             │      │  (MapLibre + deck.gl)│
│  Public, no auth │      │  Fetch, decode, clip   │      │  Map + radar overlay │
└─────────────────┘      └──────────────────────┘      └────────────────────┘
```

### Backend (Python / FastAPI)

Responsibilities:
- Fetch MRMS tilt-level GRIB2 files from S3 across 8 elevation angles
- Decode with custom minimal GRIB2 decoder (no eccodes dependency)
- Clip to NYC bounding box
- Derive 2D composite via `nanmax` across tilt levels
- Compute 3D voxel volumes for deck.gl PointCloudLayer
- Serve radar data as JSON, PNG, or pre-computed voxel arrays
- Two-tier caching: in-memory bounded cache + on-disk raw/decoded cache
- Background seeding on startup (60 frames across all tilts)

### Frontend (HTML + JS)

Responsibilities:
- Render base map with MapLibre GL JS (Stadia/MapTiler/OpenFreeMap cascade)
- 3D terrain via AWS elevation tiles (always enabled)
- Overlay radar data using deck.gl BitmapLayer (2D) or PointCloudLayer (3D)
- View mode toggle: Composite (2D flat) vs 3D Layers (volumetric points)
- Time animation with 60 frames, play/pause, scrubber, speed control
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

The 2D composite is derived via `nanmax` across all tilt grids at each grid point — equivalent to the pre-computed `MergedReflectivityQCComposite` product, but we get 3D for free.

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

### NYC bounding box for clipping
```
North: 42.0
South: 39.44
East:  -72.67
West:  -75.23
```

This is larger than the original plan — covers NYC, most of NJ, Long Island, the Hudson Valley, and parts of CT. Generous enough to see weather systems approaching from any direction.

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
center: [-73.98, 40.75],  // Midtown Manhattan
zoom: 10,                  // Shows all 5 boroughs + surroundings
pitch: 0,                  // Flat in composite mode; 50° in 3D mode
bearing: 0
```

## Tech Stack

### Backend
- **Python 3.11+**
- **FastAPI** — API server
- **numpy** — array operations, clipping, voxel computation
- **boto3** — S3 access (unsigned requests, no credentials needed)
- **Pillow** — PNG rendering + JPEG2000 decoding (ships with openjpeg)
- **uvicorn** — ASGI server
- **python-dotenv** — load `.env` for API keys

No eccodes, no cfgrib, no system-level C dependencies.

### Frontend
- **MapLibre GL JS** (~v4) — base map + 3D terrain
- **deck.gl** (~v9) — BitmapLayer (2D), PointCloudLayer (3D), MapboxOverlay integration
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
| `/api/radar/latest` | GET | Latest clipped NYC radar as JSON (grid + metadata) |
| `/api/radar/image` | GET | Latest radar as PNG with NWS color scale |
| `/api/radar/frames?count=60` | GET | Recent frames as base64 PNGs, oldest-first |
| `/api/radar/volume` | GET | Latest multi-level snapshot as voxel data |
| `/api/radar/volume/frames?count=60` | GET | Recent volume frames as pre-computed voxels |
| `/api/radar/refresh` | GET | Force cache invalidation + re-seed |
| `/api/config` | GET | Frontend configuration (map tile API keys) |
| `/health` | GET | Cache status (frame counts) |

The `/frames` and `/volume/frames` endpoints return 503 while the server is still seeding on startup. The frontend retries automatically (up to ~2 minutes).

## Data Pipeline (`pipeline.py`)

The pipeline is the core orchestration layer. It operates on a single principle: **fetch tilt-level data once, derive both 2D and 3D outputs.**

### Seed flow (startup)

1. List the 60 most recent `00.50` tilt keys from S3
2. For each timestamp, derive S3 keys for all 8 tilt levels
3. Fetch each tilt in parallel (ThreadPoolExecutor, 8 workers)
4. For each tilt: check disk cache → download from S3 → decode GRIB2 → clip to NYC → save to disk cache
5. Across all tilts for a timestamp: compute `nanmax` composite + extract voxels above 10 dBZ
6. Store composite in `composite_cache`, voxels in `volume_cache`

### Caching architecture

**In-memory (`cache.py`):**
- `BoundedCache` — thread-safe OrderedDict with max 70 entries, oldest-first eviction
- `composite_cache` — keyed by S3 ref_key, stores `FrameEntry(metadata, grid)`
- `volume_cache` — keyed by ISO timestamp, stores voxel lists

**On-disk (`disk_cache.py`):**
- `data/raw/{tilt}/` — raw `.grib2.gz` bytes from S3
- `data/decoded/{tilt}/` — `.npy` (numpy array) + `.json` (metadata) per decoded frame
- 24-hour eviction on startup
- Legacy cache migration from flat layout to per-tilt layout

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

Backend PNG rendering uses alpha 220 for all active bands. The 3D PointCloudLayer uses a dynamic alpha ramp from 100–255 based on dBZ value.

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
- **File size.** A single CONUS tilt frame is ~2-3 MB compressed. The NYC clip is a few KB.
- **CORS.** S3 bucket does not serve CORS headers. All data flows through the backend.
- **No credentials.** S3 access uses unsigned requests (`botocore.UNSIGNED`).
- **Terrain tiles.** AWS `elevation-tiles-prod` is public and free.
- **Scanning direction.** MRMS grids scan left→right, top→bottom (NW corner first). The decoder checks scanning mode flags and flips the array if needed.
- **Startup time.** Seeding 60 frames across 8 tilt levels (480 files) takes 1-2 minutes. The server starts accepting requests immediately; the frontend retries on 503.
- **Thread safety.** `BoundedCache` uses `threading.Lock`. Seeding runs in a daemon thread. Pipeline uses `ThreadPoolExecutor` for concurrent S3 fetches.

## File Structure

```
weather_radar/
├── CLAUDE.md              ← this file
├── .env                   ← API keys (not committed)
├── .env.example           ← template for .env
├── .gitignore
├── backend/
│   ├── requirements.txt   ← production dependencies
│   ├── requirements-dev.txt ← dev/test dependencies
│   ├── main.py            ← FastAPI app, routes, lifespan
│   ├── pipeline.py        ← data pipeline: fetch tilts → composite + voxels
│   ├── mrms.py            ← S3 client, GRIB2 decode, clip to bbox
│   ├── cache.py           ← BoundedCache (in-memory, thread-safe)
│   ├── disk_cache.py      ← on-disk raw + decoded cache, per-tilt layout
│   ├── render.py          ← grid-to-PNG rendering (NWS color scale)
│   └── grib2/
│       ├── __init__.py
│       ├── decoder.py     ← entry point: bytes → (metadata, numpy array)
│       ├── sections.py    ← section-level parsing (0–7)
│       ├── packing.py     ← simple, J2K, PNG unpackers
│       └── bitstream.py   ← N-bit integer reader utility
├── frontend/
│   ├── index.html         ← single-page app
│   ├── style.css          ← dark theme, glassmorphism panels
│   ├── app.js             ← map init, animation, 2D/3D rendering
│   └── colors.js          ← NWS color scale + legend builder
├── tests/
│   ├── __init__.py
│   ├── fixtures/          ← real MRMS .grib2.gz file (gitignored)
│   └── test_decoder.py    ← shape, bounds, value range, clip tests
├── scripts/
│   └── test_fetch.py      ← standalone download + decode diagnostic
└── data/                  ← runtime cache (gitignored)
    ├── raw/{tilt}/        ← raw .grib2.gz files from S3
    └── decoded/{tilt}/    ← .npy + .json per decoded frame
```

## Running

```bash
# Install dependencies
pip install -r backend/requirements.txt

# Set up API keys (optional — app works without them)
cp .env.example .env
# Edit .env to add STADIA_API_KEY or MAPTILER_API_KEY

# Start the server (from project root)
uvicorn backend.main:app

# Open frontend (served separately, or just open the file)
open frontend/index.html
```

The server seeds 60 frames on startup in a background thread. The frontend auto-retries until frames are available (~1-2 minutes).

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
   - Serve motion vectors alongside frame data in `GET /api/radar/frames`

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
- One fetch path gives both 2D and 3D output — `nanmax` across tilts produces an equivalent composite
- Avoids maintaining two separate S3 listing/download paths
- 3D volume data was the eventual goal anyway

### Why PointCloudLayer instead of ColumnLayer

deck.gl `PointCloudLayer` turned out to work better than `ColumnLayer` for volumetric radar:
- Each voxel is a single point with a color — simpler data format (just `[lon, lat, alt, dbz]`)
- Handles irregular sampling (not all cells are active at all levels)
- Performs well with 100K+ points
- Vertical exaggeration is just a multiplier on the altitude

### Bounding box size

Expanded from the original tight NYC box (40.4–41.0°N, 74.3–73.6°W) to a wider region (39.44–42.0°N, 75.23–72.67°W) to show enough surrounding area to see weather systems approaching. This roughly covers from Trenton to Hartford, Sandy Hook to the eastern tip of Long Island.

### On-disk cache layout

Originally used a flat `data/grib2_cache/` directory. Migrated to per-tilt subdirectories (`data/raw/00.50/`, `data/decoded/00.50/`, etc.) once we started fetching 8 tilt levels — the flat layout became unmanageable at ~1000+ files. The `disk_cache.py` module includes legacy migration logic.

### Background seeding

The server starts accepting HTTP requests immediately. Frame seeding (480 S3 fetches for 60 timestamps × 8 tilts) runs in a daemon thread. The frontend handles the 503→retry loop transparently. This avoids a long startup delay while still serving a full frame history once warmed.
