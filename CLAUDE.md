# CLAUDE.md — Weather Radar Prototype

## What we're building

A web-based weather radar viewer that pulls live MRMS data from NOAA's public AWS bucket and renders it over a map of the continental US. Full reflectivity at 8 vertical levels, smooth motion-compensated animation, three view modes (composite, 3D stacked, volume ray-marched).

Working prototype, not a production app. Prioritise getting real data on screen over polish.

## Architecture

```
NOAA S3 (noaa-mrms-pds) → Python Backend (FastAPI) → Browser Frontend (MapLibre + three.js WebGL)
```

- **Backend:** Fetches MRMS tilt-level GRIB2, decodes with custom decoder (no eccodes), stores as scipy.sparse CSR, serves 256×2048 grayscale PNG atlas tiles + motion vector PNGs via TMS endpoints
- **Frontend:** MapLibre base map + three.js overlay with GLSL shaders for GPU-side dBZ decoding, NWS color ramp, motion-compensated advection, volume rendering. ES modules bundled by esbuild for production.

## Tech Stack

**Backend:** Python 3.11+, FastAPI, numpy, scipy (sparse CSR), boto3 (unsigned S3), Pillow, uvicorn
**Frontend:** MapLibre GL JS ~v4, three.js ~v0.160, vanilla JS ES modules, esbuild
**No eccodes, no cfgrib, no framework, no JSX.** MapLibre and three.js loaded from CDN.

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/radar/atlas/{timestamp}/{z}/{x}/{y}.png` | GET | Atlas tile — 256×2048 grayscale PNG, 8 tilt bands. z=3–8. |
| `/api/radar/motion/{timestamp}.png` | GET | Motion vector field — RGB PNG (U, V, confidence). |
| `/api/radar/timestamps` | GET | Available timestamps with gap annotations, motion flags, motion config. |
| `/api/radar/refresh` | GET | Force cache invalidation + re-seed. |
| `/api/config` | GET | Frontend configuration (map tile API keys). |
| `/health` | GET | Cache status (in-memory + on-disk counts). |

`/timestamps` returns 503 while seeding; frontend retries automatically. Each entry includes `gap_before_s`, `is_gap`, and `has_motion`. Response includes `gap_info` with `expected_cadence_s`. Atlas tiles use `Cache-Control: immutable`.

## File Structure

```
weather_radar/
├── CLAUDE.md              ← this file (core project context)
├── MRMS.md                ← MRMS data source, pipeline, caching, rendering, motion, design decisions
├── GRIB2.md               ← custom GRIB2 decoder spec (templates, byte offsets, gotchas)
├── INTEGRATION.md         ← React Native integration spec (WebView + postMessage API)
├── CLOUD.md               ← Google Cloud deployment setup
├── .env / .env.example    ← API keys
├── backend/
│   ├── main.py            ← FastAPI app, endpoints, gap analysis
│   ├── pipeline.py        ← data pipeline: S3 → GRIB2 → sparse CSR → atlas + motion
│   ├── motion.py          ← FFT block matching, motion field computation + PNG encoding
│   ├── tiles.py           ← TMS tile math, atlas tile rendering, tile cache
│   ├── mrms.py            ← S3 client, file listing, download
│   ├── cache.py           ← ConusTiltCache (sparse LRU with disk fallback)
│   ├── disk_cache.py      ← on-disk raw + sparse + motion cache
│   ├── render.py          ← NWS color scale constants
│   └── grib2/             ← custom GRIB2 decoder (decoder.py, sections.py, packing.py, bitstream.py)
├── frontend/
│   ├── app.js             ← entry point: map init, responsive UI, WebView auto-detect
│   ├── radar-engine.js    ← RadarEngine: animation, timestamps, presets, gap-aware timing
│   ├── radar-layer.js     ← RadarLayer: three.js overlay, GLSL shaders, tile/motion caches
│   ├── radar-bridge.js    ← postMessage bridge for RN WebView / iframe
│   ├── colors.js          ← NWS color scale, legend builder, GPU ramp data
│   ├── index.html / style.css
│   ├── package.json       ← esbuild build/watch scripts
│   └── dist/              ← production build output (gitignored)
├── tests/                 ← decoder tests + fixtures
├── scripts/               ← diagnostic scripts
└── data/                  ← runtime cache (gitignored)
```

## Running

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app               # serves API + frontend on :8000
DEV_MODE=1 uvicorn backend.main:app    # skip S3, load from disk cache

# Optional: build minified frontend
cd frontend && npm install && npm run build && cd ..

# Tests
python scripts/test_fetch.py           # download a test fixture
pip install -r backend/requirements-dev.txt
python -m pytest tests/test_decoder.py -v
```

The server seeds 60 frames on startup in a background thread (~1-2 min), then refreshes every 60s. Frontend auto-retries until frames are available. In `DEV_MODE`, frames load from disk cache in ~1s.

## Key Constraints

- **Coordinate system:** MRMS uses lat/lon on a regular 0.01° grid (3500×7000). MapLibre handles EPSG:4326.
- **Startup time:** Seeding 480 S3 files takes 1-2 min. Server accepts requests immediately; frontend retries on 503.
- **Thread safety:** `ConusTiltCache` uses `threading.Lock`. Seeding runs in a daemon thread. Pipeline uses `ThreadPoolExecutor`.
- **No CORS from S3:** All data flows through the backend.
- **Terrain tiles:** AWS `elevation-tiles-prod` (public, free). 3D terrain currently disabled due to tile seam issue.

## Responsive Layout

- **Desktop (>600px):** control panel top-right, animation bar bottom-center
- **Mobile (≤600px):** full-width animation bar with hamburger for control panel popup
- **`?controls=minimal`:** animation bar only
- **`?controls=none`:** no UI chrome (host app controls via postMessage)
- **Embedded context:** auto-detected via `window.ReactNativeWebView` or `window.parent !== window`

## Deep-Dive Docs

- **[MRMS.md](MRMS.md)** — data source, S3 bucket, pipeline flow, caching architecture, sparse storage, atlas tiles, NWS color scale, motion interpolation, gap-aware animation, design decisions
- **[GRIB2.md](GRIB2.md)** — custom decoder spec: section layout, supported templates, byte offsets, signed integer encoding, bitmap handling
- **[INTEGRATION.md](INTEGRATION.md)** — React Native WebView integration, postMessage API, controls modes
- **[CLOUD.md](CLOUD.md)** — Google Cloud deployment: GCE VM, Docker Compose, Caddy, service accounts, deploy.sh
