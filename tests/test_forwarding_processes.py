from __future__ import annotations

import multiprocessing
import os
import pickle
import socket
import struct
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest

from nexmon_capture import forwarding
from nexmon_capture.config import Settings
from nexmon_capture.storage import CaptureStorage


def listener(ip="127.0.0.1", port=0):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((ip, port))
    server.listen(8)
    server.settimeout(8)
    return server


def read_batch(server):
    connection, _ = server.accept()
    with connection:
        connection.settimeout(5)
        data = bytearray()
        while part := connection.recv(65536):
            data.extend(part)
    size = struct.unpack_from(">I", data)[0]
    assert size == len(data) - 4
    return bytes(data), pickle.loads(data[4:])


def wait_until(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for forwarding")


def worker_with_blocked_socket(*args):
    real_send = forwarding.send_batch

    def send(rows, settings):
        if settings.control_ip != "127.0.0.2":
            return real_send(rows, settings)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect((settings.control_ip, settings.tcp_port))
            # Force a socket wait independently of OS TCP buffer sizes.
            connection.recv(1)

    forwarding.send_batch = send
    forwarding._worker(*args)


def test_real_subprocess_per_ip_preserves_exact_legacy_wire_bytes(frame, monkeypatch):
    with listener() as first, listener("127.0.0.2", first.getsockname()[1]) as second:
        settings = Settings(
            send_csi_data=True,
            control_ip="127.0.0.1,127.0.0.2",
            tcp_port=first.getsockname()[1],
            send_shutdown_timeout=0.5,
        )
        with forwarding.CSIForwarder(settings, b"123456") as sender:
            assert len({d.process.pid for d in sender.destinations}) == 2
            assert all(d.process.pid != os.getpid() for d in sender.destinations)
            monkeypatch.setattr(
                forwarding,
                "send_batch",
                Mock(side_effect=AssertionError("parent sent CSI")),
            )
            frames = [frame]
            sender.submit(frames)
            frames.clear()
            one, rows = read_batch(first)
            two, _ = read_batch(second)
            expected = [frame.wire_row(b"123456")]
            payload = pickle.dumps(expected, protocol=pickle.HIGHEST_PROTOCOL)
            assert one == two == struct.pack(">I", len(payload)) + payload
            assert rows == expected
            assert rows[0][0] == frame.csi_value
            assert rows[0][5] == frame.gain
            wait_until(lambda: sender.sent == 2)


def test_offline_ip_does_not_delay_other_destination(frame):
    with listener() as healthy:
        settings = Settings(
            send_csi_data=True,
            control_ip="127.0.0.2,127.0.0.1",
            tcp_port=healthy.getsockname()[1],
            socket_timeout=0.2,
            send_shutdown_timeout=0.2,
        )
        with forwarding.CSIForwarder(settings, b"123456") as sender:
            started = time.monotonic()
            sender.submit([frame])
            assert time.monotonic() - started < 0.5
            assert read_batch(healthy)[1] == [frame.wire_row(b"123456")]
            wait_until(lambda: sender.failed == 1)


def test_blocked_network_process_cannot_delay_storage_or_other_ip(
    tmp_path, frame, monkeypatch
):
    monkeypatch.setattr(forwarding, "_worker", worker_with_blocked_socket)
    with (
        listener() as healthy,
        listener("127.0.0.2", healthy.getsockname()[1]) as stalled,
    ):
        settings = Settings(
            send_csi_data=True,
            control_ip="127.0.0.2,127.0.0.1",
            tcp_port=healthy.getsockname()[1],
            send_queue_batches=1,
            socket_timeout=10,
            send_shutdown_timeout=0.1,
        )
        with forwarding.CSIForwarder(settings, b"123456") as sender:
            sender.submit([frame])
            connection, _ = stalled.accept()
            with connection:
                assert read_batch(healthy)[1] == [frame.wire_row(b"123456")]
                assert sender.destinations[0].counters[:] == [0, 0]
                storage = CaptureStorage(tmp_path)
                committed = []

                def on_commit(batch):
                    committed.extend(batch)
                    sender.submit(batch)

                try:
                    started = time.monotonic()
                    storage.write([frame], on_commit)
                    assert time.monotonic() - started < 1
                    assert committed == [frame]
                    repository = next(iter(storage.repositories.values()))
                    assert repository.connection.execute(
                        "SELECT COUNT(*) FROM CSI_frame"
                    ).fetchone() == (1,)
                finally:
                    storage.close()
                assert read_batch(healthy)[1] == [frame.wire_row(b"123456")]


def test_shutdown_terminates_blocked_process_and_reaps_it(frame, monkeypatch):
    monkeypatch.setattr(forwarding, "_worker", worker_with_blocked_socket)
    with listener("127.0.0.2") as stalled:
        settings = Settings(
            send_csi_data=True,
            control_ip="127.0.0.2",
            tcp_port=stalled.getsockname()[1],
            socket_timeout=10,
            send_shutdown_timeout=0.1,
            send_queue_batches=1,
        )
        sender = forwarding.CSIForwarder(settings, b"123456")
        pid = sender.destinations[0].process.pid
        try:
            sender.submit([frame])
            connection, _ = stalled.accept()
            with connection:
                sender.submit([frame])
                sender.submit([frame])
                started = time.monotonic()
                sender.close()
                sender.close()
                assert time.monotonic() - started < 3
                assert pid not in {p.pid for p in multiprocessing.active_children()}
                assert sender.sent == 0
                assert sender.dropped == 3
        finally:
            sender.close()


def test_start_failure_keeps_local_capture_available(monkeypatch):
    monkeypatch.setattr(
        forwarding, "CSIForwarder", Mock(side_effect=OSError("resources exhausted"))
    )
    assert forwarding.make_forwarder(Settings(send_csi_data=True), b"123456") is None


@pytest.mark.parametrize("rx_mac", [None, b"12345", bytes(6)])
def test_bad_receiver_mac_fails_before_starting_worker(rx_mac):
    with pytest.raises(ValueError, match="receiver MAC"):
        forwarding.make_forwarder(Settings(send_csi_data=True), rx_mac)


def test_killed_worker_drops_new_batches_and_does_not_break_storage(tmp_path, frame):
    settings = Settings(
        send_csi_data=True, control_ip="127.0.0.1", send_shutdown_timeout=0.1
    )
    with forwarding.CSIForwarder(settings, b"123456") as sender:
        destination = sender.destinations[0]
        destination.process.terminate()
        destination.process.join(timeout=3)
        storage = CaptureStorage(tmp_path, tmp_path / "run.db")
        try:
            storage.write([frame], sender.submit)
            assert sender.dropped == 1
            assert storage.repositories[tmp_path / "run.db"].connection.execute(
                "SELECT COUNT(*) FROM CSI_frame"
            ).fetchone() == (1,)
        finally:
            storage.close()


def test_both_days_forward_only_after_commit(tmp_path, frame):
    calls = []
    storage = CaptureStorage(tmp_path)
    first = replace(frame, timestamp=1)
    second = replace(frame, timestamp=86400000001)

    def committed(batch):
        repository = next(reversed(storage.repositories.values()))
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM CSI_frame"
        ).fetchone() == (1,)
        calls.extend(batch)

    try:
        storage.write([second, first], committed)
    finally:
        storage.close()
    assert calls == [first, second]
