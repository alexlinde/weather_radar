# Custom GRIB2 Decoder

**Do not use eccodes, cfgrib, pygrib, or wgrib2.** These all depend on the eccodes C library, which is ~100MB+, has painful cross-platform installation, and is massive overkill for our use case. We only need to decode one product family (MRMS reflectivity) which uses a narrow, predictable subset of the GRIB2 spec.

## GRIB2 structure (what we parse)

| Section | Name | What we extract |
|---------|------|-----------------|
| 0 | Indicator | Magic bytes `GRIB`, edition (must be 2), total message length |
| 1 | Identification | Reference time (year, month, day, hour, minute, second) |
| 2 | Local Use | Skip (optional, MRMS may not include it) |
| 3 | Grid Definition | Grid template, Ni (cols), Nj (rows), lat/lon of first and last grid point, resolution |
| 4 | Product Definition | Parameter category, parameter number, level type, level value (tilt angle) |
| 5 | Data Representation | Packing template number, reference value, binary scale, decimal scale, bits per value |
| 6 | Bitmap | Bitmap presence indicator — if present, a bitmask of valid data points |
| 7 | Data | Packed data values |
| 8 | End | Magic bytes `7777` |

All multi-byte integers are big-endian. Use Python's `struct` module.

## Supported templates

- **Grid Template 3.0** — regular lat/lon grid (the only one MRMS uses)
- **Data Representation Template 5.0** — simple packing (N-bit unsigned integers)
- **Data Representation Template 5.40** — JPEG2000 packing (decoded via Pillow)
- **Data Representation Template 5.41** — PNG packing (decoded via Pillow)

All three data templates use the same physical formula:
```
value = (R + packed_int * 2^E) / 10^D
```

## GRIB2 signed integer encoding

GRIB2 uses an explicit sign bit (MSB), **not** two's-complement, for signed 16-bit fields like the binary scale factor (E) and decimal scale factor (D):
```python
def _signed16(raw: int) -> int:
    sign = (raw >> 15) & 1
    magnitude = raw & 0x7FFF
    return -magnitude if sign else magnitude
```

## Longitude encoding

MRMS longitude values can use the high bit (0x80000000) to represent negative values — not the GRIB2 sign-bit convention but a straightforward unsigned-wrapping pattern:
```python
def microdeg_lon(raw: int) -> float:
    if raw & 0x80000000:
        return (raw - 0x100000000) * 1e-6
    return raw * 1e-6
```

## Bitmap handling

Bitmap indicator byte 5 of Section 6:
- 255 = no bitmap, all grid points have data
- 0 = bitmap follows (1 bit per grid point, 1 = data present, 0 = missing)

When a bitmap is present, the packed values in Section 7 are sparse — only grid points where the bitmap bit is 1 have corresponding values. Expand back to the full grid with NaN fill.

## Section 3 byte offsets (Template 3.0)

All offsets from section start, 0-based:

| Offset | Field | Description |
|--------|-------|-------------|
| +30..33 | Ni | Columns (number of points along parallel) |
| +34..37 | Nj | Rows (number of points along meridian) |
| +46..49 | La1 | Latitude of first grid point (signed int, microdegrees) |
| +50..53 | Lo1 | Longitude of first grid point (unsigned, microdegrees) |
| +55..58 | La2 | Latitude of last grid point |
| +59..62 | Lo2 | Longitude of last grid point |
| +63..66 | Di | i-direction increment (microdegrees) |
| +67..70 | Dj | j-direction increment (microdegrees) |
| +71 | Scanning mode | Bit flags for scan direction |

**Note:** La2/Lo2 start at offset +55/+59, not +56/+60 as the GRIB2 spec's 1-based numbering might suggest. This is because the resolution flags byte at +54 is a single byte, not a 4-byte field.

## Section 5 byte offsets

| Offset | Field | Description |
|--------|-------|-------------|
| +11..14 | R | Reference value (IEEE 754 float) |
| +15..16 | E | Binary scale factor (GRIB2 signed 16-bit) |
| +17..18 | D | Decimal scale factor (GRIB2 signed 16-bit) |
| +19 | bits | Number of bits per packed value |

## What we do NOT need to handle
- Grid templates other than 3.0
- Packing templates other than 5.0, 5.40, 5.41
- Multiple messages per file
- Complex/second-order packing (Template 5.2, 5.3)
- Spectral data, ensemble metadata, or any exotic GRIB2 features

If we encounter an unsupported template, fail loudly with a clear error message identifying the template number.
