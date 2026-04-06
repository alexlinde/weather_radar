# GR2Analyst-Inspired Radar Interpretation Features

## What we already cover

The app already handles several GR2Analyst concepts: multi-tilt reflectivity, composite (fmax), 3D views, animation, NWS color ramp, dBZ filtering presets, and a legend. The biggest gaps are in **data interrogation** and **situational awareness overlays** — the things that let a user look at the radar and *understand* what the storm is doing.

## Recommended features, in priority order

### 1. Cursor data readout (highest priority)

GR2Analyst's readout bar is always visible at the bottom of the screen, showing the exact dBZ, lat/lon, and altitude under the mouse. This is the single most useful feature for interpretation — without it, the user is guessing from color alone.

**Implementation:** Pure frontend. On `mousemove`, project the cursor to lat/lon (MapLibre's `map.unproject()`), then sample the nearest atlas tile texture at that UV to get the encoded uint8, reverse the formula (`dBZ = pixel / 2.0 - 30.0`). Show a persistent readout bar at the bottom with: lat, lon, dBZ value, and current tilt band (or "composite max").

- Add a `readPixelAt(lngLat)` method to `RadarLayer` that reads from the current frame textures
- Add a fixed readout bar in `frontend/index.html` below the animation bar
- Wire `map.on('mousemove')` in `frontend/app.js` to update it

### 2. Derived products: VIL and Echo Tops

GR2Analyst displays VIL (Vertically Integrated Liquid) and Echo Tops alongside reflectivity. These are the primary tools for assessing storm severity without dual-pol data:

- **VIL** = vertical integral of Z (converted from dBZ) through the column height. High VIL (>40 kg/m^2) indicates severe hail potential.
- **Echo Top** = maximum altitude where reflectivity exceeds 18 dBZ. Deep echoes (>40k ft) indicate strong updrafts.

**Implementation:** We already have all 8 tilt levels in every atlas tile. Two approaches:

- **GPU-side (preferred):** Add new shader modes that operate on the same atlas texture. Instead of `fmax`, the VIL shader converts each band's dBZ to Z (linear), integrates over the tilt height intervals, and maps the result to a VIL color scale. Echo Tops walks bands top-down and returns the height of the first band exceeding 18 dBZ.
- **Backend-side:** Add new tile endpoints (`/api/radar/vil/...`, `/api/radar/echotop/...`) that compute VIL/ET from the sparse grids and render as colored PNG tiles. Simpler but slower and doesn't benefit from the existing atlas pipeline.

GPU-side is better — it reuses the existing tile loading with zero new network requests, and the user can toggle between products instantly.

- Add VIL and Echo Top modes to the View segmented control in `frontend/index.html`
- Add VIL/ET color ramps to `frontend/colors.js` (VIL: green/yellow/red/purple, 0-80 kg/m^2; ET: blue/green/yellow/red, 0-60 kft)
- Add `vilMode` and `echoTopMode` GLSL fragments to `frontend/radar-layer.js`

### 3. NWS warning polygon overlays

GR2Analyst prominently displays active NWS warnings (tornado, severe thunderstorm, flash flood) as colored polygons on the map. This is critical context — the whole point of looking at radar is often to see what's happening relative to warnings.

**Implementation:**

- Fetch active warnings from `https://api.weather.gov/alerts/active?area=US&event=Tornado%20Warning,Severe%20Thunderstorm%20Warning,Flash%20Flood%20Warning` (returns GeoJSON).
- Add as a MapLibre GeoJSON source + fill/line layers with standard NWS colors (red = TOR, orange = SVR, green = FFW).
- Refresh every 60 seconds. Toggle on/off from the control panel.
- Add a small warnings summary badge showing active count.

- Backend: proxy through a new endpoint `/api/warnings` (avoids CORS issues with api.weather.gov)
- Frontend: add a "Warnings" toggle and GeoJSON layer in `frontend/app.js`

### 4. Vertical cross-section tool

GR2Analyst's cross-section lets you draw a line across a storm and see reflectivity in a height-vs-distance plane. This is the primary way to assess storm structure (overhang, bounded weak echo region, etc.).

**Implementation:**

- User clicks two points on the map to define the cross-section line.
- Backend endpoint `/api/radar/cross-section?lat1=&lon1=&lat2=&lon2=&timestamp=` samples the 8 tilt grids along the line, returning a 2D array (distance x height) of dBZ values.
- Frontend renders this in a small overlay canvas using the NWS color ramp.

- Add cross-section sampling logic to backend (interpolate along the line in each tilt's sparse grid)
- Add a draw-line interaction mode and overlay panel to the frontend

### 5. Distance / bearing measurement tool

GR2Analyst has a ruler tool for measuring distances. Simple but frequently used (e.g., "how far is that hook echo from town?").

**Implementation:** Pure frontend. Click two points, show distance (km/mi) and bearing using the Haversine formula. Draw a geodesic line on the map.

- Add a "Measure" button to `frontend/index.html`
- Use `map.on('click')` in a measurement mode to capture points
- Display result in a small tooltip or the readout bar

### 6. County / political boundaries toggle

GR2Analyst shows county lines prominently because NWS warnings are issued by county. MapLibre's base maps show them faintly, but a dedicated high-contrast county boundary layer would help.

**Implementation:** Add a toggleable MapLibre line layer sourced from a public US county boundaries GeoJSON tileset (e.g., from MapTiler or a self-hosted PMTiles file). Style with thin white/gray lines.

---

## What to skip (for now)

- **Velocity / dual-pol products (ZDR, CC, KDP):** Requires individual radar site data (Level-II or Level-III), not MRMS. Completely different data pipeline. Major architectural change.
- **Storm cell tracking / IDs:** MRMS publishes tracking products but they're a different format. Large scope.
- **Hail size (MESH):** Requires VIL + echo top height + temperature profile. The temp profile isn't available from MRMS alone.
- **Storm Relative Motion:** Requires velocity data.

## Suggested implementation order

The cursor readout is low-effort / high-value and should go first. Derived products (VIL/ET) add the most new analytical capability. Warning polygons add the most situational awareness. Cross-section and measurement are power-user tools.
