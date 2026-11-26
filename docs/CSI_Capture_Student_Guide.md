# Nexmon CSI capture: student guide

These examples assume the code is installed at `/home/pi/secure_mount/rpi-csi-rx`.

| Item | Location on the Pi |
| --- | --- |
| Code | `/home/pi/secure_mount/rpi-csi-rx` |
| Settings | `/home/pi/secure_mount/rpi-csi-rx/.env` |
| Daily databases | `/home/pi/secure_mount/data/csi_daily` |
| Named databases | `/home/pi/secure_mount/data` |
| Capture log | `/home/pi/secure_mount/logs/csi_extract.log` |

## First setup

The Pi must already have the Nexmon Gain Recovery V2 firmware and its matching
`mcp` and `nexutil` utilities. The receiver also uses `iw` and `ip`.
The capture script uses `/home/pi/.pyenv/versions/nexmon-csi/bin/python`.

```bash
cd /home/pi/secure_mount/rpi-csi-rx
/home/pi/.pyenv/versions/nexmon-csi/bin/python -m pip install -e .
chmod +x csi_extract.sh
mkdir -p /home/pi/secure_mount/logs
```

If `.env` does not exist, create it with `cp .env.example .env`.
Keep the settings in an existing `.env` when updating the code.

For the first switch from the old application, stop its running extractor,
configuration watcher and automatic restart service before using this script.
The new script manages its own captures; it cannot stop an old service from
restarting the previous application. No systemd service is required for this version.

If committing from Windows, record the executable bit in Git once:

```bash
git update-index --chmod=+x csi_extract.sh
git commit -m "Make CSI capture launcher executable"
```

## Change settings

```bash
nano /home/pi/secure_mount/rpi-csi-rx/.env
```

Set `CHANNEL_NUM` to the transmitter's primary channel and `BANDWIDTH` to its
bandwidth in MHz. For example, primary channel 48 with 80 MHz uses
`CHANNEL_NUM=48` and `BANDWIDTH=80`. Nexmon receives the channel specification
`48/80` through `mcp`; do not substitute the bonded centre-channel number.
The installed firmware and `mcp` must support the selected channel combination.

Select one or more transmitters:

```dotenv
TX_MAC_ADDRESS=AA:BB:CC:DD:EE:FF,00:11:22:33:44:55
```

Leave `TX_MAC_ADDRESS=` blank to accept all transmitters. The old JSON transmitter
configuration is no longer used. Settings are read at startup; restart capture
after editing `.env`.

To record locally and also forward CSI to one or more control servers:

```dotenv
SEND_CSI_DATA=true
CONTROL_IP=192.168.1.100,192.168.1.101
TCP_PORT=8080
SEND_QUEUE_BATCHES=20
SEND_SHUTDOWN_TIMEOUT_SEC=3
```

Set `SEND_CSI_DATA=false` to record only in the local database. This is the default.
Forwarding uses unencrypted TCP. The server must accept length-prefixed pickle
CSI messages without TLS or receiver authentication, including the seventh
field containing the original 70-byte Nexmon header.
This extractor does not register with the server through MQTT.

Replace the example IPs with the actual destination addresses. Separate them with
commas; a single address also works. All destinations use `TCP_PORT`, and each
machine must run a compatible Nexmon TCP receiver. Spaces around commas are
allowed and duplicate IPs are removed. Restart capture after editing `.env`.

Each machine has a separate sender subprocess and limited queue. Network sends
and message serialization run in those subprocesses. An unavailable machine or
full queue cannot delay local extraction/storage or the other sender processes.
When a queue fills, forwarded copies for that machine are dropped; local records
stay saved. Failed sends are not replayed automatically. CSI/gain byte encodings
are preserved, and the complete header is included in each forwarded row.

Deploy and restart the updated unified receiver **before** updating/restarting the
Pi extractor. The receiver accepts both older six-field messages and the extended
seven-field messages. New messages populate `nexmon_header`; older messages and
historical records can still show NULL. No database migration or `.env` change is
needed for the header.

The receiver's MAC is read from `NEXMON_IFACE` automatically. `RX_MAC_ADDRESS`
can override it when required by your server's receiver mapping.

## Start in the terminal

From `/home/pi`:

```bash
cd /home/pi
sudo ./secure_mount/rpi-csi-rx/csi_extract.sh
```

Or from the project directory:

```bash
sudo ./csi_extract.sh
```

The script stops the previous capture started by this version on the same
interface, configures Nexmon, and starts recording. It does not delete old
databases or logs. Press **Ctrl+C** to stop and flush pending frames to disk.

## Start in the background

```bash
cd /home/pi
sudo ./secure_mount/rpi-csi-rx/csi_extract.sh > /home/pi/secure_mount/logs/csi_extract.log 2>&1 &
```

The log directory must exist and be writable by the shell user. `>` replaces the
previous log; use `>>` to append. For capture that must survive disconnecting SSH:

```bash
sudo -v
sudo nohup /home/pi/secure_mount/rpi-csi-rx/csi_extract.sh > /home/pi/secure_mount/logs/csi_extract.log 2>&1 < /dev/null &
```

## Choose a database name

With no name, the script writes `csi_YYYY-MM-DD.db` under `CSI_DAILY_DIR` and
rotates by the packet's **UTC date**. The next day's file is created when its
first frame is flushed. Every daily file contains both `CSI_frame` and
`MAC_addr`; MAC IDs belong only to that file.

Give a name to keep the entire capture in one file, including across midnight:

```bash
sudo ./csi_extract.sh test_csi_name
```

Example result:
`/home/pi/secure_mount/data/test_csi_name-2026-10-15-12_34_56.db`.
When `CSI_DAILY_DIR` ends in `csi_daily`, named captures go in its parent folder.
Otherwise they go directly in `CSI_DAILY_DIR`.

An absolute path chooses another folder:

```bash
sudo ./csi_extract.sh /home/pi/csi_database/test_csi_name
```

Example result:
`/home/pi/csi_database/test_csi_name-2026-10-15-12_34_56.db`.
The suffix uses the Pi's local start time. Missing folders are created.
An existing file with the same complete name is never overwritten.

## Read the log and data

```bash
tail -f /home/pi/secure_mount/logs/csi_extract.log
ls -lh /home/pi/secure_mount/data/csi_daily
ls -lh /home/pi/secure_mount/data
```

The periodic status reports `received`, `stored`, `pending`, `rejected` and
`filtered`. When forwarding is enabled, it also reports TCP `sent`, `failed`,
`dropped` and `pending` frame counts separately for each destination. `sent` means the TCP send completed, not that the
server confirmed database storage; the legacy protocol has no acknowledgement.

If installed, use SQLite to inspect a database (replace the example date):

```bash
sqlite3 /home/pi/secure_mount/data/csi_daily/csi_2026-10-15.db 'SELECT COUNT(*) FROM CSI_frame;'
sqlite3 /home/pi/secure_mount/data/csi_daily/csi_2026-10-15.db 'SELECT id, hex(mac_addr) FROM MAC_addr;'
```

The `gain` column is retained for Nexmon because the firmware provides actual
gain measurements used by the existing control server. CSI values are uncompressed
little-endian int16 real/imaginary pairs, with 56/114/242 effective subcarriers for
20/40/80 MHz respectively. Timestamps are UTC microseconds.

SQLite may create `-wal` and `-shm` files during recording. Do not delete these
while capturing. Stop cleanly before copying the database; shutdown requests a
WAL checkpoint, although another database reader can delay truncation.

## Stop background capture

```bash
sudo /home/pi/secure_mount/rpi-csi-rx/csi_extract.sh --stop
```

This requests a graceful stop of the capture on `NEXMON_IFACE`, waits for it to
finish, and disables firmware CSI collection. Use the same `--env-file` if you
started with a custom environment file. Wait for the final capture status before
copying data or powering off. Forwarding has a shared three-second drain budget
by default (`SEND_SHUTDOWN_TIMEOUT_SEC`). Remaining sender subprocesses are then
stopped, with up to two additional seconds for cleanup. Remote copies still
pending are counted as dropped; locally committed frames remain saved.

## Troubleshooting

- **No Nexmon packets:** check `iw dev`, `iw phy`, the patched firmware, and that
  the transmitter is sending Wi-Fi traffic on the configured channel/bandwidth.
  This extractor does not generate transmitter traffic or reconnect Wi-Fi.
- **All frames filtered:** verify the actual transmitter MAC, or temporarily set
  `TX_MAC_ADDRESS=` and restart.
- **Frames rejected:** check the bandwidth and firmware version. This parser
  expects the 70-byte Gain Recovery V2 header used by the old codebase; stock
  Nexmon's 18-byte header is not interchangeable.
- **mcp size mismatch:** the default `NEXMON_CONFIG_LENGTH=106` retains the old
  launcher's `-l106`. Use the `mcp` and parameter length matching your installed
  firmware. A reported size mismatch is detected before radio changes.
- **TCP failures:** verify `CONTROL_IP`, `TCP_PORT` and the server's compatible plain
  TCP listener. Local recording continues. Failed or queue-dropped batches are
  not replayed automatically; the database retains the locally committed CSI.
- **Sender subprocess exited:** its destination drops new forwarded batches while
  local capture and other destinations continue. Restart capture to start a new
  worker after resolving the logged error.
- **Legacy daily database has no MAC table:** older daily files may refer to MAC
  IDs stored in the old main `database.db`. Keep those files together. Start a
  named capture or select a new daily directory; this version refuses to guess
  old MAC mappings.
- **Database write error or disk full:** capture stops with an error. Free space
  and restart. This version does not automatically delete historical databases.
