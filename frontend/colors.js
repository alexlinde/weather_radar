/**
 * NWS Reflectivity Color Scale utilities.
 *
 * Maps dBZ values to RGBA colours using the standard NWS palette.
 * Values below 5 dBZ (or NaN/null) are fully transparent.
 */

const NWS_DBZ_COLORS = [
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
 * Map a dBZ value to an [r, g, b, a] array (0–255 each).
 * Returns [0,0,0,0] for values below 5 dBZ or null/undefined.
 */
function dbzToColor(dbz) {
  if (dbz == null || isNaN(dbz) || dbz < 5) return [0, 0, 0, 0];
  for (const band of NWS_DBZ_COLORS) {
    if (dbz >= band.min && dbz < band.max) {
      return [band.r, band.g, band.b, 220];
    }
  }
  // Above highest band
  return [200, 200, 255, 220];
}

/**
 * Build the legend DOM entries — call once on page load.
 * @param {HTMLElement} container
 */
function buildLegend(container) {
  const bands = [
    ...NWS_DBZ_COLORS,
    { min: 65, max: Infinity, r: 200, g: 200, b: 255 },
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
