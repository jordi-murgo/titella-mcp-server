# titella-mcp-server

Master side of the Titella KVM auditing rig: the MCP server that owns the
ESP32-S3 link and drives the controlled machine.

```
Slave machine ──HDMI──> UVC grabber ──USB──> Master (pixels)
Master ──UART0 (WCH bridge)──────────────> ESP32-S3 ──native USB (HID + CDC)──> Slave machine
```

- **HID** (keyboard, relative and absolute mouse) is executed by the firmware;
  the router never accepts HID from the slave side.
- **CDC** carries shell, system, files and transfers, served by the ephemeral
  slave agent from the `titella-slave` project.
- **Screen** capture reads the local UVC grabber with `ffmpeg`; the slave takes
  no part and exposes no network service towards the master.
- **Shared MCP**: `titella-http` is the only process that creates the serial
  link, the shell and files clients and the HID lease. Each `titella-mcp`
  wrapper bridges its stdio session to that process over loopback Streamable
  HTTP.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install --find-links vendor .
.venv/bin/titella-mcp --help
```

Installed scripts: `titella-mcp`, `titella-http` and `titella-probe`.

The slave payloads are consumed from the exact `titella-slave-assets==1.0.0`
wheel vendored in `vendor/` (`--find-links vendor` resolves it without an
external index); the master builds no slave artifact and embeds no slave
implementation source. The wheel is produced by the `titella-slave`
repository; do not hand-edit it.

## Configuration

Precedence: `--config` > environment variables > `~/.config/titella/config.toml` > defaults.

| Variable | Default | Purpose |
|---|---|---|
| `TITELLA_CONFIG` | — | Alternative TOML path |
| `TITELLA_TRANSPORT` | `serial` | Master→ESP32 transport: `serial` (UART) or `wifi` (TCP over the SoftAP) |
| `TITELLA_SERIAL_PORT` | autodetect | Controller UART port (`serial` only) |
| `TITELLA_SERIAL_BAUD` | `115200` | Master↔ESP32 UART baud rate; must match the flashed firmware |
| `TITELLA_WIFI_HOST` | `192.168.4.1` | ESP32 SoftAP IP (`wifi` only) |
| `TITELLA_WIFI_PORT` | `5555` | Firmware TCP port (`wifi` only) |
| `TITELLA_SCREEN_DEVICE` | `USB3 Video` | Grabber device name (`ffmpeg -f avfoundation -list_devices true -i ""`) |
| `TITELLA_SCREEN_INPUT_FORMAT` | `avfoundation` on macOS, `v4l2` on Linux | `ffmpeg` input backend |
| `TITELLA_SCREEN_PIXEL_FORMAT` | `uyvy422` | Pixel format negotiated with the grabber (UVC rejects `yuv420p`) |
| `TITELLA_SCREEN_FRAMERATE` | `30` | Capture rate; must be a mode the card supports |
| `TITELLA_STATE_DIR` | system temp | Lease, resolution, frames and `link_id` |
| `TITELLA_KEYBOARD_LAYOUT` | `es_ES` | HID layout preference reported in `titella_list` (`es_ES` or `en_US`); `slave_bootstrap` never uses it as a fallback |
| `TITELLA_SLAVE_PLATFORM` | unset | Slave platform: `macos`, `linux` or `windows`. It is never inferred from the master |
| `TITELLA_SLAVE_CDC_PORT` | unset | Native CDC of the slave; the loader probes its own OS when empty (`COM3` on Windows) |
| `TITELLA_TERMINAL_MODS` | platform profile | Modifiers of the shortcut that opens a terminal on the slave (`CMD` on macOS/Windows) |
| `TITELLA_TERMINAL_KEY` | platform profile | Terminal shortcut key (`SPACE` on macOS, `R` on Windows; required on Linux) |
| `TITELLA_LAUNCHER_TEXT` | platform profile | Text typed into the launcher; an explicit empty value types nothing |
| `TITELLA_HEARTBEAT_S` | `5` | Heartbeat period |
| `TITELLA_DISCONNECT_GRACE_S` | `30` | Reconnection window before cleanup |
| `TITELLA_HTTP_PORT` | `8765` | Loopback port of the shared MCP server |
| `TITELLA_DEFAULT_TITELLA` | unset | Default `[titella.<id>]` when multiple instances are configured |

TOML example (single or multi-titella via `[titella.<id>]`):

```toml
state_dir = "~/.local/state/titella"
http_port = 8765
default_titella = "omarchy"

[titella.omarchy]
transport = "serial"
serial_port = "/dev/cu.wchusbserial<serial>"
serial_baud = 2000000
screen_device = "USB3 Video"
screen_pixel_format = "uyvy422"
screen_framerate = 30
keyboard_layout = "es_ES"
slave_platform = "linux"
terminal_mods = "CMD"
terminal_key = "RETURN"
launcher_text = ""

[titella.winlab]
transport = "wifi"
wifi_host = "192.168.4.1"
wifi_port = 5555
screen_device = "1"
slave_platform = "windows"
```

The slave platform is a hard selection: `slave_bootstrap` fails with
`slave_platform_required` instead of guessing from the master. Each platform
has a bootstrap profile:

| Platform | Implementation | Launcher | Loader / hello waits |
|---|---|---|---|
| `macos` | Python zipapp | `CMD`+`SPACE`, text `Terminal` | 20 s / 20 s |
| `linux` | Python zipapp | the configured `TITELLA_TERMINAL_MODS`/`TITELLA_TERMINAL_KEY` binding; no text by default | 20 s / 20 s |
| `windows` | C# on PowerShell 5.1 | `CMD`+`R`, text `powershell.exe -NoLogo -NoProfile` | 30 s / 90 s |

The per-field configuration overrides the profile; on Linux a missing binding
fails with `slave_launcher_required` before any keystroke, and a Linux
launcher text stays empty because the shortcut already launches the terminal.
The slave screen must be unlocked while bootstrapping.

The keyboard layout is a mandatory precondition of every attempt, never a
default: verify the target's active input source on the focused window with
`screen()`, then call
`slave_bootstrap(owner, platform=..., layout="es_ES"|"en_US", layout_confirmed=true)`.
A missing layout answers `slave_layout_required`, an unsupported one
`invalid_argument` and an unconfirmed one `slave_layout_unconfirmed`, each
before any keystroke; the configured `keyboard_layout` is never a fallback.
The attestation is a caller claim, not a measured proof, and a launcher window
transition can change the per-window input source; stop instead of confirming
what cannot be established. The full procedure is in `getting-started`.

## MCP tools

Every operational tool accepts an optional `titella: str | None = None` parameter to target a specific `[titella.<id>]` instance (omitting it uses `default_titella` or the sole registered instance).

**Registry and discovery**

| Tool | Purpose |
|---|---|
| `titella_list` | Registered `[titella.<id>]` instances, default selection, transport, lease and agent readiness. |
| `titella_discover` | Safe discovery of candidate WCH/Espressif serial ports and local UVC capture devices without probing unknown ports. |
| `titella_upsert` | Creates or updates `[titella.<id>]` in `config.toml` and hot-reloads it without interrupting other active instances. |
| `titella_remove` | Removes `[titella.<id>]` (refuses with `titella_busy` if its slave agent is active unless `force=True`). |
| `titella_set_default` | Sets `default_titella` in `config.toml` and in memory. |

**Diagnostics**

| Tool | Purpose |
|---|---|
| `kvm_status` | Aggregate state (firmware, lease, agent, capture) plus `next_steps`. Use it before diagnosing blindly. |

**Screen and lease**

| Tool | Purpose |
|---|---|
| `screen` | PNG from the UVC grabber plus capture resolution. |
| `lock_acquire` / `lock_renew` / `lock_release` / `lock_status` | Logical HID lease. `lock_release` does **not** close the UART or remote sessions. |

**HID** (require a current lease)

| Tool | Purpose |
|---|---|
| `mouse_abs`, `mouse_rel`, `click`, `wheel` | Absolute mouse in capture pixels, relative movement, click and scroll. |
| `key`, `type` | Key with modifiers and literal text. |
| `keyboard_layout` | Selects `es_ES` or `en_US` before typing. |

**Documentation** (no rig, lease or agent required)

| Tool | Purpose |
|---|---|
| `docs_index` | Catalog of the seven packaged documents with `id`, title, description, `uri`, `source` and `version`. |
| `docs_read(document_id)` | Full Markdown of one document with its provenance. |

The same documents are exposed as fixed resources `titella://docs/<id>`
(`text/markdown`) over both the stdio wrapper and the shared HTTP daemon.
Identifiers are catalog keys, never paths or URLs; unknown ids answer
`doc_not_found` and a missing or oversized packaged asset answers
`docs_unavailable`.

**Slave agent**

| Tool | Purpose |
|---|---|
| `slave_bootstrap(owner, layout=None, platform=None, cdc_port=None, layout_confirmed=false)` | Deploys the agent of the selected platform over HID + CDC. Requires the HID lease and an explicitly verified layout with `layout_confirmed=true`. |
| `slave_status` | State of the last bootstrap, including the selected platform and implementation. |
| `slave_stop` | Agent's own cleanup and exit. |

**One-shot shell**

| Tool | Purpose |
|---|---|
| `remote_shell(cmd, sync_timeout=10, timeout=60, cwd=None)` | Returns `finished` with `stdout`/`stderr`/`returncode`, or `running` with `job_id`. |
| `remote_shell_result(job_id)` | `running`, `finished`, `error` or `not_found`. |
| `remote_shell_cancel(job_id)` | Idempotent cancellation of the process group. |

**PTY terminal**

| Tool | Purpose |
|---|---|
| `remote_terminal_open(cwd, shell, rows, cols, idle_timeout=900)` | Opens the PTY. |
| `remote_terminal_read(session_id, after=0, wait_seconds=1, max_bytes=32768, encoding="utf-8")` | Returns `next_offset` and, if the consumer fell behind, `dropped_before`. |
| `remote_terminal_write(session_id, data, encoding)` | `encoding="base64"` for exact bytes. |
| `remote_terminal_resize(session_id, rows, cols)` | Remote `TIOCSWINSZ`. |
| `remote_terminal_close(session_id)` | Kills the process group; idempotent. |

**Files, system and transfers**

| Tool | Limit |
|---|---|
| `remote_fs_read` | 32 KiB by default, 64 KiB maximum per page. |
| `remote_fs_write` | 64 KiB; use `remote_upload` for more. |
| `remote_fs_list` | 200 entries per response, sorted. |
| `remote_fs_delete` | File or symlink only; never recursive. |
| `remote_fs_replace` | Exactly one occurrence. |
| `remote_fs_search` / `remote_fs_find` | 200 / 500 results, 4 MiB read per operation. |
| `remote_sysinfo` | System, Python, uid, gid and home. |
| `remote_upload`, `remote_download` | Up to 512 MiB, 512-byte blocks with an 8-block window and per-block ACK, SHA-256 and atomic rename. |
| `remote_transfer_status`, `remote_transfer_cancel` | Progress and cancellation with partial cleanup. |

Errors return `code`, `message`, `retryable` and, when they exist,
`request_id`/`session_id`. A transport failure never becomes an empty shell
result.

## Usage

```bash
export TITELLA_SERIAL_PORT=/dev/cu.wchusbserial<serial>
titella-probe            # exclusive UART diagnostic; stop the MCP daemon first
titella-probe --simulate # framing without hardware
titella-mcp              # stdio wrapper; reuses or starts the shared server
titella-http             # FastAPI server in the foreground, manual operation only
```

Each `titella-mcp` process keeps only its own stdio MCP session. The first one
takes a private lock, starts `titella-http` detached if `/health` is
unavailable, and connects to `http://127.0.0.1:8765/mcp`. Later wrappers reuse
the same PID and therefore the same owner of UART, jobs, PTYs and transfers.
The daemon does not exit when an agent runtime disconnects.

The server writes its startup log to `TITELLA_STATE_DIR/http-<port>.log`. If the
port belongs to another service or another version, the wrapper fails without
killing the process. After upgrading the package, send `SIGTERM` to the PID
published by `/health`; the next wrapper starts the new version.

Typical flow:

1. `kvm_status` reports what is missing (firmware, lease, agent, capture)
   before acting.
2. `screen` calibrates the resolution.
3. `lock_acquire` reserves the HID lease.
4. `slave_bootstrap` (once per session) starts the agent: verify the focused
   window's active input source first, then pass the verified `layout` with
   `layout_confirmed=true`.
5. `remote_shell`, `remote_terminal_*` and `remote_fs_*` do not need the HID
   lease.
6. `slave_stop`, or a disconnect longer than 30 s, removes processes,
   temporary files and the payload.

## Documentation

Procedures are packaged with the code and served over MCP; they are not
duplicated in this README. Canonical sources:

- server documents: `src/titella/_docs/` (`getting-started`, `operation`,
  `troubleshooting`, `mcp-transport`), shipped as package data of the
  `titella` wheel;
- slave documents: `docs/` of the `titella-slave` repository
  (`bootstrap-python`, `bootstrap-windows`, `serial-protocol`), shipped inside
  the pinned `titella-slave-assets` wheel and read from there.

Both transports (stdio and streamable HTTP) expose the same catalog through
`docs_index`/`docs_read` and the `titella://docs/<id>` resources. Start with
`getting-started`; the platform bootstrap guide and `troubleshooting` cover a
failed session. Documentation never reports live state — use `kvm_status`.

## Trust model

There is no security boundary between master and slave beyond physical control
of the cables. The UART/CDC link is plaintext by explicit decision: any process
with access to those ports can observe or inject commands with the slave
user's privileges. The SoftAP TCP variant replaces the cable with a WPA2
network whose passphrase is the access control. `link_id` correlates a session
and allows resuming a short replug; it neither authenticates nor encrypts.

Streamable HTTP listens on `127.0.0.1` only and has no authentication: any local
process that can connect to the port has the same operational capability as an
MCP client. Capture only runs `ffmpeg` against a local UVC device attached to
the master.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```
