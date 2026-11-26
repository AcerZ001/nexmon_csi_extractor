"""Best-effort Nexmon TCP fan-out, with an isolated subprocess per destination."""

from __future__ import annotations

import logging
import multiprocessing
import pickle
import queue
import signal
import socket
import struct
import time
from dataclasses import dataclass

from .config import Settings
from .packet import CSIFrame

LOGGER = logging.getLogger("csi-forwarding")


def send_batch(rows: list, settings: Settings) -> None:
    """Send one legacy Nexmon batch to one numeric IP in a worker process."""
    payload = pickle.dumps(rows, protocol=pickle.HIGHEST_PROTOCOL)
    ip = settings.control_ip
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
        connection.settimeout(settings.socket_timeout)
        connection.connect((ip, settings.tcp_port))
        connection.sendall(struct.pack(">I", len(payload)) + payload)


def _worker(inbox, stopping, counters, ip, port, timeout, rx_mac, log_level):
    """Only this process encodes batches, opens TCP sockets and sends CSI."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(message)s")
    parent = multiprocessing.parent_process()
    settings = Settings(control_ip=ip, tcp_port=port, socket_timeout=timeout)
    last_warning = float("-inf")
    while parent is None or parent.is_alive():
        try:
            frames = inbox.get(timeout=0.1)
        except queue.Empty:
            if stopping.is_set():
                break
            continue
        except (EOFError, OSError):
            break
        try:
            rows = [frame.wire_row(rx_mac) for frame in frames]
            send_batch(rows, settings)
            counters[0] += len(frames)
        except Exception as exc:
            counters[1] += len(frames)
            now = time.monotonic()
            if now - last_warning >= 30:
                LOGGER.warning(
                    "TCP forwarding to %s:%d failed: %s; local CSI is stored",
                    ip,
                    port,
                    exc,
                )
                last_warning = now


@dataclass
class Destination:
    ip: str
    inbox: object
    process: object
    counters: object
    submitted: int = 0
    dropped: int = 0
    last_warning: float = float("-inf")


class CSIForwarder:
    def __init__(self, settings: Settings, rx_mac: bytes):
        self.settings = settings
        self.rx_mac = rx_mac
        self.destinations: list[Destination] = []
        self.closed = False
        # spawn avoids inheriting SQLite connections, UDP sockets or signal handlers.
        context = multiprocessing.get_context("spawn")
        self.stopping = context.Event()
        try:
            for ip in settings.control_ips:
                inbox = context.Queue(maxsize=settings.send_queue_batches)
                # A dead/slow worker must never make interpreter exit wait for IPC.
                inbox.cancel_join_thread()
                counters = context.Array("Q", 2, lock=False)
                process = context.Process(
                    target=_worker,
                    args=(
                        inbox,
                        self.stopping,
                        counters,
                        ip,
                        settings.tcp_port,
                        settings.socket_timeout,
                        rx_mac,
                        settings.log_level,
                    ),
                    name=f"csi-tcp-{ip}",
                    daemon=True,
                )
                destination = Destination(ip, inbox, process, counters)
                try:
                    process.start()
                except Exception:
                    inbox.close()
                    process.close()
                    raise
                self.destinations.append(destination)
                LOGGER.info(
                    "TCP forwarding subprocess started: %s:%d PID=%d receiver=%s",
                    ip,
                    settings.tcp_port,
                    process.pid,
                    rx_mac.hex(":"),
                )
        except BaseException:
            self.close()
            raise

    @property
    def sent(self) -> int:
        return sum(destination.counters[0] for destination in self.destinations)

    @property
    def failed(self) -> int:
        return sum(destination.counters[1] for destination in self.destinations)

    @property
    def dropped(self) -> int:
        return sum(destination.dropped for destination in self.destinations)

    def submit(self, frames: list[CSIFrame]) -> None:
        """Queue committed immutable frames without waiting for capacity/network."""
        for destination in self.destinations:
            reason = None
            if self.closed:
                reason = "forwarding closed"
            elif not destination.process.is_alive():
                reason = "subprocess exited"
            else:
                try:
                    # Queue feeder performs IPC serialization outside capture's path.
                    # Snapshot the list because capture clears its pending batch.
                    destination.inbox.put_nowait(list(frames))
                    destination.submitted += len(frames)
                except queue.Full:
                    reason = "queue full"
                except (OSError, ValueError):
                    reason = "IPC queue unavailable"
            if reason:
                destination.dropped += len(frames)
                now = time.monotonic()
                if now - destination.last_warning >= 30:
                    LOGGER.warning(
                        "TCP forwarding to %s: %s; dropping %d forwarded frames (local data retained)",
                        destination.ip,
                        reason,
                        len(frames),
                    )
                    destination.last_warning = now

    def log_status(self) -> None:
        for destination in self.destinations:
            sent, failed = destination.counters
            LOGGER.info(
                "TCP %s sent=%d failed=%d dropped=%d pending=%d",
                destination.ip,
                sent,
                failed,
                destination.dropped,
                max(0, destination.submitted - sent - failed),
            )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.stopping.set()
        deadline = time.monotonic() + self.settings.send_shutdown_timeout
        for destination in self.destinations:
            destination.process.join(timeout=max(0, deadline - time.monotonic()))
        for destination in self.destinations:
            if destination.process.is_alive():
                destination.process.terminate()
        deadline = time.monotonic() + 1
        for destination in self.destinations:
            destination.process.join(timeout=max(0, deadline - time.monotonic()))
        for destination in self.destinations:
            if destination.process.is_alive():
                destination.process.kill()
        deadline = time.monotonic() + 1
        for destination in self.destinations:
            destination.process.join(timeout=max(0, deadline - time.monotonic()))
            sent, failed = destination.counters
            destination.dropped += max(0, destination.submitted - sent - failed)
            destination.submitted = sent + failed
            destination.inbox.close()
            if not destination.process.is_alive():
                destination.process.close()
        self.log_status()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def make_forwarder(settings: Settings, rx_mac: bytes | None) -> CSIForwarder | None:
    if not settings.send_csi_data:
        return None
    if rx_mac is None or len(rx_mac) != 6 or rx_mac == bytes(6):
        raise ValueError("A nonzero six-byte receiver MAC is required for forwarding")
    try:
        return CSIForwarder(settings, rx_mac)
    except Exception:
        LOGGER.exception(
            "Cannot start forwarding subprocesses; continuing local CSI capture"
        )
        return None
