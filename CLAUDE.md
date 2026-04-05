# CLAUDE.md — NYC Weather Radar Prototype

## What we're building

A web-based weather radar viewer that pulls live MRMS (Multi-Radar Multi-Sensor) data from NOAA's public AWS bucket and renders it over a map of New York City. The goal is to show real radar data the way a meteorologist would see it — full reflectivity at multiple vertical levels, with smooth animation — on top of a base map that gives geographic context (terrain, water, urban areas).

This is a working prototype, not a production app. Prioritise getting real data on screen over polish.

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
- Fetch latest MRMS GRIB2 files from S3
- Decode with custom minimal GRIB2 decoder (see below)
- Clip to NYC bounding box
- Serve radar data as JSON (for deck.gl layers) or PNG tiles (for image overlay)
- Cache aggressively — MRMS updates every 2 minutes, no need to re-fetch more often

### Frontend (HTML + JS)

Responsibilities:
- Render base map with MapLibre GL JS
- Overlay radar data using deck.gl layers
- Controls for threshold, opacity, vertical level selection
- Time animation with frame interpolation

## Data Source: MRMS on AWS

**Bucket:** `noaa-mrms-pds` (public, no credentials needed)
**Region:** `us-east-1`
**Docs:** https://registry.opendata.aws/noaa-mrms-pds/

### Key products to use

**Composite reflectivity (2D — start here):**
```
s3://noaa-mrms-pds/conus/MergedReflectivityQCComposite/
```
Single-level maximum reflectivity. Good for the initial 2D map overlay. Files are gzipped GRIB2.

**3D reflectivity (multi-level — phase 2):**
```
s3://noaa-mrms-pds/conus/MergedReflectivityQC/
```
This product is available at 33 vertical tilt levels (00.50° through 19.50°). Each level is a separate GRIB2 file. For a 3D view, fetch several levels and stack them.

### File naming convention
```
MRMS_MergedReflectivityQCComposite_00.50_YYYYMMDD-HHmmSS.grib2.gz
```

### How to list available files
Use an HTTP GET to the S3 bucket's listing endpoint. Or use boto3 with no credentials:

```python
import boto3
from botocore import UNSIGNED
from botocore.config import Config

s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))

# List latest composite reflectivity files
response = s3.list_objects_v2(
    Bucket='noaa-mrms-pds',
    Prefix='conus/MergedReflectivityQCComposite/',
    Delimiter='/'
)
```

Note: The bucket uses a `latest/` prefix pattern for some products. Explore the bucket structure first — list prefixes under `conus/` to understand the layout.

### NYC bounding box for clipping
```
North: 41.0
South: 40.4
East:  -73.6
West:  -74.3
```

This covers all five boroughs, parts of NJ, Long Island, and Westchester. Generous enough to see approaching weather.

## Base Map

Use **MapLibre GL JS** as the map renderer (open source, no vendor lock-in).

For tile sources, try these in order of preference:

### Option 1: Stadia Maps — Stamen Terrain (recommended)
Best visual fit for this use case. Shows elevation shading, water bodies, and urban context.
- Requires a free API key from https://stadiamaps.com
- Style URL: `https://tiles.stadiamaps.com/styles/stamen_terrain.json?api_key={key}`
- Free tier is generous (200k tiles/month)

### Option 2: MapTiler Outdoor
Good terrain + trail/urban detail.
- Free API key from https://www.maptiler.com
- Style URL: `https://api.maptiler.com/maps/outdoor-v2/style.json?key={key}`

### Option 3: OpenFreeMap (no key needed)
Zero-config fallback. Less visual detail but works immediately.
- Style URL: `https://tiles.openfreemap.org/styles/liberty`

**Store the API key in a `.env` file, never commit it.**

### Map initial view
```javascript
center: [-73.98, 40.75],  // Midtown Manhattan
zoom: 10,                  // Shows all 5 boroughs + surroundings
pitch: 0,                  // Start flat for Phase 1; increase to ~45 in Phase 3 when terrain is enabled
bearing: 0
```

## Tech Stack

### Backend
- **Python 3.11+**
- **FastAPI** — API server
- **numpy** — array operations, clipping, resampling
- **boto3** — S3 access (unsigned requests, no credentials needed)
- **Pillow** — PNG tile generation + JPEG2000 decoding (ships with openjpeg)
- **uvicorn** — ASGI server

No eccodes, no cfgrib, no system-level C dependencies. See "Custom GRIB2 Decoder" section below.

### Frontend
- **MapLibre GL JS** (~v4) — base map rendering
- **deck.gl** (~v9) — GPU-accelerated data layers on top of MapLibre
- **Vanilla JS or lightweight bundler** — keep it simple for a prototype, no React needed

Load MapLibre and deck.gl from CDN for prototype speed:
```html
<script src="https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.css" rel="stylesheet" />
<script src="https://unpkg.com/deck.gl@9/dist.min.js"></script>
```

## Build Plan

### Phase 1: Get data on screen (start here)

1. **Backend: Fetch + decode MRMS composite reflectivity**
   - Write a Python script that downloads the latest composite reflectivity GRIB2 from S3
   - Decode with the custom GRIB2 decoder (see section below)
   - Clip to the NYC bounding box
   - Print the array shape, lat/lon bounds, min/max values to verify

2. **Backend: Serve as API**
   - FastAPI endpoint `GET /api/radar/latest` returns JSON:
     ```json
     {
       "timestamp": "2026-04-05T18:02:00Z",
       "bounds": { "north": 41.0, "south": 40.4, "east": -73.6, "west": -74.3 },
       "grid": { "rows": <int>, "cols": <int> },
       "data": [[<dbz values>]]  
     }
     ```
   - Also support `GET /api/radar/image` that returns a PNG with transparent background, NWS reflectivity color scale applied. This is a fallback if the JSON payload is too large for smooth rendering.

3. **Frontend: Base map + radar overlay**
   - Initialise MapLibre with the chosen base map style
   - Fetch radar data from the API
   - Render using a deck.gl `BitmapLayer` (if PNG) or `GridCellLayer` (if JSON grid)
   - Apply NWS reflectivity color scale
   - Add opacity slider

### Phase 2: Time animation

4. **Backend: Serve recent frames**
   - Endpoint `GET /api/radar/frames?count=10` returns the last N frames (timestamps + data)
   - Cache decoded frames in memory

5. **Frontend: Animation loop**
   - Fetch a batch of frames
   - Animate through them on a timer
   - Linear interpolation between frames as a baseline (alpha blend)
   - Play/pause control, speed control, scrub slider

### Phase 3: 3D terrain + vertical levels

6. **Frontend: Enable 3D terrain**
   - Add a raster-DEM source to MapLibre using AWS Terrain Tiles (free, no key):
     ```javascript
     map.addSource('terrain', {
       type: 'raster-dem',
       tiles: ['https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'],
       encoding: 'terrarium',
       tileSize: 256,
     });
     map.setTerrain({ source: 'terrain', exaggeration: 1.5 });
     ```
   - Set an initial pitch (e.g. 45°) so terrain is visible
   - This gives the user the Palisades, Hudson valley, harbor bathymetry, and LI topography for free
   - Test that the 2D radar overlay from Phase 1 still renders correctly when draped over terrain

7. **Backend: Multi-level data**
   - Fetch MergedReflectivityQC at multiple tilt angles
   - Stack into a 3D array, serve as JSON or binary
   - Endpoint `GET /api/radar/volume`

8. **Frontend: 3D radar visualisation**
   - Use deck.gl `ColumnLayer` or custom layer to render vertical columns above the terrain
   - Or render stacked translucent planes as explored in earlier prototypes
   - Radar volumes should float above the terrain surface, not clip through it
   - Add vertical level selector / cross-section tool

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
    - This replaces the naive alpha-blend from Phase 2
    - Storms should slide smoothly across the map instead of ghosting/doubling
    - Fall back to crossfade locally where the motion field has high residual error (cell splits, new development, decay)

## NWS Reflectivity Color Scale

Use this standard palette for mapping dBZ to RGBA. The alpha channel should be 0 for values below 5 dBZ (no echo).

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

## Custom GRIB2 Decoder

**Do not use eccodes, cfgrib, pygrib, or wgrib2.** These all depend on the eccodes C library, which is ~100MB+, has painful cross-platform installation, and is massive overkill for our use case. We only need to decode one product family (MRMS reflectivity) which uses a narrow, predictable subset of the GRIB2 spec.

### GRIB2 structure (what we need to parse)

A GRIB2 file is a sequence of numbered sections. Each section starts with a 4-byte length and a 1-byte section number:

| Section | Name | What we extract |
|---------|------|-----------------|
| 0 | Indicator | Magic bytes `GRIB`, edition (must be 2), total message length |
| 1 | Identification | Reference time (year, month, day, hour, minute, second) |
| 2 | Local Use | Skip (optional, MRMS may not include it) |
| 3 | Grid Definition | Grid template, Ni (cols), Nj (rows), lat/lon of first and last grid point, resolution |
| 4 | Product Definition | Parameter category, parameter number, level type, level value |
| 5 | Data Representation | Packing template number, reference value, binary scale, decimal scale, bits per value |
| 6 | Bitmap | Bitmap presence indicator — if present, a bitmask of valid data points |
| 7 | Data | Packed data values |
| 8 | End | Magic bytes `7777` |

All multi-byte integers are big-endian. Use Python's `struct` module.

### Grid Definition (Section 3)

MRMS uses **Template 3.0** (regular lat/lon grid). Key fields at known byte offsets within the section:
- Bytes 31–34: Ni (number of points along a parallel = columns)
- Bytes 35–38: Nj (number of points along a meridian = rows)  
- Bytes 47–50: La1 (latitude of first grid point, microdegrees, signed)
- Bytes 51–54: Lo1 (longitude of first grid point, microdegrees, unsigned or signed)
- Bytes 56–59: La2 (latitude of last grid point)
- Bytes 60–63: Lo2 (longitude of last grid point)
- Bytes 64–67: Di (i-direction increment, microdegrees)
- Bytes 68–71: Dj (j-direction increment, microdegrees)

Latitudes/longitudes are stored as scaled integers (multiply by 10⁻⁶ to get degrees). Check whether scanning direction is N→S or S→N (byte 72, scanning mode flags).

### Data Representation (Section 5)

MRMS products typically use one of these packing templates:

**Template 5.0 — Simple Packing (most common for reflectivity)**
- Reference value R (IEEE 754 float, bytes 12–15)
- Binary scale factor E (signed 16-bit, bytes 16–17)
- Decimal scale factor D (signed 16-bit, bytes 18–19)
- Number of bits per packed value (byte 20)
- Decode: `value = (R + packed_int * 2^E) / 10^D`
- Unpack the data section as a bitstream of N-bit unsigned integers

**Template 5.40 — JPEG2000 Packing**
- Same R, E, D header fields
- Data section (7) contains a raw JPEG2000 codestream
- Decode the J2K with Pillow: `Image.open(BytesIO(j2k_bytes))` → numpy array of packed integers
- Then apply the same `(R + packed * 2^E) / 10^D` formula
- Pillow ships with openjpeg, so this requires no additional C library

**Template 5.41 — PNG Packing**
- Same structure as 5.40 but data is a PNG image
- Decode with Pillow, same formula

### Bitmap (Section 6)

Byte 6 of Section 6 is the bitmap indicator:
- Value 255 = no bitmap, all grid points have data
- Value 0 = bitmap follows, one bit per grid point (1 = data present, 0 = missing)

If a bitmap is present, only grid points where the bitmap bit is 1 have corresponding packed values in Section 7. Expand the sparse data back to the full grid, filling missing points with NaN or a sentinel.

### Implementation approach

```
backend/
├── grib2/
│   ├── __init__.py
│   ├── decoder.py      ← Main entry point: bytes in → (metadata_dict, numpy_array) out
│   ├── sections.py     ← Parse each section, return structured dicts
│   ├── packing.py      ← Unpack simple, J2K, and PNG templates
│   └── bitstream.py    ← Utility for reading N-bit packed integers from a byte buffer
```

The decoder should:
1. Validate the GRIB magic and edition number
2. Walk sections by reading length + section number at each boundary
3. Parse sections 3 (grid) and 5 (packing params) into dicts
4. Decode section 7 using the appropriate unpacker
5. Apply the scaling formula
6. Reshape to (Nj, Ni) and handle scanning direction
7. Return metadata (timestamp, grid bounds, resolution) + a numpy 2D array of dBZ values

**Write tests for the decoder immediately** — download a single MRMS file to `tests/fixtures/` and verify that the decoded output has the expected shape, bounds, and value range. If possible, cross-validate against a known tool (e.g., run `wgrib2 -V` on the same file to get expected metadata, even if we don't use wgrib2 in production).

### What we do NOT need to handle
- Grid templates other than 3.0 (rotated, polar stereographic, etc.)
- Packing templates other than 5.0, 5.40, 5.41
- Multiple messages per file (MRMS uses one message per file)
- Complex/second-order packing (Template 5.2, 5.3)
- Spectral data, ensemble metadata, or any other exotic GRIB2 features

If we encounter an unsupported template, fail loudly with a clear error message identifying the template number, rather than producing garbage output.

## Key Constraints & Gotchas

- **MRMS files are gzipped.** Decompress in memory with `gzip.decompress()` before passing to the decoder.
- **Coordinate system.** MRMS uses lat/lon on a regular grid (not projected). MapLibre uses EPSG:4326 / Web Mercator. The data will need to be mapped to pixel coordinates when rendering.
- **Data freshness.** Files appear in S3 with ~2-3 min latency from observation time. The latest file might be 2-5 minutes old. That's fine.
- **File size.** A single CONUS composite reflectivity frame is ~2-3 MB compressed. The NYC clip will be tiny (a few KB).
- **CORS.** The S3 bucket may not serve CORS headers for browser-direct access. Fetch from the backend, not the browser.
- **No credentials.** Access the S3 bucket with unsigned requests. Do not configure or require AWS credentials.
- **Terrain tiles.** The AWS elevation tiles (`elevation-tiles-prod`) are also public and free. No key needed.
- **Scanning direction.** MRMS grids typically scan left→right, top→bottom (NW corner is first data point). Verify this by checking the scanning mode flags in Section 3 byte 72. If bit 2 is set, rows go S→N and the array needs flipping.

## File Structure

```
mrms-radar-prototype/
├── CLAUDE.md              ← this file
├── .env                   ← API keys (not committed)
├── .gitignore
├── backend/
│   ├── requirements.txt
│   ├── main.py            ← FastAPI app
│   ├── mrms.py            ← MRMS fetch/clip logic, uses grib2 decoder
│   ├── cache.py           ← Simple in-memory frame cache
│   └── grib2/
│       ├── __init__.py
│       ├── decoder.py     ← Entry point: bytes → (metadata, numpy array)
│       ├── sections.py    ← Section-level parsing
│       ├── packing.py     ← Simple, J2K, PNG unpackers
│       └── bitstream.py   ← N-bit integer reader utility
├── frontend/
│   ├── index.html         ← Single-page app
│   ├── style.css
│   ├── app.js             ← Map init, radar overlay, controls
│   └── colors.js          ← NWS color scale utilities
├── tests/
│   ├── fixtures/          ← One real MRMS GRIB2 file for testing
│   └── test_decoder.py    ← Verify shape, bounds, value range
└── scripts/
    └── test_fetch.py      ← Standalone script to test MRMS data download + decode
```

## Definition of Done (Phase 1)

- [ ] `tests/fixtures/` contains a real MRMS GRIB2 file
- [ ] `python -m pytest tests/test_decoder.py` passes — decoder produces correct shape, bounds, and value range
- [ ] Running `python scripts/test_fetch.py` downloads and decodes a live MRMS frame, prints shape and value range
- [ ] `uvicorn backend.main:app` starts the API server
- [ ] `GET /api/radar/latest` returns real clipped radar data for NYC
- [ ] Opening `frontend/index.html` shows a map of NYC with live radar reflectivity overlaid
- [ ] Radar uses the NWS color scale and has an opacity control
- [ ] Areas with no radar echo (< 5 dBZ) are fully transparent
- [ ] The map base layer clearly shows water (Hudson, East River, harbour), land, and urban context

## Definition of Done (Phase 3)

- [ ] Map renders with 3D terrain — tilting the map shows elevation in the NYC region
- [ ] Radar overlay drapes correctly over terrain without clipping through hills
- [ ] Multi-level reflectivity data renders as 3D volumes above the terrain surface
