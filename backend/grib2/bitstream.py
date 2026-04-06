"""
BitstreamReader: reads N-bit unsigned integers packed sequentially into a byte buffer.

Used by Template 5.0 (simple packing) to unpack variable-width integers from
GRIB2 Section 7.
"""

import numpy as np


class BitstreamReader:
    """Reads N-bit unsigned integers from a packed byte buffer."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._bit_pos = 0

    def read(self, n: int) -> int:
        """Read the next N-bit unsigned integer."""
        if n == 0:
            return 0

        result = 0
        bits_remaining = n

        while bits_remaining > 0:
            byte_idx = self._bit_pos >> 3
            if byte_idx >= len(self._data):
                raise EOFError(
                    f"BitstreamReader: ran out of data at bit {self._bit_pos} "
                    f"(data length {len(self._data)} bytes)"
                )

            bit_offset_in_byte = self._bit_pos & 7
            bits_available_in_byte = 8 - bit_offset_in_byte
            bits_to_take = min(bits_remaining, bits_available_in_byte)

            shift = bits_available_in_byte - bits_to_take
            mask = (1 << bits_to_take) - 1
            chunk = (self._data[byte_idx] >> shift) & mask

            result = (result << bits_to_take) | chunk
            self._bit_pos += bits_to_take
            bits_remaining -= bits_to_take

        return result

    def read_array(self, n: int, count: int) -> np.ndarray:
        """Read `count` consecutive N-bit unsigned integers using vectorized numpy ops."""
        if n == 0:
            self._bit_pos += 0
            return np.zeros(count, dtype=np.uint64)

        total_bits = n * count
        byte_start = self._bit_pos >> 3
        bit_offset = self._bit_pos & 7
        bytes_needed = (bit_offset + total_bits + 7) >> 3

        buf = np.frombuffer(
            self._data[byte_start : byte_start + bytes_needed],
            dtype=np.uint8,
        )
        bits = np.unpackbits(buf)[bit_offset : bit_offset + total_bits]
        bits = bits.reshape(count, n)
        powers = np.uint64(1) << np.arange(n - 1, -1, -1, dtype=np.uint64)
        values = bits.astype(np.uint64) @ powers

        self._bit_pos += total_bits
        return values
