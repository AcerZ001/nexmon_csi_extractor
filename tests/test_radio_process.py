import base64
import json
import signal
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nexmon_capture import process, radio
from nexmon_capture.config import Settings


def fake_radio(monkeypatch, settings=Settings(), params=None):
    calls = []
    params = params if params is not None else b"\x09\x10\x01" + b"\0" * 103
    monkeypatch.setattr(radio.shutil, "which", lambda tool: "/usr/bin/" + tool)

    def run(args, **_):
        calls.append(args)
        if args[0].endswith("mcp"):
            return SimpleNamespace(
                returncode=0, stdout=base64.b64encode(params).decode(), stderr=""
            )
        if args[-1] == "info":
            return SimpleNamespace(
                returncode=0,
                stdout="Interface wlan0\n\ttype monitor\n\twiphy 0\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(radio, "command", run)
    return radio.NexmonRadio(settings), calls


def test_radio_applies_channel_mac_filter_and_disables_on_stop(monkeypatch):
    settings = Settings(channel=48, bandwidth=80, tx_macs=(b"123456",))
    device, calls = fake_radio(monkeypatch, settings)
    device.preflight()
    device.setup_monitor()
    device.start()
    device.close()
    mcp = next(cmd for cmd in calls if cmd[0].endswith("mcp"))
    assert mcp[1:3] == ["-c", "48/80"]
    assert mcp[-2:] == ["-m", "31:32:33:34:35:36"]
    configs = [cmd for cmd in calls if cmd[0].endswith("nexutil")]
    assert len(configs) == 2
    assert "-l106" in configs[0]
    assert base64.b64decode(configs[0][-1][2:])[2] == 1
    assert base64.b64decode(configs[1][-1][2:])[2] == 0


def test_many_macs_use_software_filter(monkeypatch):
    settings = replace(Settings(), tx_macs=tuple(bytes([i]) * 6 for i in range(5)))
    device, calls = fake_radio(monkeypatch, settings)
    device.preflight()
    assert "-m" not in calls[-1]


def test_mcp_and_firmware_parameter_size_must_match(monkeypatch):
    device, _ = fake_radio(monkeypatch, params=b"\0" * 34)
    with pytest.raises(RuntimeError, match="106"):
        device.preflight()


def test_monitor_on_wrong_phy_is_not_reused(monkeypatch):
    device, _ = fake_radio(monkeypatch)
    device.preflight()
    monkeypatch.setattr(
        radio,
        "command",
        lambda *_args, **_kw: SimpleNamespace(
            returncode=0, stdout="\twiphy 1\n\ttype monitor\n"
        ),
    )
    with pytest.raises(RuntimeError, match="not a monitor"):
        device.setup_monitor()


@pytest.mark.parametrize("valid", [True, False])
def test_stop_verifies_pid_identity_before_signalling(tmp_path, monkeypatch, valid):
    lock = process.CaptureLock("wlan0")
    lock.path = tmp_path / "capture.lock"
    lock.path.write_text(json.dumps({"pid": 123456, "start": "original"}))
    lock.file = lock.path.open("r+")
    monkeypatch.setattr(
        process, "process_start", lambda _: "original" if valid else "reused-pid"
    )
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _: b"python\0/home/pi/project/src/application/csi_extractor.py\0",
    )
    kill = Mock()
    monkeypatch.setattr(process.os, "kill", kill)
    try:
        if valid:
            lock._signal_holder()
            kill.assert_called_once_with(123456, signal.SIGTERM)
        else:
            with pytest.raises(RuntimeError, match="safely identify"):
                lock._signal_holder()
            kill.assert_not_called()
    finally:
        lock.close()
