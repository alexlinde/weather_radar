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

The radar starts paused on the latest frame. Use the playback controls to animate through time — storms slide smoothly between keyframes via motion-compensated interpolation.

## Responsive layout

The UI adapts to screen size:

- **Desktop** — control panel (top-right), animation bar (bottom-center), legend toggle (bottom-right).
- **Mobile** — animation bar stretches full-width with a hamburger button that opens controls as a popup. Legend toggle moves to top-right.

## Embedding

The radar auto-detects when it's running inside a React Native WebView or iframe and activates a postMessage bridge for programmatic control. No special URL parameter is needed — just load the base URL.

For a stripped-down view where the host provides its own controls:

```
http://localhost:8000/?controls=minimal
```

See [INTEGRATION.md](INTEGRATION.md) for the full React Native spec, postMessage API, and reference implementation.

## Data source

All radar data comes from the [NOAA MRMS](https://registry.opendata.aws/noaa-mrms-pds/) public S3 bucket (`noaa-mrms-pds`). No credentials or API keys are required for radar data access.
