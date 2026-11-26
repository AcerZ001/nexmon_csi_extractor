"""Self-contained daily SQLite files, or one timestamped database per run."""

from __future__ import annotations

import logging
import sqlite3
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from .packet import CSIFrame

LOGGER = logging.getLogger("csi-extractor")


def daily_db_path(directory: Path, timestamp: int) -> Path:
    if (
        not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or not 0 <= timestamp <= 253402300799999999
    ):
        raise ValueError("Invalid CSI timestamp (integer UTC microseconds required)")
    day = date(1970, 1, 1) + timedelta(days=timestamp // 86_400_000_000)
    return directory / f"csi_{day.isoformat()}.db"


class CSIRepository:
    def __init__(self, path: Path, *, create_new: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        if create_new:
            with path.open("xb"):
                pass
        self.connection = sqlite3.connect(path, timeout=5)
        self.macs: dict[bytes, int] = {}
        try:
            self._init_schema()
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            self.connection.close()
            raise
        LOGGER.info("CSI database opened: %s", path)

    def _init_schema(self) -> None:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            columns = {
                row[1]
                for row in self.connection.execute("PRAGMA table_info(CSI_frame)")
            }
            if columns:
                if (
                    "feitcsi_header" in columns
                    or not {"id", "csi_value", "mac_id", "rssi", "timestamp"} <= columns
                ):
                    raise ValueError(
                        "Unsupported database schema; select a new CSI_DAILY_DIR or run name"
                    )
                has_macs = self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND lower(name)='mac_addr'"
                ).fetchone()
                if (
                    not has_macs
                    and self.connection.execute(
                        "SELECT 1 FROM CSI_frame LIMIT 1"
                    ).fetchone()
                ):
                    raise ValueError(
                        "Legacy daily database has no MAC_addr table. Keep it with its old database.db and choose a new CSI_DAILY_DIR or named run"
                    )
            self.connection.execute("""CREATE TABLE IF NOT EXISTS MAC_addr (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac_addr BLOB NOT NULL UNIQUE)""")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS CSI_frame (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                csi_value BLOB NOT NULL,
                mac_id INTEGER NOT NULL REFERENCES MAC_addr(id),
                rssi INTEGER NOT NULL,
                gain BLOB,
                timestamp INTEGER NOT NULL,
                bandwidth INTEGER,
                num_subcarriers INTEGER,
                nexmon_header BLOB)""")
            if self.connection.execute("""SELECT 1 FROM CSI_frame c
                LEFT JOIN MAC_addr m ON m.id=c.mac_id WHERE m.id IS NULL LIMIT 1""").fetchone():
                raise ValueError(
                    "Database has missing MAC mappings; use a new database"
                )
            if columns:
                for name, kind in (
                    ("gain", "BLOB"),
                    ("bandwidth", "INTEGER"),
                    ("num_subcarriers", "INTEGER"),
                    ("nexmon_header", "BLOB"),
                ):
                    if name not in columns:
                        self.connection.execute(
                            f"ALTER TABLE CSI_frame ADD COLUMN {name} {kind}"
                        )
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_csi_mac_id_timestamp ON CSI_frame(mac_id, timestamp)"
            )

    def insert_frames(self, frames: list[CSIFrame]) -> None:
        new_macs = {}
        rows = []
        with self.connection:
            for frame in frames:
                mac_id = self.macs.get(frame.tx_mac, new_macs.get(frame.tx_mac))
                if mac_id is None:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO MAC_addr(mac_addr) VALUES (?)",
                        (frame.tx_mac,),
                    )
                    mac_id = self.connection.execute(
                        "SELECT id FROM MAC_addr WHERE mac_addr=?", (frame.tx_mac,)
                    ).fetchone()[0]
                    new_macs[frame.tx_mac] = mac_id
                rows.append(
                    (
                        frame.csi_value,
                        mac_id,
                        frame.rssi,
                        frame.gain,
                        frame.timestamp,
                        frame.bandwidth,
                        frame.num_subcarriers,
                        frame.header,
                    )
                )
            self.connection.executemany(
                """INSERT INTO CSI_frame
                (csi_value, mac_id, rssi, gain, timestamp, bandwidth, num_subcarriers, nexmon_header)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        self.macs.update(new_macs)

    def close(self) -> None:
        try:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.connection.close()


class CaptureStorage:
    def __init__(self, directory: Path, run_path: Path | None = None):
        self.directory = directory
        self.run_path = run_path
        self.repositories: OrderedDict[Path, CSIRepository] = OrderedDict()
        if run_path is not None:
            self.repositories[run_path] = CSIRepository(run_path, create_new=True)
        else:
            directory.mkdir(parents=True, exist_ok=True)

    def write(
        self, frames: list[CSIFrame], on_commit: Callable[[list[CSIFrame]], None]
    ) -> None:
        batches = defaultdict(list)
        for frame in frames:
            batches[
                self.run_path or daily_db_path(self.directory, frame.timestamp)
            ].append(frame)
        for path, batch in sorted(batches.items()):
            if path not in self.repositories:
                if len(self.repositories) >= 2:
                    _, oldest = self.repositories.popitem(last=False)
                    oldest.close()
                self.repositories[path] = CSIRepository(path)
            self.repositories.move_to_end(path)
            self.repositories[path].insert_frames(batch)
            # Notify only committed frames, including when a later day fails.
            on_commit(batch)

    def close(self) -> None:
        first_error = None
        for repository in self.repositories.values():
            try:
                repository.close()
            except sqlite3.Error as exc:
                first_error = first_error or exc
        self.repositories.clear()
        if first_error:
            raise first_error
