"""Capture settings loaded once at startup from the project's .env."""

from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values


def mac_bytes(value: str) -> bytes:
    value = value.strip().replace("-", ":")
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}|[0-9a-fA-F]{12}", value):
        raise ValueError(f"Invalid MAC address: {value!r}; provide all six pairs")
    return bytes.fromhex(value.replace(":", ""))


def parse_control_ips(value: str) -> tuple[str, ...]:
    """Validate and deduplicate numeric destination IPs, preserving order."""
    addresses = []
    for item in value.split(","):
        try:
            address = str(ipaddress.ip_address(item.strip()))
        except ValueError as exc:
            raise ValueError(
                "CONTROL_IP must be a comma-separated list of valid IP addresses"
            ) from exc
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


@dataclass(frozen=True)
class Settings:
    interface: str = "wlan0"
    monitor_interface: str = "mon0"
    channel: int = 9
    bandwidth: int = 20
    config_length: int = 106
    tx_macs: tuple[bytes, ...] = ()
    daily_dir: Path = Path("/home/pi/secure_mount/data/csi_daily")
    batch_size: int = 500
    write_interval_ms: int = 1000
    log_level: str = "INFO"
    stats_interval: int = 30
    send_csi_data: bool = False
    control_ip: str = "192.168.0.254"
    tcp_port: int = 8080
    socket_timeout: float = 5.0
    send_queue_batches: int = 20
    send_shutdown_timeout: float = 3.0
    rx_mac: bytes | None = None

    def __post_init__(self):
        if not isinstance(self.send_csi_data, bool):
            raise ValueError("SEND_CSI_DATA must be true or false")
        if self.send_csi_data:
            parse_control_ips(self.control_ip)
        if not 1 <= self.tcp_port <= 65535:
            raise ValueError("TCP_PORT must be between 1 and 65535")
        if not math.isfinite(self.socket_timeout) or not 0 < self.socket_timeout <= 30:
            raise ValueError(
                "SOCKET_TIMEOUT must be greater than 0 and at most 30 seconds"
            )
        if not 1 <= self.send_queue_batches <= 1000:
            raise ValueError("SEND_QUEUE_BATCHES must be between 1 and 1000")
        if (
            not math.isfinite(self.send_shutdown_timeout)
            or not 0 <= self.send_shutdown_timeout <= 10
        ):
            raise ValueError(
                "SEND_SHUTDOWN_TIMEOUT_SEC must be between 0 and 10 seconds"
            )
        if self.rx_mac is not None and (
            not isinstance(self.rx_mac, bytes)
            or len(self.rx_mac) != 6
            or self.rx_mac == bytes(6)
        ):
            raise ValueError("RX_MAC_ADDRESS must be a nonzero six-byte MAC address")

    @property
    def control_ips(self) -> tuple[str, ...]:
        return parse_control_ips(self.control_ip)


def load_settings(env_file: Path, environ: Mapping[str, str] | None = None) -> Settings:
    if not env_file.is_file():
        raise ValueError(
            f"Environment file not found: {env_file}; copy .env.example to .env"
        )
    values = {
        key: value
        for key, value in dotenv_values(env_file, interpolate=False).items()
        if value is not None
    }
    values.update(os.environ if environ is None else environ)

    def integer(name: str, default: int, maximum: int) -> int:
        try:
            result = int(values.get(name, str(default)))
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if not 1 <= result <= maximum:
            raise ValueError(f"{name} must be between 1 and {maximum}")
        return result

    def boolean(name: str, default: str = "false") -> bool:
        value = values.get(name, default).strip().lower()
        if value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError(f"{name} must be true or false")
        return value in {"true", "1", "yes", "on"}

    def number(name: str, default: str) -> float:
        try:
            return float(values.get(name, default))
        except ValueError as exc:
            raise ValueError(f"{name} must be a number") from exc

    interface = values.get(
        "NEXMON_IFACE", values.get("WLAN_INTERFACE", "wlan0")
    ).strip()
    monitor = values.get("NEXMON_MONITOR_IFACE", "mon0").strip()
    for value in (interface, monitor):
        if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,14}", value):
            raise ValueError(
                "Interface names must be 1-15 letters, digits, dots, hyphens or underscores"
            )
    if monitor == interface:
        raise ValueError("NEXMON_MONITOR_IFACE must differ from NEXMON_IFACE")
    channel = integer("CHANNEL_NUM", 9, 196)
    bandwidth = integer("BANDWIDTH", 20, 80)
    if bandwidth not in (20, 40, 80) or (channel <= 14 and bandwidth == 80):
        raise ValueError(
            "BANDWIDTH must be 20/40 MHz for 2.4 GHz or 20/40/80 MHz for 5 GHz"
        )
    tx_macs = tuple(
        dict.fromkeys(
            mac_bytes(mac)
            for mac in values.get("TX_MAC_ADDRESS", "").split(",")
            if mac.strip()
        )
    )
    directory = Path(values.get("CSI_DAILY_DIR", str(Settings.daily_dir))).expanduser()
    if not directory.is_absolute():
        directory = env_file.resolve().parent / directory
    level = values.get("LOG_LEVEL", "INFO").strip().upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Invalid LOG_LEVEL")
    send = boolean("SEND_CSI_DATA")
    control_ip = values.get("CONTROL_IP", "192.168.0.254").strip()
    rx_mac = values.get("RX_MAC_ADDRESS", "").strip()
    return Settings(
        interface=interface,
        monitor_interface=monitor,
        channel=channel,
        bandwidth=bandwidth,
        config_length=integer("NEXMON_CONFIG_LENGTH", 106, 4096),
        tx_macs=tx_macs,
        daily_dir=directory,
        log_level=level,
        batch_size=integer("DB_BATCH_SIZE", 500, 10000),
        write_interval_ms=integer("WRITE_INTERVAL_MS", 1000, 60000),
        stats_interval=integer("STATS_LOG_INTERVAL_SEC", 30, 3600),
        send_csi_data=send,
        control_ip=control_ip,
        tcp_port=integer("TCP_PORT", 8080, 65535),
        socket_timeout=number("SOCKET_TIMEOUT", "5"),
        send_queue_batches=integer("SEND_QUEUE_BATCHES", 20, 1000),
        send_shutdown_timeout=number("SEND_SHUTDOWN_TIMEOUT_SEC", "3"),
        rx_mac=mac_bytes(rx_mac) if rx_mac else None,
    )
