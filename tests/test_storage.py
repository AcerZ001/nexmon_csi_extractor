import sqlite3
from dataclasses import replace

import pytest

from nexmon_capture.storage import CaptureStorage, CSIRepository


def test_daily_files_have_independent_mac_tables(tmp_path, frame):
    storage = CaptureStorage(tmp_path)
    first = replace(frame, timestamp=86_400_000_000 - 1)
    second = replace(frame, timestamp=86_400_000_000, tx_mac=b"123456")
    committed = []
    storage.write([first, second], committed.extend)
    storage.close()
    assert committed == [first, second]
    for path, expected in [
        (tmp_path / "csi_1970-01-01.db", first),
        (tmp_path / "csi_1970-01-02.db", second),
    ]:
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT id, mac_addr FROM MAC_addr").fetchall() == [
                (1, expected.tx_mac)
            ]
            assert db.execute(
                "SELECT csi_value, rssi, gain, timestamp, bandwidth, nexmon_header FROM CSI_frame"
            ).fetchone() == (
                expected.csi_value,
                expected.rssi,
                expected.gain,
                expected.timestamp,
                expected.bandwidth,
                expected.header,
            )
            assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert (
            not path.with_name(path.name + "-wal").exists()
            or path.with_name(path.name + "-wal").stat().st_size == 0
        )


def test_named_run_stays_in_one_file_across_midnight(tmp_path, frame):
    path = tmp_path / "run.db"
    storage = CaptureStorage(tmp_path / "daily", path)
    storage.write(
        [replace(frame, timestamp=1), replace(frame, timestamp=86400000001)],
        lambda _: None,
    )
    storage.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM CSI_frame").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM MAC_addr").fetchone() == (1,)
    assert not (tmp_path / "daily").exists()
    with pytest.raises(FileExistsError):
        CaptureStorage(tmp_path, path)


def test_failed_transaction_rolls_back_mac_and_frames(tmp_path, frame):
    repo = CSIRepository(tmp_path / "test.db")
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_frames([replace(frame, csi_value=None)])
    assert repo.connection.execute("SELECT count(*) FROM MAC_addr").fetchone() == (0,)
    repo.insert_frames([frame])
    assert repo.connection.execute("SELECT count(*) FROM CSI_frame").fetchone() == (1,)
    repo.close()


def test_legacy_without_mac_mapping_is_not_reassigned(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE CSI_frame(id INTEGER PRIMARY KEY, csi_value BLOB, mac_id INTEGER, rssi INTEGER, gain BLOB, timestamp INTEGER)"
        )
        db.execute("INSERT INTO CSI_frame VALUES (1, X'1234', 8, -42, NULL, 1)")
    with pytest.raises(ValueError, match="Legacy daily"):
        CSIRepository(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT mac_id FROM CSI_frame").fetchone() == (8,)
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='MAC_addr'"
            ).fetchone()
            is None
        )


def test_prior_day_commit_reported_once_when_next_day_fails(
    tmp_path, frame, monkeypatch
):
    original = CSIRepository.insert_frames

    def fail_next(self, frames):
        if frames[0].timestamp >= 86400000000:
            raise sqlite3.OperationalError("disk full")
        original(self, frames)

    monkeypatch.setattr(CSIRepository, "insert_frames", fail_next)
    storage = CaptureStorage(tmp_path)
    committed = []
    first = replace(frame, timestamp=1)
    with pytest.raises(sqlite3.OperationalError):
        storage.write([first, replace(frame, timestamp=86400000000)], committed.extend)
    assert committed == [first]
    storage.close()
