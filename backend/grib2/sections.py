"""
Section-level parsers for GRIB2.

Each function takes the full raw (decompressed) GRIB2 bytes and the byte offset
of the start of that section (including its 4-byte length prefix), and returns a
structured dict.

All multi-byte integers in GRIB2 are big-endian.
"""

import struct


def _unpack(fmt: str, data: bytes, offset: int):
    """struct.unpack_from with big-endian prefix."""
    return struct.unpack_from(">" + fmt, data, offset)


def _signed16(raw: int) -> int:
    """Convert a GRIB2 signed 16-bit value (sign bit is MSB of MSB byte, not two's-complement)."""
    # GRIB2 encodes signed integers with an explicit sign bit (not two's-complement).
    # Bit 15 is the sign bit: 1 = negative.
    sign = (raw >> 15) & 1
    magnitude = raw & 0x7FFF
    return -magnitude if sign else magnitude


def parse_section0(data: bytes, offset: int) -> dict:
    """
    Section 0 — Indicator Section (16 bytes, no length prefix in the usual sense).
    Offset points to the 'GRIB' magic.
    """
    magic = data[offset : offset + 4]
    if magic != b"GRIB":
        raise ValueError(f"Not a GRIB2 file: expected 'GRIB' at offset {offset}, got {magic!r}")

    # Bytes 7: reserved / discipline
    discipline = data[offset + 6]
    edition = data[offset + 7]
    if edition != 2:
        raise ValueError(f"Expected GRIB edition 2, got {edition}")

    # Bytes 8–15: total length (8-byte unsigned big-endian)
    (total_length,) = struct.unpack_from(">Q", data, offset + 8)

    return {
        "discipline": discipline,
        "edition": edition,
        "total_length": total_length,
        "section_length": 16,
    }


def parse_section1(data: bytes, offset: int) -> dict:
    """Section 1 — Identification Section. Extracts reference time."""
    (sec_len,) = _unpack("I", data, offset)
    sec_num = data[offset + 4]
    assert sec_num == 1, f"Expected section 1, got {sec_num}"

    # Bytes relative to start of section (offset 0 = first byte of length field)
    # centre: bytes 5-6, sub-centre: 7-8, master tables: 9, local tables: 10
    # significance: 11, year: 12-13, month: 14, day: 15, hour: 16, min: 17, sec: 18
    (year,) = _unpack("H", data, offset + 12)
    month = data[offset + 14]
    day = data[offset + 15]
    hour = data[offset + 16]
    minute = data[offset + 17]
    second = data[offset + 18]

    return {
        "section_length": sec_len,
        "year": year,
        "month": month,
        "day": day,
        "hour": hour,
        "minute": minute,
        "second": second,
    }


def parse_section3(data: bytes, offset: int) -> dict:
    """
    Section 3 — Grid Definition Section.
    Only Template 3.0 (regular lat/lon) is supported.
    Byte offsets in the spec are 1-based from the section start; we use 0-based from `offset`.
    """
    (sec_len,) = _unpack("I", data, offset)
    sec_num = data[offset + 4]
    assert sec_num == 3, f"Expected section 3, got {sec_num}"

    source = data[offset + 5]
    (num_data_points,) = _unpack("I", data, offset + 6)
    template_num_raw = _unpack("H", data, offset + 12)[0]

    if template_num_raw != 0:
        raise NotImplementedError(
            f"Grid template {template_num_raw} not supported. Only Template 3.0 (regular lat/lon) is implemented."
        )

    # Template 3.0 fields (byte offsets within section, 0-based from section start):
    # Spec says bytes 31-34 are Ni, but that's 1-based. 0-based: offset+30 to offset+33
    # We'll count from the section start (offset).
    #
    # Section header: 5 bytes (length[4] + section_num[1])
    # Grid def section header: source[1] + num_data_points[4] + optional_list_len[1] + interp[1] + template_num[2] = 9
    # Total header = 14 bytes before template fields start (offset+14)
    # Template 3.0 fields (0-based from section start):
    #   +14: shape of earth (1)
    #   +15: scale factor radius (1)
    #   +16-19: scaled value radius (4)
    #   +20: scale factor major axis (1)
    #   +21-24: scaled major axis (4)
    #   +25: scale factor minor axis (1)
    #   +26-29: scaled minor axis (4)
    #   +30-33: Ni (4)
    #   +34-37: Nj (4)
    #   +38-41: basic angle (4)
    #   +42-45: subdivision (4)
    #   +46-49: La1 (4, microdegrees signed)
    #   +50-53: Lo1 (4, microdegrees)
    #   +54: resolution flags (1)
    #   +55-58: La2 (4)
    #   +59-62: Lo2 (4)
    #   +63-66: Di (4)
    #   +67-70: Dj (4)
    #   +71: scanning mode (1)

    (Ni,) = _unpack("I", data, offset + 30)
    (Nj,) = _unpack("I", data, offset + 34)

    # La1, Lo1: signed microdegrees. GRIB2 uses the most-significant bit as sign for Lo1.
    (La1_raw,) = _unpack("i", data, offset + 46)
    (Lo1_raw,) = _unpack("I", data, offset + 50)
    resolution_flags = data[offset + 54]
    (La2_raw,) = _unpack("i", data, offset + 55)
    (Lo2_raw,) = _unpack("I", data, offset + 59)
    (Di_raw,) = _unpack("I", data, offset + 63)
    (Dj_raw,) = _unpack("I", data, offset + 67)
    scanning_mode = data[offset + 71]

    def microdeg_lon(raw: int) -> float:
        # Lo1/Lo2 can encode negative longitudes with the high bit set
        if raw & 0x80000000:
            return (raw - 0x100000000) * 1e-6
        return raw * 1e-6

    La1 = La1_raw * 1e-6
    Lo1 = microdeg_lon(Lo1_raw)
    La2 = La2_raw * 1e-6
    Lo2 = microdeg_lon(Lo2_raw)
    Di = Di_raw * 1e-6
    Dj = Dj_raw * 1e-6

    # Scanning mode bit flags:
    # Bit 1 (0x80): 0 = points scan W→E (i increases eastward)
    # Bit 2 (0x40): 0 = rows scan N→S (j increases southward) — array starts at NW corner
    # Bit 3 (0x20): 0 = consecutive i (row-major)
    scan_i_neg = bool(scanning_mode & 0x80)   # True = i scans E→W
    scan_j_pos = bool(scanning_mode & 0x40)   # True = j scans S→N (rows go south to north)

    return {
        "section_length": sec_len,
        "Ni": Ni,
        "Nj": Nj,
        "La1": La1,
        "Lo1": Lo1,
        "La2": La2,
        "Lo2": Lo2,
        "Di": Di,
        "Dj": Dj,
        "scanning_mode": scanning_mode,
        "scan_i_neg": scan_i_neg,
        "scan_j_pos": scan_j_pos,
        "num_data_points": num_data_points,
    }


def parse_section4(data: bytes, offset: int) -> dict:
    """Section 4 — Product Definition Section."""
    (sec_len,) = _unpack("I", data, offset)
    sec_num = data[offset + 4]
    assert sec_num == 4, f"Expected section 4, got {sec_num}"

    (num_coords,) = _unpack("H", data, offset + 5)
    (template_num,) = _unpack("H", data, offset + 7)

    param_category = data[offset + 9]
    param_number = data[offset + 10]
    # Level type and value (template 4.0 layout)
    level_type = data[offset + 22] if sec_len > 22 else None

    return {
        "section_length": sec_len,
        "template_num": template_num,
        "param_category": param_category,
        "param_number": param_number,
        "level_type": level_type,
    }


def parse_section5(data: bytes, offset: int) -> dict:
    """
    Section 5 — Data Representation Section.
    Handles templates 5.0 (simple), 5.40 (JPEG2000), 5.41 (PNG).
    """
    (sec_len,) = _unpack("I", data, offset)
    sec_num = data[offset + 4]
    assert sec_num == 5, f"Expected section 5, got {sec_num}"

    (num_packed,) = _unpack("I", data, offset + 5)
    (template_num,) = _unpack("H", data, offset + 9)

    if template_num not in (0, 40, 41):
        raise NotImplementedError(
            f"Data representation template {template_num} not supported. "
            "Only 5.0 (simple), 5.40 (JPEG2000), and 5.41 (PNG) are implemented."
        )

    # R: reference value (IEEE 754 float, bytes 11-14 of section)
    (R,) = _unpack("f", data, offset + 11)
    # E: binary scale factor (signed 16-bit, GRIB sign convention)
    (E_raw,) = _unpack("H", data, offset + 15)
    # D: decimal scale factor (signed 16-bit, GRIB sign convention)
    (D_raw,) = _unpack("H", data, offset + 17)
    bits_per_value = data[offset + 19]

    E = _signed16(E_raw)
    D = _signed16(D_raw)

    return {
        "section_length": sec_len,
        "template_num": template_num,
        "num_packed": num_packed,
        "R": R,
        "E": E,
        "D": D,
        "bits_per_value": bits_per_value,
    }


def parse_section6(data: bytes, offset: int) -> dict:
    """
    Section 6 — Bitmap Section.
    Returns the bitmap as a bytes object (or None if no bitmap present).
    """
    (sec_len,) = _unpack("I", data, offset)
    sec_num = data[offset + 4]
    assert sec_num == 6, f"Expected section 6, got {sec_num}"

    bitmap_indicator = data[offset + 5]

    if bitmap_indicator == 255:
        # No bitmap; all data points are present
        return {"section_length": sec_len, "has_bitmap": False, "bitmap": None}
    elif bitmap_indicator == 0:
        # Bitmap follows immediately after this byte
        bitmap_bytes = data[offset + 6 : offset + sec_len]
        return {"section_length": sec_len, "has_bitmap": True, "bitmap": bitmap_bytes}
    else:
        raise NotImplementedError(
            f"Bitmap indicator {bitmap_indicator} not supported. Expected 0 (present) or 255 (absent)."
        )


def parse_section7(data: bytes, offset: int) -> dict:
    """Section 7 — Data Section. Returns raw packed bytes."""
    (sec_len,) = _unpack("I", data, offset)
    sec_num = data[offset + 4]
    assert sec_num == 7, f"Expected section 7, got {sec_num}"

    payload = data[offset + 5 : offset + sec_len]
    return {"section_length": sec_len, "data": payload}
