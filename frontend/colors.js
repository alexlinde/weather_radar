/**
 * NWS Reflectivity Color Scale utilities.
 *
 * Maps dBZ values to RGBA colours using the standard NWS palette.
 * Values below 5 dBZ (or NaN/null) are fully transparent.
 */

export const NWS_DBZ_COLORS = [
  { min:  5, max: 10, r:  64, g: 192, b:  64 }, // light green
  { min: 10, max: 15, r:  48, g: 160, b:  48 },
  { min: 15, max: 20, r:   0, g: 144, b:   0 }, // green
  { min: 20, max: 25, r:   0, g: 120, b:   0 },
  { min: 25, max: 30, r: 255, g: 255, b:   0 }, // yellow
  { min: 30, max: 35, r: 230, g: 180, b:   0 }, // gold
  { min: 35, max: 40, r: 255, g: 100, b:   0 }, // orange
  { min: 40, max: 45, r: 255, g:   0, b:   0 }, // red
  { min: 45, max: 50, r: 200, g:   0, b:   0 }, // dark red
  { min: 50, max: 55, r: 180, g:   0, b: 120 }, // magenta
  { min: 55, max: 60, r: 150, g:   0, b: 200 }, // purple
  { min: 60, max: 65, r: 255, g: 255, b: 255 }, // white
  { min: 65, max: 75, r: 200, g: 200, b: 255 }, // light blue-white
];

/**
 * Create a 256-entry RGBA Uint8Array for use as a 256×1 GPU color ramp texture.
 *
 * Index maps to the encoded pixel value from the atlas tile.
 * Encoding: pixel = round((dBZ + 30) * 2), so dBZ = pixel / 2 - 30.
 * Index 0 is always transparent (no echo).
 */
export function createColorRampData() {
  const data = new Uint8Array(256 * 4);

  for (let i = 1; i < 256; i++) {
    const dbz = i / 2.0 - 30.0;
    const off = i * 4;

    if (dbz < 5) {
      // Below display threshold — transparent
      continue;
    }

    let r = 0, g = 0, b = 0, matched = false;
    for (const band of NWS_DBZ_COLORS) {
      if (dbz >= band.min && dbz < band.max) {
        // Interpolate within the band for smooth gradients
        const t = (dbz - band.min) / (band.max - band.min);
        const nextIdx = NWS_DBZ_COLORS.indexOf(band) + 1;
        if (nextIdx < NWS_DBZ_COLORS.length) {
          const next = NWS_DBZ_COLORS[nextIdx];
          r = Math.round(band.r + (next.r - band.r) * t);
          g = Math.round(band.g + (next.g - band.g) * t);
          b = Math.round(band.b + (next.b - band.b) * t);
        } else {
          r = band.r; g = band.g; b = band.b;
        }
        matched = true;
        break;
      }
    }

    if (!matched && dbz >= 75) {
      r = 200; g = 200; b = 255;
    }

    if (r || g || b) {
      const alpha = Math.min(255, Math.max(100, Math.round(100 + (dbz - 10) * (155 / 50))));
      data[off]     = r;
      data[off + 1] = g;
      data[off + 2] = b;
      data[off + 3] = alpha;
    }
  }

  return data;
}

/**
 * Build the legend DOM entries — call once on page load.
 * @param {HTMLElement} container
 */
export function buildLegend(container) {
  const bands = [
    ...NWS_DBZ_COLORS,
    { min: 75, max: Infinity, r: 200, g: 200, b: 255 },
  ];
  container.innerHTML = '';
  for (const band of bands) {
    const item = document.createElement('div');
    item.className = 'legend-item';

    const swatch = document.createElement('div');
    swatch.className = 'legend-swatch';
    swatch.style.backgroundColor = `rgb(${band.r},${band.g},${band.b})`;

    const label = document.createElement('span');
    label.textContent = band.max === Infinity
      ? `≥${band.min} dBZ`
      : `${band.min}–${band.max} dBZ`;

    item.appendChild(swatch);
    item.appendChild(label);
    container.appendChild(item);
  }
}
