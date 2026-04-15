# MRMS Radar — React Native Integration Spec

This document specifies how to embed the MRMS weather radar viewer into a React Native app. The radar runs as a WebView loading from the radar backend server. All rendering (MapLibre + three.js + GLSL shaders) happens inside the WebView — the RN side is a thin wrapper.

## Prerequisites

- Radar backend running and reachable from the device (e.g. `https://radar.example.com` or a local dev server)
- `react-native-webview` package installed in the RN project

```bash
# Expo
npx expo install react-native-webview

# Bare RN
npm install react-native-webview
```

## Architecture

```
┌──────────────────────────────────────────────┐
│ React Native App                             │
│                                              │
│  ┌─────────────────────────────────────────┐ │
│  │ RadarMap component                      │ │
│  │                                         │ │
│  │  ┌───────────────────────────────────┐  │ │
│  │  │ WebView (auto-detected)           │  │ │
│  │  │ src={BASE_URL}/?controls=none     │  │ │
│  │  │                                   │  │ │
│  │  │  MapLibre + three.js + GLSL       │  │ │
│  │  │  No UI chrome — host controls     │  │ │
│  │  └───────────────────────────────────┘  │ │
│  │                                         │ │
│  └─────────────────────────────────────────┘ │
│                                              │
│  (on expand → modal)                         │
│                                              │
│  ┌─────────────────────────────────────────┐ │
│  │ RadarFullScreen (modal)                 │ │
│  │                                         │ │
│  │  WebView (same URL, responsive)         │ │
│  │  src={BASE_URL}/#7/{lat}/{lon}/0/0      │ │
│  │                                         │ │
│  │  All controls: view mode, presets,      │ │
│  │  animation bar, intensity, legend, etc. │ │
│  └─────────────────────────────────────────┘ │
└──────────────────────────────────────────────┘
```

## Responsive Layout & Auto-Detection

The radar frontend uses a single responsive layout that works on all screen sizes. No `?mode=` parameter is needed.

**Responsive behavior:**
- Desktop (>600px): control panel top-right, animation bar bottom-center, legend, MapLibre nav controls.
- Mobile (≤600px): animation bar stretches full-width with a square play/pause button and a hamburger on the right end. Hamburger opens the control panel as a popup above the toolbar. Legend toggle moves to top-right.

**Embedded context auto-detection:**
The frontend automatically detects when it's running inside a React Native WebView (`window.ReactNativeWebView`) or an iframe (`window.parent !== window`). When embedded:
- The postMessage bridge activates (see API below)
- An expand button appears top-left (sends `requestFullScreen` to the host)
- URL hash sync is disabled (the host controls navigation)

**Controls mode (`?controls=` parameter):**

| URL | Controls shown | Typical use |
|-----|----------------|-------------|
| `{BASE_URL}/` | Full responsive UI — adapts to screen size | Full-screen modal |
| `{BASE_URL}/?controls=minimal` | Animation bar only — no control panel, nav, or legend | Compact embed with basic playback |
| `{BASE_URL}/?controls=none` | No UI chrome at all — bare map + radar rendering | Inline embed where the host app provides all controls via postMessage |

All three modes render the same radar data with the same GPU pipeline. The `none` mode is designed for inline embedding where the native app drives playback, view mode, and settings entirely through the postMessage bridge.

## postMessage API

Communication between RN and the WebView uses `postMessage`. The radar frontend handles inbound messages and emits outbound messages via `window.ReactNativeWebView.postMessage()`.

### Inbound (RN → WebView)

Send JSON strings via `webViewRef.current.postMessage(JSON.stringify(msg))`.

| Message | Fields | Effect |
|---------|--------|--------|
| `setMode` | `{ type: 'setMode', mode: 'composite'\|'3d'\|'volume' }` | Switch radar view mode |
| `setPreset` | `{ type: 'setPreset', preset: 'all'\|'precip'\|'severe' }` | Apply filter preset |
| `setIntensity` | `{ type: 'setIntensity', value: 0.0–1.0 }` | Set radar opacity |
| `setViewport` | `{ type: 'setViewport', center: [lng, lat], zoom: number }` | Fly map to location |
| `seekFrame` | `{ type: 'seekFrame', index: number }` | Seek to frame by index (0-based). Pauses playback. Use with `total` from `frameChanged` events. |
| `play` | `{ type: 'play' }` | Start animation |
| `pause` | `{ type: 'pause' }` | Pause animation |

### Outbound (WebView → RN)

Received via the WebView's `onMessage` prop. Parse with `JSON.parse(event.nativeEvent.data)`.

| Message | Fields | When |
|---------|--------|------|
| `ready` | `{ type: 'ready', timestamps: number }` | Radar loaded and frames available |
| `frameChanged` | `{ type: 'frameChanged', timestamp: string, index: number, total: number }` | Current animation frame changed |
| `requestFullScreen` | `{ type: 'requestFullScreen' }` | User tapped expand button (embedded context) |
| `error` | `{ type: 'error', message: string }` | Fetch or rendering error |

## Component Spec: RadarMap

Replace the existing `RadarMap` component (currently using RainViewer + `react-native-maps`) with a WebView-based implementation. The component should maintain the same external interface.

### Props

```typescript
interface RadarMapProps {
  lat: number;           // Station latitude (used for initial viewport centering)
  lon: number;           // Station longitude
  testID?: string;       // For testing
  radarBaseUrl?: string; // Radar server URL, default from env/config
}
```

### Behavior

1. **Inline mode (default):** Render a fixed-height WebView (240px, matching the current RadarMap height) loading `{radarBaseUrl}/?controls=none`. The frontend auto-detects the WebView context and activates the postMessage bridge. No UI chrome is rendered — the host app provides its own controls. The WebView is non-scrollable and transparent-background.

2. **Initial viewport:** On `ready` message, send a `setViewport` message to center the map on the station's `lat`/`lon` at zoom 6–7 (regional view, vs the default CONUS zoom 4). The radar starts paused on the latest frame — send a `play` message to start animation if desired.

3. **Full-screen mode:** When the WebView sends `requestFullScreen`, open a modal/overlay containing a second WebView loading `{radarBaseUrl}/`. Pass the same viewport center via hash fragment. Provide a close/back button.

4. **Loading state:** Show a placeholder/spinner until the `ready` message arrives. The radar server may take 1–2 minutes to seed on first boot (the frontend handles retry internally, but the WebView will show a blank map during this time).

5. **Error handling:** On `error` messages, optionally show a subtle error indicator. The WebView handles its own retry logic, so this is informational only.

### Reference Implementation

```tsx
import { useState, useRef, useCallback } from 'react';
import { View, Text, Modal, TouchableOpacity, StyleSheet } from 'react-native';
import { WebView } from 'react-native-webview';

const RADAR_BASE_URL = process.env.EXPO_PUBLIC_RADAR_URL ?? 'http://localhost:8000';

interface RadarMapProps {
  lat: number;
  lon: number;
  testID?: string;
  radarBaseUrl?: string;
}

type WebViewRef = WebView;

function useRadarWebView(lat: number, lon: number) {
  const ref = useRef<WebViewRef>(null);
  const [ready, setReady] = useState(false);

  const onMessage = useCallback((event: { nativeEvent: { data: string } }) => {
    try {
      const msg = JSON.parse(event.nativeEvent.data);
      if (msg.type === 'ready') {
        setReady(true);
        // Center on station location with regional zoom
        ref.current?.postMessage(JSON.stringify({
          type: 'setViewport',
          center: [lon, lat],
          zoom: 7,
        }));
      }
    } catch { /* ignore non-JSON messages */ }
    return msg;
  }, [lat, lon]);

  return { ref, ready, onMessage };
}

export function RadarMap({ lat, lon, testID, radarBaseUrl }: RadarMapProps) {
  const baseUrl = radarBaseUrl ?? RADAR_BASE_URL;
  const [fullScreen, setFullScreen] = useState(false);
  const embed = useRadarWebView(lat, lon);

  const handleEmbedMessage = useCallback((event: { nativeEvent: { data: string } }) => {
    const msg = embed.onMessage(event);
    if (msg?.type === 'requestFullScreen') {
      setFullScreen(true);
    }
  }, [embed.onMessage]);

  return (
    <View testID={testID}>
      {/* Inline embed */}
      <View style={styles.container}>
        <WebView
          ref={embed.ref}
          source={{ uri: `${baseUrl}/?controls=none` }}
          style={styles.webview}
          scrollEnabled={false}
          bounces={false}
          javaScriptEnabled
          domStorageEnabled
          allowsInlineMediaPlayback
          mediaPlaybackRequiresUserAction={false}
          onMessage={handleEmbedMessage}
          // Transparent background while loading
          containerStyle={{ backgroundColor: 'transparent' }}
        />
        {!embed.ready && (
          <View style={styles.loadingOverlay}>
            <Text style={styles.loadingText}>Loading radar...</Text>
          </View>
        )}
      </View>

      {/* Full-screen modal */}
      <Modal
        visible={fullScreen}
        animationType="slide"
        supportedOrientations={['landscape', 'portrait']}
        onRequestClose={() => setFullScreen(false)}
      >
        <View style={styles.fullScreenContainer}>
          <WebView
            source={{ uri: `${baseUrl}/#7/${lat}/${lon}/0/0` }}
            style={styles.fullScreenWebview}
            javaScriptEnabled
            domStorageEnabled
            allowsInlineMediaPlayback
            mediaPlaybackRequiresUserAction={false}
          />
          <TouchableOpacity
            style={styles.closeButton}
            onPress={() => setFullScreen(false)}
          >
            <Text style={styles.closeButtonText}>Close</Text>
          </TouchableOpacity>
        </View>
      </Modal>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    height: 240,
    borderRadius: 8,
    overflow: 'hidden',
    position: 'relative',
  },
  webview: {
    flex: 1,
    backgroundColor: 'transparent',
  },
  loadingOverlay: {
    ...StyleSheet.absoluteFillObject,
    backgroundColor: 'rgba(0,0,0,0.6)',
    alignItems: 'center',
    justifyContent: 'center',
  },
  loadingText: {
    color: '#888',
    fontSize: 12,
    fontFamily: 'monospace',
  },
  fullScreenContainer: {
    flex: 1,
    backgroundColor: '#0d1117',
  },
  fullScreenWebview: {
    flex: 1,
  },
  closeButton: {
    position: 'absolute',
    top: 50,
    right: 16,
    backgroundColor: 'rgba(13, 17, 23, 0.88)',
    borderRadius: 8,
    paddingHorizontal: 16,
    paddingVertical: 8,
    borderWidth: 1,
    borderColor: 'rgba(255,255,255,0.15)',
  },
  closeButtonText: {
    color: '#e6edf3',
    fontSize: 14,
    fontWeight: '500',
  },
});
```

### Full-screen URL hash trick

The full-screen WebView URL uses the hash fragment to pre-set the map position:

```
{baseUrl}/#7/{lat}/{lon}/0/0
```

Format: `#{zoom}/{lat}/{lng}/{bearing}/{pitch}`. The radar frontend parses this on load and initializes the map at that location. This avoids needing a `ready` → `setViewport` round-trip for the full-screen view.

## Configuration

The RN app needs one config value: the radar backend URL.

| Variable | Example | Description |
|----------|---------|-------------|
| `EXPO_PUBLIC_RADAR_URL` | `https://radar.example.com` | Radar backend base URL. Falls back to `http://localhost:8000` for development. |

The radar backend serves both the API (`/api/radar/*`) and the frontend static files (`/`) from the same origin, so a single URL is sufficient.

## Replacing the Existing RadarMap

The existing `RadarMap.native.tsx` uses `react-native-maps` with RainViewer tiles. The MRMS radar is a strict upgrade:

| Feature | RainViewer (current) | MRMS Radar (new) |
|---------|---------------------|-------------------|
| Data source | RainViewer API (third-party) | NOAA MRMS (authoritative) |
| Resolution | ~2 km | ~1 km (0.01 deg) |
| Update interval | ~10 min | ~2 min |
| Temporal interpolation | None (frame jump) | Motion-compensated advection |
| View modes | 2D only | Composite, 3D stacked, Volume |
| Color ramp | Custom 7-step | NWS standard 13-step |
| Vertical data | None | 8 tilt levels (0.5 deg to 19 deg) |

To replace:

1. Install `react-native-webview`
2. Replace `RadarMap.native.tsx` with the WebView implementation above
3. Replace `RadarMap.tsx` (web platform) with an iframe-based equivalent:
   ```tsx
   export function RadarMap({ lat, lon, testID }: RadarMapProps) {
     const baseUrl = process.env.EXPO_PUBLIC_RADAR_URL ?? 'http://localhost:8000';
     return (
       <View testID={testID} style={{ height: 240, borderRadius: 8, overflow: 'hidden' }}>
         <iframe
           src={`${baseUrl}/?controls=none#7/${lat}/${lon}/0/0`}
           style={{ width: '100%', height: '100%', border: 'none' }}
           allow="webgl"
         />
       </View>
     );
   }
   ```
4. Add `EXPO_PUBLIC_RADAR_URL` to `.env`
5. Remove `react-native-maps` if no longer used elsewhere

## WebView Considerations

- **WebGL support:** Required. All modern iOS (WKWebView) and Android (Chromium-based) WebViews support WebGL. No special flags needed.
- **CORS:** Not an issue — the WebView loads from the radar server origin, and all API calls are same-origin.
- **Cache:** Atlas tiles use `Cache-Control: immutable`. The WebView's HTTP cache makes revisited animation frames instant.
- **Memory:** The radar uses ~50–100 MB of WebView memory for textures. On low-memory devices, the WebView may reclaim textures (they reload automatically on next access).
- **Touch events:** The WebView handles pan/zoom gestures internally (MapLibre). Set `scrollEnabled={false}` and `bounces={false}` to prevent RN scroll view interference.
- **Orientation:** The embed is designed for landscape but works in portrait. The full-screen modal should allow both orientations via `supportedOrientations`.

## Testing

The `RadarMap` component should have a co-located test file. Key test cases:

1. Renders WebView with correct URL
2. Shows loading overlay before `ready` message
3. Hides loading overlay after `ready` message
4. Opens full-screen modal on `requestFullScreen` message
5. Closes modal on close button press
6. Sends `setViewport` message on ready

Mock `react-native-webview` in tests — the radar rendering itself can't be tested in Jest (requires a real browser with WebGL).
