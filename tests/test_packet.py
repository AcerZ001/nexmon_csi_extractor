import socket
import struct

import pytest
from conftest import packet_bytes

from nexmon_capture.packet import SO_TIMESTAMPNS, decode_packet, timestamp_us


@pytest.mark.parametrize(
    "bandwidth,count,edge,inner", [(20, 56, 28, 1), (40, 114, 58, 2), (80, 242, 122, 2)]
)
def test_iq_matches_old_fft_shift_and_keeps_signed_int16(bandwidth, count, edge, inner):
    frame = decode_packet(packet_bytes(bandwidth), bandwidth, 123456)
    nfft = bandwidth * 16 // 5
    original = [(i - 32768, 32767 - i) for i in range(nfft)]
    shifted = original[nfft // 2 :] + original[: nfft // 2]
    expected = [
        shifted[nfft // 2 + index]
        for index in list(range(-edge, -inner + 1)) + list(range(inner, edge + 1))
    ]
    assert list(struct.iter_unpack("<hh", frame.csi_value)) == expected
    assert frame.num_subcarriers == count
    assert frame.timestamp == 123456
    assert frame.rssi == -42
    assert frame.tx_mac.hex() == "aabbccddeeff"


def test_gain_blob_preserves_existing_control_server_field_order(frame):
    assert struct.unpack("<11h", frame.gain) == (
        -18,
        -6,
        12,
        -17,
        -5,
        -14,
        -13,
        -1,
        11,
        17,
        1234,
    )
    assert len(frame.header) == 70


def test_ip_options_are_not_mistaken_for_gain_header():
    assert decode_packet(packet_bytes(options=b"\x01" * 4), 20, 1) == decode_packet(
        packet_bytes(), 20, 1
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p[:-1],
        lambda p: p[:16] + b"\0\0" + p[18:],
        lambda p: p[:42] + b"\0\0" + p[44:],
        lambda p: p[:38] + b"\0\x08" + p[40:],
        lambda p: p[:20] + b"\x20\0" + p[22:],
    ],
)
def test_bad_packets_rejected(mutate):
    with pytest.raises(ValueError):
        decode_packet(mutate(packet_bytes()), 20, 1)


def test_wrong_bandwidth_rejected():
    with pytest.raises(ValueError, match="BANDWIDTH"):
        decode_packet(packet_bytes(80), 20, 1)


def test_non_csi_traffic_ignored():
    packet = packet_bytes()
    assert decode_packet(b"other traffic", 20, 1) is None
    assert decode_packet(packet[:6] + b"123456" + packet[12:], 20, 1) is None


@pytest.mark.parametrize("fmt", ["=qq", "=ii"])
def test_kernel_timestamp_32_and_64_bit(fmt):
    data = struct.pack(fmt, 1700000000, 999999999)
    assert (
        timestamp_us([(0, 0, b"noise"), (socket.SOL_SOCKET, SO_TIMESTAMPNS, data)])
        == 1700000000999999
    )
