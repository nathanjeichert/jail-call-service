"""
G.729 WAV header repair.

Some jail call recordings arrive with the first 60 bytes zeroed out,
making them unreadable by standard tools. This module detects that
condition and grafts a valid G.729 WAV header onto the data.

Ported from attemptWavHeaderRepair() in ffmpegWorker.ts.
"""

import struct
import logging

logger = logging.getLogger(__name__)


def attempt_wav_header_repair(data: bytes) -> bytes | None:
    """
    Check if the first 60 bytes are zeroed. If so, replace them with a
    valid G.729 WAV header and return the repaired bytes.

    Returns None if repair is not needed or not possible.
    """
    if len(data) < 60:
        return None

    # Check if first 60 bytes are all zero
    if any(b != 0 for b in data[:60]):
        return None

    # Verify there's actual data after the header
    if all(b == 0 for b in data[60:min(len(data), 1024)]):
        return None

    data_size = len(data) - 60

    # Build G.729 WAV header (60 bytes):
    # RIFF chunk (12 bytes)
    # fmt  chunk (28 bytes): standard fmt (8) + 20-byte body
    # fact chunk (12 bytes)
    # data chunk (8 bytes)
    header = bytearray(60)

    # RIFF chunk
    header[0:4] = b'RIFF'
    struct.pack_into('<I', header, 4, data_size + 52)  # file size - 8
    header[8:12] = b'WAVE'

    # fmt chunk
    header[12:16] = b'fmt '
    struct.pack_into('<I', header, 16, 20)        # chunk size = 20
    struct.pack_into('<H', header, 20, 0x2222)    # wFormatTag = G.729
    struct.pack_into('<H', header, 22, 2)         # nChannels = 2
    struct.pack_into('<I', header, 24, 8000)      # nSamplesPerSec = 8000
    struct.pack_into('<I', header, 28, 2000)      # nAvgBytesPerSec = 2000
    struct.pack_into('<H', header, 32, 20)        # nBlockAlign = 20
    struct.pack_into('<H', header, 34, 1)         # wBitsPerSample = 1
    struct.pack_into('<H', header, 36, 2)         # cbSize = 2
    struct.pack_into('<H', header, 38, 1)         # extra param

    # fact chunk
    header[40:44] = b'fact'
    struct.pack_into('<I', header, 44, 4)         # chunk size = 4
    struct.pack_into('<I', header, 48, 0)         # dwSampleLength = 0

    # data chunk
    header[52:56] = b'data'
    struct.pack_into('<I', header, 56, data_size)

    repaired = bytes(header) + data[60:]
    logger.info("WAV header repaired: grafted G.729 header onto %d bytes of data", data_size)
    return repaired


def repair_file_in_place(path: str) -> bool:
    """
    Attempt to repair a WAV file's header in-place.
    Returns True if repair was performed, False if not needed.
    """
    with open(path, 'rb') as f:
        data = f.read()

    repaired = attempt_wav_header_repair(data)
    if repaired is None:
        return False

    with open(path, 'wb') as f:
        f.write(repaired)

    logger.info("Repaired WAV header in-place: %s", path)
    return True
