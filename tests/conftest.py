from __future__ import annotations

import struct

import pytest

from nexmon_capture.packet import decode_packet


def packet_bytes(bandwidth=20, mac=b"\xaa\xbb\xcc\xdd\xee\xff", options=b""):
    header = bytearray(70)
    header[:2] = b"\x11\x11"
    struct.pack_into("b", header, 2, -42)
    header[4:10] = mac
    for i in range(48):
        struct.pack_into("b", header, 18 + i, i - 24)
    struct.pack_into("<h", header, 66, 1234)
    nfft = bandwidth * 16 // 5
    iq = b"".join(struct.pack("<hh", i - 32768, 32767 - i) for i in range(nfft))
    udp = struct.pack("!HHHH", 5500, 5500, 8 + len(header) + len(iq), 0)
    ip = bytearray(20)
    ip[0] = 0x45 + len(options) // 4
    ip[9] = 17
    struct.pack_into(
        "!H", ip, 2, len(ip) + len(options) + len(udp) + len(header) + len(iq)
    )
    return b"\xff" * 6 + b"NEXMON" + b"\x08\x00" + ip + options + udp + header + iq


@pytest.fixture
def frame():
    return decode_packet(packet_bytes(), 20, 1_760_000_000_123456)
