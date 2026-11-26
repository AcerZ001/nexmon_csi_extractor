"""Apply Nexmon firmware parameters and manage the monitor interface."""

from __future__ import annotations

import base64
import logging
import re
import shutil
import subprocess

from .config import Settings

LOGGER = logging.getLogger("csi-extractor")


def command(args: list[str], *, check: bool = True):
    result = subprocess.run(
        args, check=False, capture_output=True, text=True, timeout=10
    )
    if check and result.returncode:
        raise RuntimeError(
            f"{' '.join(args[:2])} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def wiphy(output: str) -> str:
    match = re.search(r"^\s*wiphy\s+(\d+)\s*$", output, re.MULTILINE)
    if not match:
        raise RuntimeError(
            "iw did not report a wireless PHY; check the Nexmon driver and interface"
        )
    return "phy" + match.group(1)


class NexmonRadio:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.created_monitor = False
        self.configured = False
        self.tools: dict[str, str] = {}
        self.params = b""
        self.phy = ""

    def preflight(self) -> None:
        for name in ("mcp", "nexutil", "iw", "ip"):
            binary = shutil.which(name)
            if binary is None:
                raise RuntimeError(
                    f"{name} is missing from PATH; install the matching Nexmon utilities"
                )
            self.tools[name] = binary
        settings = self.settings
        info = command([self.tools["iw"], "dev", settings.interface, "info"])
        self.phy = wiphy(info.stdout)
        args = [
            self.tools["mcp"],
            "-c",
            f"{settings.channel}/{settings.bandwidth}",
            "-C",
            "0x1",
            "-N",
            "0x1",
        ]
        # Four addresses work with both the original mcp and the extended one.
        # Longer lists are filtered in Python, with firmware filtering disabled.
        if 0 < len(settings.tx_macs) <= 4:
            args += ["-m", ",".join(mac.hex(":") for mac in settings.tx_macs)]
        result = command(args)
        try:
            self.params = base64.b64decode(result.stdout.strip(), validate=True)
        except ValueError as exc:
            raise RuntimeError(
                f"mcp did not return CSI parameters: {result.stdout.strip()} {result.stderr.strip()}"
            ) from exc
        if len(self.params) != settings.config_length or len(self.params) < 4:
            raise RuntimeError(
                f"mcp generated {len(self.params)} bytes, but NEXMON_CONFIG_LENGTH={settings.config_length}. Use the mcp utility matching your installed firmware"
            )
        if self.params[2] != 1:
            raise RuntimeError("mcp parameters do not enable CSI collection")

    def setup_monitor(self) -> None:
        settings = self.settings
        command([self.tools["ip"], "link", "set", "dev", settings.interface, "up"])
        monitor = command(
            [self.tools["iw"], "dev", settings.monitor_interface, "info"], check=False
        )
        if monitor.returncode:
            command(
                [
                    self.tools["iw"],
                    "phy",
                    self.phy,
                    "interface",
                    "add",
                    settings.monitor_interface,
                    "type",
                    "monitor",
                ]
            )
            self.created_monitor = True
        elif wiphy(monitor.stdout) != self.phy or not re.search(
            r"^\s*type\s+monitor\s*$", monitor.stdout, re.MULTILINE
        ):
            raise RuntimeError(
                f"{settings.monitor_interface} is not a monitor interface on {self.phy}"
            )
        command(
            [self.tools["ip"], "link", "set", "dev", settings.monitor_interface, "up"]
        )

    def _apply(self, params: bytes) -> None:
        command(
            [
                self.tools["nexutil"],
                f"-I{self.settings.interface}",
                "-s500",
                "-b",
                f"-l{len(params)}",
                "-v" + base64.b64encode(params).decode("ascii"),
            ]
        )

    def start(self) -> None:
        self.configured = True
        self._apply(self.params)
        LOGGER.info(
            "Nexmon configured: interface=%s primary_channel=%d bandwidth=%d MHz",
            self.settings.interface,
            self.settings.channel,
            self.settings.bandwidth,
        )

    def close(self) -> None:
        try:
            if self.configured:
                # csi_params.csi_collect is byte 2 in the existing firmware ABI.
                disabled = bytearray(self.params)
                disabled[2] = 0
                self._apply(bytes(disabled))
        finally:
            if self.created_monitor:
                command(
                    [self.tools["iw"], "dev", self.settings.monitor_interface, "del"]
                )
