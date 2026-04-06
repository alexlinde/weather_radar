# Weather Radar

A web-based weather radar viewer that renders live MRMS (Multi-Radar Multi-Sensor) reflectivity data from NOAA over an interactive map. Supports composite 2D, stacked 3D, and volumetric ray-marched views with smooth motion-compensated animation between 2-minute radar frames.

![Python](https://img.shields.io/badge/python-3.11+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Quickstart

**Prerequisites:** Python 3.11+ and pip.

```bash
# Clone and enter the repo
git clone <repo-url>
cd weather_radar

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r backend/requirements.txt

# Start the server
uvicorn backend.main:app
```

Open **http://localhost:8000** in your browser.

On first launch the server seeds ~60 radar frames from NOAA's public S3 bucket in the background (no AWS credentials needed). The page will show a loading state for 1–2 minutes while data arrives, then the radar overlay appears automatically.

### Dev mode (offline)

If you've already run the server once and have cached data in `data/`, you can skip S3 fetching:

```bash
DEV_MODE=1 uvicorn backend.main:app
```

Frames load from disk in about a second.

### Optional: nicer base map

The app works out of the box with [OpenFreeMap](https://openfreemap.org) (no API key). For richer terrain styling, copy the env template and add a key:

```bash
cp .env.example .env
# Edit .env — add a Stadia Maps or MapTiler key
```

## What you're looking at

- **Composite** — maximum reflectivity across all elevation angles, projected onto the ground. The classic radar view.
- **3D** — eight tilt levels rendered as stacked translucent planes at their physical altitudes.
- **Volume** — GPU ray-marched volumetric rendering through all tilt levels.

Use the playback controls to animate through time. Storms slide smoothly between keyframes via motion-compensated interpolation.

## Project structure

```
backend/          Python/FastAPI server — fetches, decodes, and serves radar data
  grib2/          Custom minimal GRIB2 decoder (no eccodes dependency)
  main.py         API endpoints
  pipeline.py     Data pipeline: S3 → sparse grids → atlas tiles → motion fields
  motion.py       FFT block matching for inter-frame displacement
  tiles.py        TMS atlas tile rendering
frontend/         Browser app — vanilla JS, MapLibre GL + three.js
  index.html      Single-page app entry point
  app.js          Map init, animation loop, UI wiring
  radar-layer.js  three.js overlay with GLSL shaders
  colors.js       NWS reflectivity color scale
tests/            Decoder tests
```

## Running tests

```bash
# Download a test fixture (one-time)
python scripts/test_fetch.py

# Run tests
pip install -r backend/requirements-dev.txt
python -m pytest tests/ -v
```

## Data source

All radar data comes from the [NOAA MRMS](https://registry.opendata.aws/noaa-mrms-pds/) public S3 bucket (`noaa-mrms-pds`). No credentials or API keys are required for radar data access.
