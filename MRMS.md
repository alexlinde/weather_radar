# MRMS Data Source & Pipeline

## Data Source: MRMS on AWS

**Bucket:** `noaa-mrms-pds` (public, no credentials needed)
**Region:** `us-east-1`
**Docs:** https://registry.opendata.aws/noaa-mrms-pds/

### Data path

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

### Sentinel value handling

MRMS uses large negative values (e.g., -999, -99) as sentinels for missing/no-data. These are masked to NaN after decoding:
```python
data[data < -30.0] = np.nan
```

The -30 dBZ threshold preserves legitimate weak reflectivity values while catching all known sentinel patterns.

### MRMS data gotchas

- **Files are gzipped.** Decompress in memory with `gzip.decompress()` before passing to the decoder.
- **Bucket prefix is uppercase.** Use `CONUS/MergedReflectivityQC_{tilt}/` not `conus/`.
- **Day boundary spanning.** List files from today, yesterday, and 2 days ago to handle UTC boundaries.
- **Data freshness.** Files appear in S3 with ~2-3 min latency. The latest file may be 2-5 min old.
- **File size.** A single CONUS tilt frame is ~0.5 MB compressed. The full CONUS grid is 3500×7000 at 0.01° resolution.
- **Data sparsity.** 95-99% of CONUS grid cells are NaN/sentinel. This is why scipy.sparse is so effective.
- **CORS.** S3 bucket does not serve CORS headers. All data flows through the backend.
- **No credentials.** S3 access uses unsigned requests (`botocore.UNSIGNED`).
- **Scanning direction.** MRMS grids scan left→right, top→bottom (NW corner first). The decoder checks scanning mode flags and flips the array if needed.
- **Sparse zero = NaN.** When converting to CSR, NaN becomes implicit zero. When reading back for rendering, explicit zeros are treated as NaN (no echo). This is valid because dBZ < -30 is already masked.

## Data Pipeline (`pipeline.py`)

The pipeline operates on a single principle: **fetch tilt-level data once, store as sparse matrices, derive atlas tiles on demand.**

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

1. **Incremental fetch** — lists the 20 most recent S3 keys, skips any already on disk, fetches/decodes/caches only truly new frames. Retries once (3s delay) on S3KeyNotFound for files still propagating.
2. **Atlas pre-render** — renders z=4 and z=5 atlas tiles for newly fetched frames
3. **Motion computation** — computes motion fields for any new consecutive frame pairs
4. **Purge** — removes disk entries older than 3 hours and trims to a max of 60 frames

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

## Atlas Tile Rendering

Atlas tiles are rendered on demand from sparse tilt grids. For each tile:

1. Compute geographic overlap between the tile bounds and the MRMS grid
2. For each of the 8 tilt levels, extract the sparse subgrid, convert to dense, resample to 256×256 (nearest-neighbor)
3. Encode dBZ to uint8: `round((dBZ + 30) * 2)`, NaN/zero → 0
4. Stack 8 bands vertically into a 256×2048 array
5. Encode as grayscale PNG via Pillow

Each atlas tile is a **256×2048 grayscale PNG** — 8 vertical bands of 256×256, one per tilt level:

- Row 0–255: tilt 00.50° (1 km)
- Row 256–511: tilt 01.00° (1.5 km)
- Row 512–767: tilt 01.50° (2 km)
- ...
- Row 1792–2047: tilt 19.00° (19 km)

Pixel encoding: `uint8 = round((dBZ + 30) * 2)`, mapping dBZ range [-30, +97.5] to [0, 255]. Value 0 = no echo. The GPU shader reverses this: `dBZ = pixel / 2.0 - 30.0`.

The resulting PNG is typically 2-10 KB per tile. All colorisation, compositing, and smoothing happens on the GPU via GLSL shaders.

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

The GLSL shader uses a dBZ-proportional alpha curve shaped by the opacity slider.

## Motion-Compensated Interpolation (`motion.py`)

Between consecutive 2-minute radar frames, a simple crossfade causes storms to ghost/pulse. Motion-compensated interpolation fixes this by sliding storm cells along their displacement vectors.

### Backend: FFT block matching

`compute_motion_field()` in `motion.py`:
1. Build 2D composite via `fmax` across 8 tilt levels
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

Value 128 in R/G = zero displacement. One global CONUS-wide PNG per frame pair (~2-5 KB).

### Frontend: Semi-Lagrangian advection shader

The GLSL fragment shader's `getMotion()` function:
1. Converts tile UV → geographic lon/lat (via Mercator inverse)
2. Maps lon/lat → motion texture UV using the CONUS grid bounds
3. Samples the motion PNG to get (U, V, confidence)
4. Converts displacement from degrees to tile UV space (with latitude-dependent Mercator correction)

The `sampleInterp()` function performs semi-Lagrangian advection:
- Traces backward from the current position by `α * displacement` to sample frame A
- Traces forward by `(1-α) * displacement` to sample frame B
- Blends via `mix(sA, sB, α)` for the advected result
- Returns `mix(crossfade, advected, confidence)` — confidence-weighted blend

Fallback: if motion data is unavailable, the shader reverts to plain crossfade. The animation never stalls waiting for motion data.

## Gap-Aware Animation

MRMS data has inherent gaps — the ~2-minute cadence is nominal, not guaranteed. Upstream causes include radar scan cycle timing, ingestion latency from the ~180 WSR-88D sites, product generation failures, and individual radar outages.

The `/timestamps` endpoint computes the median inter-frame cadence and annotates each entry with `gap_before_s` and `is_gap` (flagged when the gap exceeds 1.5× the median cadence).

The frontend uses this to:
1. **Weight animation timing** — per-frame weight array proportional to gap duration (capped at 3×), so transitions spanning larger gaps play proportionally slower.
2. **Show gap markers on the scrubber** — amber tick marks at gap positions with tooltip showing gap duration.
3. **Annotate the frame time display** — shows e.g. "14:08 (+4m)" on gap frames.

## Virtual Volumes (Tilt Carry-Forward)

MRMS composites are generated every ~2 minutes, but the underlying WSR-88D radars take ~4-6 minutes for a full volume scan. The lower tilts (00.50, 01.00) are scanned first and published fastest; higher tilts (07.00, 10.00, 19.00) only appear when the volume scan has progressed far enough. This means most 2-minute frames only contain 2-4 of the 8 tilt levels natively.

In composite mode this is invisible (fmax ignores empty bands), but in 3D mode each tilt is rendered on its own plane — missing tilts cause layers to blink in and out as you scrub through frames.

### Approach: WDSS-II virtual volumes

Following the approach used by NOAA's WDSS-II system (Lakshmanan et al. 2002), we build "virtual volumes" by carrying forward the most recent data for each tilt level. Rather than confining data to a single volume scan, each frame inherits the latest available data at every elevation angle.

Implementation in `pipeline.py`:

1. **`_fill_from_recent()`** — after decoding a frame's native tilts, walks backward through the disk cache to find prior frames that have the missing tilts. Copies the sparse CSR grids forward.
2. **Chain-aware staleness tracking** — if a prior frame's tilt was itself carried forward, the true origin timestamp is used for the age check, preventing stale data from propagating through long chains.
3. **10-minute staleness cap** (`MAX_CARRY_FORWARD_S = 600`) — tilts older than 10 minutes (5x the nominal cadence) are left empty rather than showing badly stale data. Handles radar maintenance outages.
4. **Backfill pass** — `backfill_virtual_volumes()` runs after seeding/warming to fill missing tilts in existing cached data, then updates both disk and in-memory caches.

### Provenance tracking

Each frame's `meta.json` includes a `tilt_sources` dict:

```json
{
  "tilt_sources": {
    "00.50": {"origin": "native"},
    "01.00": {"origin": "native"},
    "01.50": {"origin": "carried_forward", "from": "2026-04-15T16:08:41Z", "age_s": 120},
    "19.00": {"origin": "missing"}
  }
}
```

The `/timestamps` API exposes `native_tilts` (count of natively fetched tilts) and `total_tilts` (native + carried forward, excluding missing) per frame.

## Design Decisions

### Why tilt-level data instead of the pre-computed composite

One fetch path gives both 2D and 3D output — `fmax` across tilts produces an equivalent composite. Avoids maintaining two separate S3 listing/download paths.

### Why atlas tiles with GPU-side rendering (not PointCloudLayer)

The earlier approach used deck.gl `PointCloudLayer` which showed a "dot grid" at CONUS zoom and had no temporal interpolation. The atlas tile approach uses 256×2048 grayscale PNGs (~2-10 KB each) with three.js overlay and GLSL shaders for GPU-side dBZ decoding, color ramp, motion advection, and volume rendering. Browser HTTP cache (`Cache-Control: immutable`) makes revisited frames instant.

### Why FFT block matching for motion

Dense optical flow (Farnebäck) is too slow on CONUS-scale grids and requires OpenCV. FFT block matching is fast (~0.5-1s per pair), uses existing scipy/numpy, and produces a low-resolution motion field that's naturally smooth. The confidence output enables graceful fallback to crossfade.

### Deferred tile rebuild to prevent zoom flicker

When zooming, `updateVisibleTiles()` stores the new tile set as `_pendingTiles` instead of rebuilding immediately. Old meshes keep rendering. Once ALL pending tile textures are cached, they swap atomically. A 3-second safety timeout force-applies if some tiles fail. Never use `anyTextured` for the stale purge condition — it causes partial tile visibility.
