import pickle
import queue
import socket
import sqlite3
import struct
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest
from conftest import packet_bytes

from nexmon_capture import forwarding
from nexmon_capture.config import Settings
from nexmon_capture.packet import SO_TIMESTAMPNS
from nexmon_capture.runtime import capture
from nexmon_capture.storage import CaptureStorage


def worker_with_refused_send(*args):
    """Inject a send failure inside the spawned process, never on the LAN."""

    def fail(*_):
        raise ConnectionRefusedError("offline")

    forwarding.send_batch = fail
    forwarding._worker(*args)


def test_plain_tcp_wire_format_against_local_listener(frame):
    received = []
    errors = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)

        def receive():
            try:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(3)
                    data = b""
                    while chunk := conn.recv(4096):
                        data += chunk
                    received.append(data)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=receive)
        thread.start()
        settings = Settings(
            send_csi_data=True,
            control_ip="127.0.0.1",
            tcp_port=listener.getsockname()[1],
        )
        rows = [frame.wire_row(b"123456")]
        forwarding.send_batch(rows, settings)
        thread.join(timeout=4)
    assert not errors
    assert not thread.is_alive()
    length = struct.unpack(">I", received[0][:4])[0]
    assert length == len(received[0]) - 4
    assert pickle.loads(received[0][4:]) == rows
    assert rows == [
        [frame.csi_value, frame.tx_mac, b"123456", -42, frame.timestamp, frame.gain]
    ]


def test_toggle_off_creates_no_worker_or_socket(monkeypatch):
    constructor = Mock(side_effect=AssertionError("must not open a socket"))
    monkeypatch.setattr(forwarding.socket, "socket", constructor)
    monkeypatch.setattr(forwarding.multiprocessing, "get_context", constructor)
    assert forwarding.make_forwarder(Settings(), None) is None
    constructor.assert_not_called()


class FakeCaptureSocket:
    def __init__(self, packets, stop):
        self.packets = iter(packets)
        self.stop = stop

    def recvmsg(self, *_):
        try:
            packet = next(self.packets)
        except StopIteration:
            self.stop.set()
            raise socket.timeout()
        return (
            packet,
            [
                (
                    socket.SOL_SOCKET,
                    SO_TIMESTAMPNS,
                    struct.pack("=qq", 1700000000, 123456000),
                )
            ],
            0,
            ("wlan0",),
        )


def test_capture_filters_rejects_flushes_and_stores_during_network_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(forwarding, "_worker", worker_with_refused_send)
    settings = Settings(
        send_csi_data=True, batch_size=2, tx_macs=(bytes.fromhex("aabbccddeeff"),)
    )
    stop = threading.Event()
    sock = FakeCaptureSocket(
        [
            packet_bytes(),
            packet_bytes(mac=b"123456"),
            packet_bytes()[:-1],
            packet_bytes(),
            packet_bytes(),
        ],
        stop,
    )
    storage = CaptureStorage(tmp_path)
    sender = forwarding.make_forwarder(settings, b"654321")
    stats = capture(sock, settings, storage, sender, stop)
    sender.close()
    storage.close()
    assert stats == {"received": 5, "stored": 3, "rejected": 1, "filtered": 1}
    assert sender.failed == 3
    with sqlite3.connect(next(tmp_path.glob("*.db"))) as db:
        assert db.execute("SELECT count(*) FROM CSI_frame").fetchone() == (3,)


def test_full_send_queue_never_blocks_local_recording(frame, monkeypatch):
    destination = forwarding.Destination("127.0.0.1", Mock(), Mock(), [0, 0])
    destination.inbox.put_nowait.side_effect = queue.Full
    sender = object.__new__(forwarding.CSIForwarder)
    sender.destinations = [destination]
    sender.closed = False
    sender.submit([frame])
    assert sender.dropped == 1
    destination.process.is_alive.return_value = False
    sender.submit([frame])
    assert sender.dropped == 2
    destination.inbox.put_nowait.assert_called_once()


def test_capture_write_error_does_not_resend_committed_day(
    tmp_path, monkeypatch, frame
):
    from nexmon_capture import runtime

    frames = iter([replace(frame, timestamp=1), replace(frame, timestamp=86400000001)])
    monkeypatch.setattr(runtime, "decode_packet", lambda *_: next(frames))
    storage = Mock()

    def partial_write(batch, on_commit):
        on_commit(batch[:1])
        raise sqlite3.OperationalError("disk full")

    storage.write.side_effect = partial_write
    sender = Mock()
    stop = threading.Event()
    with pytest.raises(sqlite3.OperationalError):
        capture(
            FakeCaptureSocket([packet_bytes(), packet_bytes()], stop),
            Settings(batch_size=2),
            storage,
            sender,
            stop,
        )
    assert storage.write.call_count == 1
    assert sender.submit.call_count == 1
    assert len(sender.submit.call_args.args[0]) == 1
