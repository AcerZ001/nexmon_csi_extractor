"""Capture loop: filter, batch to disk, then optionally forward committed data."""

from __future__ import annotations

import logging
import socket
import time
from threading import Event

from .config import Settings
from .packet import decode_packet, timestamp_us

LOGGER = logging.getLogger("csi-extractor")


def capture(sock, settings: Settings, storage, forwarder, stop: Event) -> dict:
    stats = {"received": 0, "stored": 0, "rejected": 0, "filtered": 0}
    pending = []
    last_write = last_stats = time.monotonic()
    allowed = set(settings.tx_macs)

    def committed(frames):
        stats["stored"] += len(frames)
        if forwarder is not None:
            forwarder.submit(frames)

    def flush():
        nonlocal pending, last_write
        batch, pending = pending, []
        before = stats["stored"]
        try:
            if batch:
                storage.write(batch, committed)
        except Exception:
            LOGGER.error(
                "Database write failed: %d frames from this batch were not committed; stopping capture",
                len(batch) - (stats["stored"] - before),
            )
            # Do not replay earlier days that committed before a later day failed.
            raise
        last_write = time.monotonic()

    try:
        while not stop.is_set():
            try:
                packet, ancillary, flags, _address = sock.recvmsg(65535, 256)
            except socket.timeout:
                packet = None
            if packet is not None and len(packet) >= 12 and packet[6:12] == b"NEXMON":
                stats["received"] += 1
                try:
                    if flags & getattr(socket, "MSG_TRUNC", 32):
                        raise ValueError("Truncated capture buffer")
                    frame = decode_packet(
                        packet, settings.bandwidth, timestamp_us(ancillary)
                    )
                    if frame is not None:
                        if allowed and frame.tx_mac not in allowed:
                            stats["filtered"] += 1
                        else:
                            pending.append(frame)
                except ValueError as exc:
                    stats["rejected"] += 1
                    if stats["rejected"] == 1 or stats["rejected"] % 1000 == 0:
                        LOGGER.warning("Rejected Nexmon frame: %s", exc)
            now = time.monotonic()
            if (
                len(pending) >= settings.batch_size
                or now - last_write >= settings.write_interval_ms / 1000
            ):
                flush()
            if now - last_stats >= settings.stats_interval:
                LOGGER.info(
                    "CSI received=%d stored=%d pending=%d rejected=%d filtered=%d",
                    stats["received"],
                    stats["stored"],
                    len(pending),
                    stats["rejected"],
                    stats["filtered"],
                )
                if stats["received"] == 0:
                    LOGGER.warning(
                        "No Nexmon packets: check firmware, channel/bandwidth and transmitter traffic"
                    )
                if forwarder is not None:
                    forwarder.log_status()
                last_stats = now
    finally:
        try:
            flush()
        finally:
            LOGGER.info(
                "Capture stopped: received=%d stored=%d rejected=%d filtered=%d",
                stats["received"],
                stats["stored"],
                stats["rejected"],
                stats["filtered"],
            )
    return stats
