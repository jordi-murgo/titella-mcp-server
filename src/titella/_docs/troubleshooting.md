# Troubleshooting

Symptoms, their causes and the next concrete action. Use `kvm_status` first:
it aggregates firmware reachability, lease, agent and capture state plus
`next_steps` in resolution order. Reading this document performs no
operation.

## Diagnostic order

```
kvm_status
  firmware.reachable?  no -> fix transport (serial port / SoftAP TCP)
  lease.locked?        no -> lock_acquire(owner)
  agent.status?        no -> verify the focused window's input source, then
                            slave_bootstrap(owner, platform=..., layout=...,
                            layout_confirmed=true)
  capture.resolution?  no -> screen; check the UVC grabber on the master
```

`next_steps` is already ordered by that dependency chain; treat it as the
authoritative checklist for the current session.

## The agent is not ready

Symptom: `remote_shell`, `remote_terminal_open`, all `remote_fs_*` and
transfer tools answer `agent_not_ready` immediately, without waiting for a
transport timeout.

Cause: the availability gate is closed. It opens only after
`control.hello` → `control.bind` → `control.ready`, i.e. after a successful
`slave_bootstrap` or after the daemon adopts an agent that survived a daemon
restart and re-announces itself. It closes on `slave_stop`, on a failed
bootstrap, and when the lease cannot be verified.

Recovery: `slave_status` shows the last bootstrap phase and the selected
platform. If it is `not_started`, bootstrap; if `error`, read its
`error.code` and fix the named stage; if the agent is alive but the gate
closed, bootstrap again — an existing agent with a different `link_id` does
not take over (`takeover_denied`).

## slave_bootstrap refuses to start: layout preconditions

Symptom: `slave_bootstrap` answers `slave_layout_required`,
`slave_layout_unconfirmed`, or `invalid_argument` naming the layout, and the
agent state stays `not_started`.

Cause: these are preconditions, not rig failures. The bootstrap types over HID,
so it requires the target's active keyboard layout, verified on the focused
input window of this attempt, as an explicit `layout` plus a real
`layout_confirmed=true`. The configured `keyboard_layout` is never used as a
fallback, an unsupported value is `invalid_argument`, and a truthy non-boolean
confirmation is not accepted. The attestation is a caller claim, not a measured
proof: the server cannot verify your observation.

Recovery: inspect the focused window with `screen` — not the OS UI language,
not the configured layout, not a historical capture. An input source can be per
window, and the launcher opens a new window (Spotlight, terminal, PowerShell);
if the layout of the window that will receive the keystrokes cannot be
established, stop instead of confirming. An inconclusive capture may be probed
with a layout-specific punctuation character that is then erased; never execute
a probe. After any input-source or focus change the previous verification is
stale: verify again before retrying.

## The HID lease is held by someone else

Symptom: `hid_lease_busy` (retryable) from `lock_acquire`, or
`hid_lease_required` from a HID tool or `slave_bootstrap`.

Cause: another `owner` holds the logical lease, or this process never
acquired it. The lease is logical, not a physical lock: it only coordinates
clients of the same daemon.

Recovery: `lock_status(owner)` returns the current holder and expiry. Wait
for expiry, ask the other owner to release, or use `lock_renew` if you are
the holder. `lock_release` does not close the UART or the remote sessions, so
releasing between phases is safe.

## Loader timeout versus hello timeout

`bootstrap_loader_timeout` means the loader never announced
`loader.ready`; `agent_hello_timeout` means the payload was received and
started but never announced `control.hello`. They point at different halves
of the pipeline.

| Code | Boundary crossed | Typical cause | Next action |
|---|---|---|---|
| `bootstrap_loader_timeout` | launcher and loader, before the CDC opens | launcher typed into the wrong window; slave session locked; CDC permissions; Windows policy block before the port opens | check focus, unlock the slave, fix CDC permissions (below) or Windows policy; retry |
| `agent_hello_timeout` | loader received the payload; the agent did not start or did not announce | runtime missing on the slave; payload refused; Windows compile error; wrong link | platform-specific bootstrap guide; for Windows the compile log in the bootstrap directory |
| `size_mismatch` / `checksum_mismatch` | payload transfer | corrupted transfer | retry from scratch; the partial is removed |
| `takeover_denied` | agent already alive under another `link_id` | previous session not stopped | `slave_stop`, or wait for the grace window |

The per-platform waits are fixed: 20 s loader / 20 s hello on macOS and
Linux, 30 s loader / 90 s hello on Windows (the 90 s include compiling the
C# payload). Do not "fix" a timeout by raising `sync_timeout` on unrelated
tools.

## CDC permissions on macOS and Linux

The loader opens the native CDC (`/dev/cu.usbmodem*` on macOS,
`/dev/ttyACM*` on Linux). On Linux the device is owned by `root:uucp`.

- Add the slave user to `uucp` (`usermod -aG uucp <user>` or
  `gpasswd -a <user> uucp`).
- Group membership is inherited at session start: log out and back in (or
  reboot). An already-open graphical session keeps the old groups, so the
  loader keeps failing with `cdc_not_found` even though the device node is
  correct.
- On macOS, check that the port belongs to the slave user and that no other
  process (a terminal multiplexer, a serial monitor) holds it.

If the platform's terminal shortcut does not exist, the HID keystrokes land
in whatever window has focus and the loader never starts. On Linux the
terminal binding is explicit configuration (`TITELLA_TERMINAL_MODS`,
`TITELLA_TERMINAL_KEY`); without it, `slave_launcher_required` is returned
before any keystroke.

## Windows: COM port, policies and compile

- **Port selection.** With no explicit `slave_cdc_port`, the loader inspects
  the PnP `Ports` class and each port's ancestor chain, and accepts only a
  port whose product is `Titella KVM` or whose serial starts with `TITELLA-`.
  One candidate is used; zero yields `cdc_not_found`; several yield
  `cdc_ambiguous`. Both are fixed by passing `slave_cdc_port` explicitly
  (`COM[1-9][0-9]*`, for example `COM5`). The agent reuses that port when
  the link reconnects.
- **No PnP identity.** If the Windows build does not expose the device
  identity through PnP, the loader refuses to guess and returns
  `cdc_not_found` with the explicit-override instruction. Pass
  `slave_cdc_port`; do not weaken the filter.
- **Policy blocks.** The loader requires interactive PowerShell 5.1 in
  `FullLanguage` and .NET Framework 4.7.2 or later; the agent is compiled at
  run time with `Add-Type`. If a policy blocks the language mode, the
  framework version or `Add-Type`, the loader fails visibly and the master
  reports `bootstrap_loader_timeout` (when nothing opened the CDC) or
  `agent_hello_timeout` (when compilation started but failed). Titella never
  changes execution policy, never bypasses AppLocker/WDAC/AMSI, never asks for
  elevation and never downloads anything. When a policy blocks the bootstrap,
  the honest outcome is a refusal with the concrete requirement reported to
  the operator — not a workaround.
- **Compile errors.** The child PowerShell writes its diagnostics under the
  agent's own `titella-bootstrap-*` directory (`agent-compile.err.log`). If
  `FreeConsole` ran, those diagnostics remain the only record.

## Reconnection, grace window and a single TCP client

- Master and agent heartbeat every 5 s. Sessions and transfers survive a
  link loss for a 30 s grace window and only if the same process and the same
  `link_id` reappear. After the window the agent cleans up and exits, and the
  master marks the work disconnected.
- On the SoftAP transport there is exactly one TCP client. A new connection
  **replaces** the previous one: the ESP32 cuts the old client and discards
  whatever remained to be sent. Therefore do not "probe" the TCP socket while
  the daemon is connected — a probe silently evicts the daemon.
- On the serial transport, `titella-probe` opens the UART exclusively. Stop
  the shared daemon (SIGTERM to the PID published by `/health`) before
  running it; two owners of the UART produce truncated or interleaved frames,
  not a clean error.
- After reflashing firmware that changes the USB identity, Windows may assign
  a different COM number. Re-run `slave_bootstrap` with the current port; do
  not keep a stale `slave_cdc_port` for a device that no longer exists.

## Capture problems

`screen` failures are local to the master: `screen_capture_failed` is
retryable and usually means the device name, input format or pixel format is
wrong, or the grabber is held by another process. A valid but black frame is
not a failure: it means the slave is off, suspended, or the HDMI cable is
unplugged. The capture device is fixed hardware ("USB3 Video" by default);
captures are occasional and on demand.

## Transport and daemon problems

- If the wrapper reports a foreign service or a stale version on the port,
  the daemon it found is not this package's or is an older build. Do not kill
  an arbitrary process: read `/health` for `service`, `version` and `pid`, and
  send SIGTERM to that PID before the next wrapper starts the new build.
- If the firmware is unreachable, check `TITELLA_TRANSPORT` (`serial` or
  `wifi`), the port, and the baud rate: master and firmware must use the same
  rate. `hid.health` reports the flashed `firmware`, `protocol` and
  `uart_baud`, so a stale firmware is distinguishable from a working one.
- A non-HID `route_error` invalidates the agent's ready state. The next control
  heartbeat attempts to bind the live agent again; until that succeeds, agent
  tools return `agent_not_ready` instead of operating against stale state.
  Firmware 1.3.1 and later queue complete master-to-CDC frames, so temporary
  CDC backpressure delays simultaneous requests rather than truncating them.
- A chip in reset emits no banner; a pre-v1 firmware answers without the v1
  envelope fields. Neither is a framing bug on the master side.

## Documentation is not state

If a document and `kvm_status` disagree, `kvm_status` wins: documents describe
procedures, not the live rig. Conversely, a documentation error
(`doc_not_found`, `docs_unavailable`) never indicates a rig problem — it
means the requested identifier is not a catalog key or the packaged asset is
missing or oversized. Reinstall the package; there is no download fallback.
