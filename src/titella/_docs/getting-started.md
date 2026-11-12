# Getting started

This guide describes the rig topology, the prerequisites and the explicit
selection model. Reading documentation never touches the hardware: it reports
procedures, not live state. Use `kvm_status` for the current state of the rig.

## Topology

```
Slave machine ──HDMI──> UVC grabber ──USB──> Master (pixels)
Master ──UART0 (WCH bridge)──────────────> ESP32-S3 ──native USB (HID + CDC)──> Slave machine
```

Three components cooperate:

- **Master** (this package): owns the serial link, the HID lease, the screen
  capture and the MCP surface. It runs the `titella-http` shared daemon and
  one `titella-mcp` stdio wrapper per MCP client.
- **ESP32-S3**: the firmware router. It executes HID frames locally and
  forwards the other channels between the master port (UART0 or the WPA2
  SoftAP TCP client) and the selected slave destination: the native USB CDC
  by default, or the vendor-defined HID data collection once
  `slave_transport = "hid"` is selected.
- **Slave**: the controlled machine. The ephemeral agent is deployed by
  `slave_bootstrap` from the immutable `titella-slave-assets` wheel and speaks
  protocol v1 over the CDC (or the HID data transport).

There are two cables plus one video path: the master→ESP32 link (UART or
SoftAP TCP), the ESP32→slave native USB link (HID + CDC), and the
slave→grabber→master HDMI path used by `screen`.

## Prerequisites

Before any operation:

1. The ESP32-S3 runs the Titella firmware answering protocol v1; `kvm_status`
   reports whether it answers and which version it announces.
2. The UVC grabber is attached to the master and enumerated as the configured
   capture device (`TITELLA_SCREEN_DEVICE`, `USB3 Video` by default).
3. The slave machine is powered on, its graphical session is active and
   **unlocked**, and it exposes the native CDC of the ESP32-S3.
4. The slave platform is known and selected explicitly (below). It is never
   inferred from the master's own operating system.

## Explicit platform and layout selection

`slave_bootstrap(owner, layout=None, platform=None, cdc_port=None, layout_confirmed=false)`
requires a platform: the `platform` argument, else `slave_platform` in
configuration, else the call fails with `slave_platform_required` before any
keystroke. Valid values are `macos`, `linux` and `windows`. Each platform
deploys a different payload and launcher profile:

| Platform | Implementation | Launcher | Loader / hello waits |
|---|---|---|---|
| `macos` | Python zipapp | `CMD`+`SPACE`, text `Terminal` | 20 s / 20 s |
| `linux` | Python zipapp | the configured terminal binding; no text by default | 20 s / 20 s |
| `windows` | C# on PowerShell 5.1 | `CTRL`+`ESC`, Start search for `powershell` | 30 s / 90 s |

On Linux the terminal shortcut is configuration, not a default: without
`terminal_mods` and `terminal_key` the call fails with
`slave_launcher_required` before any keystroke.

The ESP32→slave leg is a separate selection, `slave_transport`:

| Value | ESP32→slave leg | Requirements |
|---|---|---|
| `cdc` (default) | native USB CDC the loader opens | any platform |
| `hid` | vendor HID data collection, reports `0x10`/`0x11` | `transport="serial"`, `agent_protocol="serial"`, `slave_platform="windows"`, no `slave_cdc_port`, firmware 1.6.0+ |

It is never inferred and never falls back: an old firmware answers
`slave_transport_unsupported` before any keystroke, and the HID bootstrap
requires an operator-prepared console (`skip_launch=true`) because it types
only a short stage-0 line and launches nothing. Switch it with
`titella_upsert(titella=..., slave_transport="hid")`; switching retires a
pinned `slave_cdc_port` atomically and is refused while an agent is live
unless `force=true`. The master's UART to the ESP32 always runs at 2,000,000
baud, matching the flashed firmware.

The keyboard layout is a second, independent precondition: `layout` is
mandatory and the configured `keyboard_layout` is never used as a fallback.

## Verified keyboard layout

`slave_bootstrap` types over HID, so the active input source of the window that
receives the launcher decides whether every typed byte lands correctly. A real
incident garbled a loader on an `en_US` target with an `es_ES` HID selection.
The call therefore requires:

- an explicit `layout` (`es_ES` or `en_US`) — a missing one answers
  `slave_layout_required`;
- an unsupported value answers `invalid_argument`;
- a real boolean `layout_confirmed=true` attesting a fresh verification on this
  attempt — absent or `false` answers `slave_layout_unconfirmed`.

All three reject before any keystroke. The attestation is a caller claim, not a
measured proof: the server cannot inspect the target's input source, so an
unverified confirmation is indistinguishable from a verified one.

Verify, fresh for every attempt:

1. Inspect the **focused input window** on the target with `screen`. The OS UI
   language, the configured layout and a historical screenshot do not prove the
   active input source of the window that will receive the keystrokes. An input
   source can be per window: a desktop indicator says nothing about the shell a
   launcher is about to open.
2. If the capture is inconclusive, probe harmlessly: type a punctuation
   character that differs between the layouts (for example `-` or `:` on the ES
   layout), inspect it with `screen`, and erase it. Never execute a probe.
3. If the layout cannot be established, or the value is unsupported, stop. Do
   not confirm what was not observed.
4. The bootstrap opens a launcher and then a new terminal/PowerShell window.
   It types the stub without pausing for another screenshot. Before attesting,
   establish that the same input source will apply to both windows; if
   per-window input sources might differ and this cannot be established, stop.
   A preflight screenshot alone cannot prove the future console layout.
5. Recheck after any change of input source or focus before a new attempt.
   Only then call bootstrap with the verified `layout` and
   `layout_confirmed=true`.

Keep the OS preference and set the HID map to match it: choosing the layout the
operator prefers on the slave, instead of the one measured, recreates the
mismatch the precondition exists to prevent.

Consult `docs_read("bootstrap-python")` or `docs_read("bootstrap-windows")`
for the platform-specific bootstrap before calling `slave_bootstrap` with
that platform for the first time.

## Documentation and operation are different actions

- Documentation tools (`docs_index`, `docs_read` and the
  `titella://docs/<id>` resources) return packaged procedure text. They never
  query the gate, the lease, the firmware or the slave, and they work with
  the rig powered off.
- Operational tools act on hardware: `kvm_status`, `screen`, `lock_*`, HID,
  `slave_*`, `remote_*`. They depend on the physical rig and on the agent
  being ready.
- `kvm_status` is the bridge: it aggregates the live state (firmware, lease,
  agent, capture) and lists the missing prerequisites in order.

## First session, tool by tool

Requirements: rig wired as above, daemon reachable, slave unlocked.

1. `kvm_status` — observable result: an aggregate with `firmware`, `lease`,
   `agent` and `capture` states plus `next_steps`. If `firmware.reachable` is
   false, stop here and fix the transport before touching the target.
2. `screen` — observable result: a PNG plus the capture resolution. A black
   frame means the slave emits no signal (off, suspended, HDMI unplugged),
   not a capture failure. With the agent running, `screen(source="agent")`
   returns the same shape from the agent's own interactive desktop instead of
   the grabber (see `docs_read("operation")`).
3. `lock_acquire(owner)` — observable result: the HID lease held by `owner`
   with its expiry. HID and `slave_bootstrap` require it.
4. `slave_bootstrap(owner, layout="es_ES"|"en_US", platform="...",
   layout_confirmed=true)` — verify the focused window's active input source
   first (see "Verified keyboard layout"). Observable result: `status="ready"`
   with `platform` and `implementation`. On failure the `error.code` names the
   stage (see `docs_read("troubleshooting")`).
5. `remote_shell`, `remote_terminal_*`, `remote_fs_*`, `remote_upload`,
   `remote_download` — these do not need the HID lease; they need the agent
   ready. See `docs_read("operation")`.
6. `slave_stop(owner)` — observable result: `status="stopped"` and the agent
   removes its payload, temporaries and bootstrap directory.

## Where to go next

| Document | Read before |
|---|---|
| `operation` | driving an already-bootstrapped rig |
| `troubleshooting` | any failed bootstrap or disconnected agent |
| `mcp-transport` | running or diagnosing the shared daemon |
| `bootstrap-python` | first `platform="macos"` / `platform="linux"` |
| `bootstrap-windows` | first `platform="windows"` |
| `serial-protocol` | implementing or debugging the v1 link |

## Errors and recovery

| Symptom | Meaning | Recovery |
|---|---|---|
| `slave_platform_required` | no platform selected | pass `platform=` or set `slave_platform` |
| `slave_layout_required` | no explicit layout for bootstrap | verify the focused window's input source, then pass `layout=` |
| `slave_layout_unconfirmed` | layout passed without a fresh attestation | pass `layout_confirmed=true` only after verifying this attempt |
| `hid_lease_required` / `hid_lease_busy` | lease missing or held by another owner | `lock_acquire(owner)`; wait or agree with the other owner |
| `agent_not_ready` | agent not bound | run `slave_bootstrap`; check `slave_status` |
| firmware unreachable in `kvm_status` | transport or wiring | fix `TITELLA_TRANSPORT` and the port before retrying |

Documentation cannot diagnose the physical rig: when a procedure fails,
`kvm_status` and `docs_read("troubleshooting")` are the pair that turns a
symptom into the next concrete action.
