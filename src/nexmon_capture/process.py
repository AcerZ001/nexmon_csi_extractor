"""One capture per interface, with a validated PID for restart and stop."""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from pathlib import Path

LOGGER = logging.getLogger("csi-extractor")


def process_start(pid: int) -> str:
    # /proc/<pid>/stat field 22; comm in parentheses can contain spaces.
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


class CaptureLock:
    def __init__(self, interface: str):
        self.path = Path(f"/run/nexmon-csi-{interface}.lock")
        self.file = None

    def _try_lock(self) -> bool:
        import fcntl

        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _signal_holder(self) -> None:
        self.file.seek(0)
        try:
            owner = json.load(self.file)
            pid = int(owner["pid"])
            if pid <= 1 or pid == os.getpid() or process_start(pid) != owner["start"]:
                raise ValueError("PID no longer identifies the capture")
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if not any(
                arg.endswith(b"/csi_extractor.py")
                or arg == b"nexmon_capture.cli"
                or arg.endswith(b"/csi-extractor")
                for arg in args
            ):
                raise ValueError("Lock owner is not a CSI extractor")
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Cannot safely identify the active capture from {self.path}; stop it manually"
            ) from exc
        LOGGER.info("Stopping previous CSI capture: PID %d", pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def acquire(self, stop_only: bool = False, timeout: float = 135) -> bool:
        self.file = self.path.open("a+", encoding="utf-8")
        if not self._try_lock():
            self._signal_holder()
            deadline = time.monotonic() + timeout
            while not self._try_lock():
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Previous capture is still stopping; wait for its database flush and retry"
                    )
                time.sleep(0.1)
        elif stop_only:
            LOGGER.info("No active capture for this interface")
        if stop_only:
            return False
        self.file.seek(0)
        self.file.truncate()
        json.dump({"pid": os.getpid(), "start": process_start(os.getpid())}, self.file)
        self.file.flush()
        return True

    def close(self) -> None:
        if self.file is not None:
            # Keep the inode: unlinking a lock permits two owners during restart.
            self.file.close()
            self.file = None
