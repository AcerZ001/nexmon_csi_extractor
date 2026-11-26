from datetime import datetime

import pytest

from nexmon_capture.config import load_settings
from nexmon_capture.paths import preflight_database_path, run_database_path


def settings_from(tmp_path, content):
    path = tmp_path / ".env"
    path.write_text(content)
    return load_settings(path, {})


def test_defaults_local_only(tmp_path):
    settings = settings_from(tmp_path, "CSI_DAILY_DIR=data/csi_daily\n")
    assert not settings.send_csi_data
    assert settings.daily_dir == tmp_path / "data/csi_daily"
    assert settings.tx_macs == ()


def test_forwarding_and_multimac_settings(tmp_path):
    settings = settings_from(
        tmp_path,
        "SEND_CSI_DATA=true\nCONTROL_IP=192.168.0.254\nTX_MAC_ADDRESS=AA-BB-CC-DD-EE-FF,001122334455,aa:bb:cc:dd:ee:ff\n",
    )
    assert settings.send_csi_data
    assert settings.tx_macs == (
        bytes.fromhex("aabbccddeeff"),
        bytes.fromhex("001122334455"),
    )


def test_multiple_destinations_are_normalized_and_deduplicated(tmp_path):
    settings = settings_from(
        tmp_path,
        "SEND_CSI_DATA=true\nCONTROL_IP=192.168.1.100, 192.168.1.101,192.168.1.100,::1\nSEND_SHUTDOWN_TIMEOUT_SEC=0.5\n",
    )
    assert settings.control_ips == ("192.168.1.100", "192.168.1.101", "::1")
    assert settings.send_shutdown_timeout == 0.5


def test_process_environment_overrides_ip_list(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SEND_CSI_DATA=true\nCONTROL_IP=192.168.1.100,192.168.1.101\n")
    assert load_settings(path, {"CONTROL_IP": "127.0.0.1,::1"}).control_ips == (
        "127.0.0.1",
        "::1",
    )


@pytest.mark.parametrize(
    "content",
    [
        "SEND_CSI_DATA=typo",
        "DB_BATCH_SIZE=0",
        "BANDWIDTH=160",
        "CHANNEL_NUM=9\nBANDWIDTH=80",
        "TX_MAC_ADDRESS=AA:BB:CC:DD:EE",
        "SEND_CSI_DATA=true\nCONTROL_IP=server.invalid",
        "TCP_PORT=65536",
        "NEXMON_IFACE=../x",
        "NEXMON_MONITOR_IFACE=wlan0",
        "SEND_CSI_DATA=true\nCONTROL_IP=192.168.1.1,",
        "SEND_CSI_DATA=true\nCONTROL_IP=",
        "SEND_CSI_DATA=true\nCONTROL_IP=192.168,x.xxx",
        "SEND_SHUTDOWN_TIMEOUT_SEC=nan",
        "SEND_SHUTDOWN_TIMEOUT_SEC=-1",
        "SEND_SHUTDOWN_TIMEOUT_SEC=11",
        "SOCKET_TIMEOUT=0",
        "SOCKET_TIMEOUT=inf",
        "SOCKET_TIMEOUT=bad",
        "RX_MAC_ADDRESS=00:00:00:00:00:00",
    ],
)
def test_invalid_settings_fail_clearly(tmp_path, content):
    with pytest.raises(ValueError):
        settings_from(tmp_path, content)


def test_environment_override_and_wlan_alias(tmp_path):
    path = tmp_path / ".env"
    path.write_text("WLAN_INTERFACE=wlan1\nSEND_CSI_DATA=true\n")
    settings = load_settings(path, {"SEND_CSI_DATA": "false"})
    assert settings.interface == "wlan1"
    assert not settings.send_csi_data


def test_named_and_absolute_path(tmp_path):
    now = datetime(2026, 10, 15, 12, 34, 56)
    daily = tmp_path / "data/csi_daily"
    assert (
        run_database_path("test.db", daily, started_at=now)
        == tmp_path / "data/test-2026-10-15-12_34_56.db"
    )
    assert (
        run_database_path(str(tmp_path / "other/test"), daily, started_at=now)
        == tmp_path / "other/test-2026-10-15-12_34_56.db"
    )


@pytest.mark.parametrize("name", ["", "../test", "relative/test", "folder/", ".db"])
def test_ambiguous_database_name_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        run_database_path(name, tmp_path)


def test_named_collision_does_not_overwrite(tmp_path):
    path = tmp_path / "test.db"
    path.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        preflight_database_path(path)
    assert path.read_bytes() == b"existing"
