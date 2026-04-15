/**
 * RadarBridge — postMessage bridge for React Native WebView communication.
 *
 * Inbound (RN → WebView):
 *   { type: 'setMode', mode: 'composite'|'3d'|'volume' }
 *   { type: 'setPreset', preset: 'all'|'precip'|'severe' }
 *   { type: 'setIntensity', value: 0.0–1.0 }
 *   { type: 'setViewport', center: [lng, lat], zoom: number }
 *   { type: 'seekFrame', index: 0–N }
 *   { type: 'play' }
 *   { type: 'pause' }
 *
 * Outbound (WebView → RN):
 *   { type: 'ready', timestamps: number }
 *   { type: 'frameChanged', timestamp, index, total }
 *   { type: 'requestFullScreen' }
 *   { type: 'error', message }
 */

export class RadarBridge {
  constructor(engine, map) {
    this._engine = engine;
    this._map = map;
    this._listening = false;
  }

  start() {
    if (this._listening) return;
    this._listening = true;

    window.addEventListener('message', e => this._onMessage(e));

    this._engine.addEventListener('timestamps', e => {
      this._send({ type: 'ready', timestamps: e.detail.count });
    });

    this._engine.addEventListener('frame', e => {
      this._send({
        type: 'frameChanged',
        timestamp: e.detail.timestamp,
        index: e.detail.index,
        total: e.detail.total,
      });
    });

    this._engine.addEventListener('status', e => {
      if (e.detail.state === 'error') {
        this._send({ type: 'error', message: e.detail.message });
      }
    });
  }

  requestFullScreen() {
    this._send({ type: 'requestFullScreen' });
  }

  _onMessage(event) {
    const msg = event.data;
    if (!msg || typeof msg.type !== 'string') return;

    switch (msg.type) {
      case 'setMode':
        if (msg.mode) this._engine.setViewMode(msg.mode);
        break;
      case 'setPreset':
        if (msg.preset) this._engine.switchPreset(msg.preset);
        break;
      case 'setIntensity':
        if (msg.value != null) this._engine.setOpacity(parseFloat(msg.value));
        break;
      case 'setViewport':
        if (msg.center && this._map) {
          this._map.flyTo({
            center: msg.center,
            zoom: msg.zoom ?? this._map.getZoom(),
            duration: 600,
          });
        }
        break;
      case 'seekFrame':
        if (msg.index != null) this._engine.setFrameIndex(parseInt(msg.index, 10));
        break;
      case 'play':
        this._engine.play();
        break;
      case 'pause':
        this._engine.pause();
        break;
    }
  }

  _send(msg) {
    // React Native WebView injects window.ReactNativeWebView
    if (window.ReactNativeWebView?.postMessage) {
      window.ReactNativeWebView.postMessage(JSON.stringify(msg));
    }
    // Also post to parent for iframe embedding
    if (window.parent !== window) {
      window.parent.postMessage(msg, '*');
    }
  }
}
