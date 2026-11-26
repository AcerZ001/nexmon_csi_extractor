"""Decode the existing BCM43455c0 Gain Recovery V2 Nexmon packet format."""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

# Preserve the control server's eleven little-endian signed int16 gain fields.
GAIN_FIELDS = (
    "lna1_1",
    "mix_1",
    "dvga_1",
    "lna1_2",
    "mix_2",
    "lna1_9",
    "lna1_10",
    "mix_10",
    "lpf1_10",
    "dvga_10",
    "agcGain",
)
GAIN_GROUPS = ("elna", "lna1", "lna2", "mix", "lpf0", "lpf1", "dvga", "trLoss")
GAIN_SUFFIXES = (1, 2, 3, 4, 9, 10)
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)


@dataclass(frozen=True)
class CSIFrame:
    csi_value: bytes
    tx_mac: bytes
    rssi: int
    timestamp: int
    gain: bytes
    bandwidth: int
    num_subcarriers: int
    header: bytes

    def wire_row(self, rx_mac: bytes) -> list:
        """Append the original 70-byte header after the six existing fields."""
        return [
            self.csi_value,
            self.tx_mac,
            rx_mac,
            self.rssi,
            self.timestamp,
            self.gain,
            self.header,
        ]


def effective_bins(bandwidth: int) -> tuple[int, ...]:
    if bandwidth not in (20, 40, 80):
        raise ValueError("Nexmon bandwidth must be 20, 40 or 80 MHz")
    edge, inner = {20: (28, 1), 40: (58, 2), 80: (122, 2)}[bandwidth]
    return tuple(range(-edge, -inner + 1)) + tuple(range(inner, edge + 1))


def decode_packet(packet: bytes, bandwidth: int, timestamp_us: int) -> CSIFrame | None:
    """Ignore other Ethernet traffic; reject malformed Nexmon frames explicitly."""
    if len(packet) < 14 or packet[6:12] != b"NEXMON" or packet[12:14] != b"\x08\x00":
        return None
    if len(packet) < 42 or packet[14] >> 4 != 4:
        raise ValueError("Truncated or invalid Nexmon IPv4 packet")
    ip_length = (packet[14] & 15) * 4
    total_length = struct.unpack_from("!H", packet, 16)[0]
    udp = 14 + ip_length
    if (
        ip_length < 20
        or total_length < ip_length + 8
        or len(packet) < 14 + total_length
        or packet[23] != 17
        or struct.unpack_from("!H", packet, 20)[0] & 0x3FFF
    ):
        raise ValueError("Invalid, fragmented or truncated Nexmon IP/UDP packet")
    _, destination, udp_length = struct.unpack_from("!HHH", packet, udp)
    if destination != 5500:
        return None
    if udp_length < 8 or udp_length != total_length - ip_length:
        raise ValueError("Invalid Nexmon UDP length")
    payload = packet[udp + 8 : udp + udp_length]
    nfft = bandwidth * 16 // 5
    bins = effective_bins(bandwidth)
    if len(payload) != 70 + nfft * 4:
        raise ValueError(
            f"Expected Gain Recovery V2 header (70 bytes) + {nfft} IQ pairs for "
            f"{bandwidth} MHz; got {len(payload)} payload bytes. Check firmware and BANDWIDTH"
        )
    if payload[:2] != b"\x11\x11":
        raise ValueError("Invalid Nexmon CSI magic")
    gain = {
        f"{group}_{suffix}": struct.unpack_from("b", payload, 18 + group_id * 6 + i)[0]
        for group_id, group in enumerate(GAIN_GROUPS)
        for i, suffix in enumerate(GAIN_SUFFIXES)
    }
    gain["agcGain"] = struct.unpack_from("<h", payload, 66)[0]
    raw = payload[70:]
    # Indexing unshifted FFT bins produces the same order as FFT shift followed
    # by the old effective-subcarrier selection, preserving int16 IQ exactly.
    csi = b"".join(raw[(index % nfft) * 4 : (index % nfft) * 4 + 4] for index in bins)
    return CSIFrame(
        csi_value=csi,
        tx_mac=payload[4:10],
        rssi=struct.unpack_from("b", payload, 2)[0],
        timestamp=timestamp_us,
        gain=struct.pack("<11h", *(gain[field] for field in GAIN_FIELDS)),
        bandwidth=bandwidth,
        num_subcarriers=len(bins),
        header=payload[:70],
    )


def timestamp_us(ancillary: list) -> int:
    for level, kind, data in ancillary:
        if (
            level == socket.SOL_SOCKET
            and kind == SO_TIMESTAMPNS
            and len(data) in (8, 16)
        ):
            seconds, nanos = struct.unpack("=qq" if len(data) == 16 else "=ii", data)
            if seconds >= 0 and 0 <= nanos < 1_000_000_000:
                return seconds * 1_000_000 + nanos // 1000
    return time.time_ns() // 1000


def open_capture_socket(interface: str):
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800))
    try:
        sock.bind((interface, 0))
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        sock.settimeout(0.2)
        return sock
    except BaseException:
        sock.close()
        raise
