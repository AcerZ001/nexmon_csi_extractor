"""Start or stop standalone Nexmon CSI capture."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sqlite3
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryFile
from threading import Event

from .config import load_settings, mac_bytes
from .forwarding import make_forwarder
from .packet import open_capture_socket
from .paths import preflight_database_path, run_database_path
from .process import CaptureLock
from .radio import NexmonRadio
from .runtime import capture
from .storage import CaptureStorage

LOGGER = logging.getLogger("csi-extractor")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database_name",
        nargs="?",
        help="Name or absolute path prefix; appends -YYYY-MM-DD-HH_MM_SS.db",
    )
    parser.add_argument(
        "--env-file", type=Path, default=Path(__file__).resolve().parents[2] / ".env"
    )
    parser.add_argument(
        "--stop", action="store_true", help="Gracefully stop this interface's capture"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("Run on the Raspberry Pi with sudo ./csi_extract.sh")
        settings = load_settings(args.env_file)
        logging.getLogger().setLevel(settings.log_level)
        lock = CaptureLock(settings.interface)
        if args.stop:
            with ExitStack() as stack:
                stack.callback(lock.close)
                lock.acquire(stop_only=True)
            LOGGER.info("CSI capture stopped")
            return 0
        stop = Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        run_path = (
            run_database_path(args.database_name, settings.daily_dir)
            if args.database_name is not None
            else None
        )
        if run_path is not None:
            preflight_database_path(run_path)
        else:
            settings.daily_dir.mkdir(parents=True, exist_ok=True)
            with TemporaryFile(dir=settings.daily_dir):
                pass
        radio = NexmonRadio(settings)
        radio.preflight()
        rx_mac = None
        if settings.send_csi_data:
            rx_mac = settings.rx_mac or mac_bytes(
                Path(f"/sys/class/net/{settings.interface}/address").read_text()
            )
            if rx_mac == b"\0" * 6:
                raise ValueError("Receiver MAC is all zero; set RX_MAC_ADDRESS")
        with ExitStack() as stack:
            stack.callback(lock.close)
            lock.acquire()
            if stop.is_set():
                return 0
            storage = CaptureStorage(settings.daily_dir, run_path)
            stack.callback(storage.close)
            forwarder = make_forwarder(settings, rx_mac)
            if forwarder is not None:
                stack.callback(forwarder.close)
            stack.callback(radio.close)
            radio.setup_monitor()
            sock = stack.enter_context(open_capture_socket(settings.interface))
            radio.start()
            LOGGER.info(
                "Capturing Nexmon on %s into %s",
                settings.interface,
                run_path or settings.daily_dir,
            )
            LOGGER.info(
                "TX MAC filter: %s",
                ", ".join(mac.hex(":") for mac in settings.tx_macs)
                or "disabled (accept all transmitters)",
            )
            if forwarder is None:
                LOGGER.info(
                    "Control-server forwarding: %s",
                    "unavailable; local recording continues"
                    if settings.send_csi_data
                    else "disabled (SEND_CSI_DATA=false)",
                )
            else:
                LOGGER.info(
                    "Control-server forwarding: plain TCP to %s:%d, receiver=%s",
                    ", ".join(settings.control_ips),
                    settings.tcp_port,
                    rx_mac.hex(":"),
                )
            capture(sock, settings, storage, forwarder, stop)
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as exc:
        LOGGER.error("Capture failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
