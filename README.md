# Weather Radar

A web-based weather radar viewer that renders live MRMS (Multi-Radar Multi-Sensor) reflectivity data from NOAA over an interactive map. Supports composite 2D, stacked 3D, and volumetric ray-marched views with smooth motion-compensated animation between 2-minute radar frames.

![Python](https://img.shields.io/badge/python-3.11+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Quickstart

**Prerequisites:** Python 3.11+ and pip. Node.js 18+ optional (for production build only).

```bash
# Clone and enter the repo
git clone <repo-url>
cd weather_radar

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate

# Install backend dependencies
pip install -r backend/requirements.txt

# (Optional) Build minified frontend
cd frontend && npm install && npm run build && cd ..

# Start the server
uvicorn backend.main:app
```

Open **http://localhost:8000** in your browser.

The frontend build step is optional — without it, the server serves the ES module source files directly (all modern browsers support `<script type="module">`). If you run `npm run build`, it bundles and minifies the JS/CSS (~37 KB) into `frontend/dist/` for production.

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

## Embedding in React Native

The radar supports an embed mode for use inside a React Native WebView:

```
http://localhost:8000/?mode=embed
```

This shows a minimal control bar (view mode, intensity, expand button) with no animation controls or panels — designed for landscape embedding. A postMessage bridge enables the host app to control the radar programmatically and receive state events. See [INTEGRATION.md](INTEGRATION.md) for the full spec and reference implementation.

## Data source

All radar data comes from the [NOAA MRMS](https://registry.opendata.aws/noaa-mrms-pds/) public S3 bucket (`noaa-mrms-pds`). No credentials or API keys are required for radar data access.
