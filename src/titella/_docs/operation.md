# Operation

How to drive a Titella rig through the MCP surface once the daemon is up:
capture, lease, bootstrap, shell, terminal, files, transfers and shutdown.
A `transport = "window"` instance replaces the physical rig with one macOS
desktop window and keeps the screen and HID tools; it is described at the end
of this document. Reading this document performs no operation; every section
names the tool sequence, the observable result and the failure mode.

## Order of operations

The surface has one hard dependency chain:

```
kvm_status -> screen -> lock_acquire -> slave_bootstrap -> remote_* -> slave_stop
```

- `screen(source="uvc")` needs only the local grabber; it works with the agent
  down. `screen(source="agent")` needs the agent ready (the gate) and the
  interactive desktop.
- HID tools and `slave_bootstrap` need the lease held by the same `owner`;
  `release_all` is the single exception: it is a lease-free emergency stop.
- `remote_*` tools need the agent ready (the gate), not the lease.
- `screen_diff` and `screen_ocr` read the last capture, so they need a
  previous `screen` and nothing else.
- Documentation tools (`docs_index`, `docs_read`) need neither.

## Status: kvm_status

Call `kvm_status` before and between phases. Observable result: `firmware`
(reachability and announced version), `lease`, `agent` (bootstrap state,
platform, implementation), `capture` (resolution and age) and `next_steps`,
the pending prerequisites in resolution order. `next_steps` is the contract:
an empty list means the rig is operable end to end.

## Screen: screen

`screen` captures one frame and fixes the absolute-mouse resolution from it.
It has two sources and no automatic fallback between them:

- `source="uvc"` (default) reads one frame from the local UVC grabber. On a
  `transport = "window"` instance it captures the configured macOS window
  instead (see the window transport section).
- `source="agent"` asks a running agent for one native-resolution PNG of its
  **primary monitor**, origin `(0,0)`, captured inside the agent's own
  interactive session. `source` in the metadata is `"agent"`, `monitor` is
  `"primary"`, and `backend` names the mechanism: the Windows agent its own
  desktop, macOS `screencapture`, X11 ImageMagick or ffmpeg, Wayland the
  freedesktop portal.

Tool sequence: `screen` alone for UVC; `slave_bootstrap` → `screen(source="agent")`
for the agent path. Observable result: a PNG image plus `capture_resolution` in
pixels; `mouse_abs` coordinates are interpreted in that resolution, whichever
source produced it. Both sources write the same last frame and calibration.

Errors and recovery:

- `screen_capture_failed` with `ok: false, retryable: true` means the local
  grabber is absent, busy or misconfigured. Check `TITELLA_SCREEN_DEVICE`,
  the input format and that no other process holds the device; retry.
- A black but valid UVC frame means the slave emits no signal (powered off,
  suspended, HDMI disconnected). That is target state, not a failure.
- `agent_not_ready` from `source="agent"` means the gate is closed: run
  `slave_bootstrap`, or check `kvm_status`. The UVC path is never used as a
  substitute.
- `screen_unsupported` means the running implementation has no `fs.screen`
  (an old agent) or its platform has no usable capture path: no `screencapture`,
  no X11 tool, no `gdbus`, or no graphical session at all. The message names
  what is missing; nothing is approximated.
- `screen_permission_denied` means the session refused the capture: macOS
  Screen Recording not granted to the launching terminal, or a Wayland portal
  request denied or cancelled. Grant it and retry; it is not a link failure.
- `invalid_screen_capture` means the received bytes contradict the metadata
  (format or PNG dimensions). No stale frame replaces the previous one, so the
  calibration still describes the last good capture.
- `screen_capture_failed` with a message naming a truncated transfer means the
  link lost blocks while streaming the image; nothing partial is published and
  the call can be retried. This is a property of the UART/ESP32 hop under a
  sustained burst, not of the capture: no image below the 32 MiB bound is
  guaranteed to arrive intact, and a smaller desktop transfers at the same
  speed but with fewer blocks at risk.
- `screen_too_large` means the encoded image exceeds the 32 MiB receiver
  bound, which is enforced while receiving.
- `screen_state_failed` means the frame arrived but cannot be persisted.
- `screen_required` from `mouse_abs` means there is no previous capture: call
  `screen` first.

Boundaries of the agent path: the PNG crosses the ordinary `fs` stream, so the
metadata request keeps its 65 s ceiling and the transfer has 180 s after it;
a near-cap, high-entropy image can therefore time out. The image is never
resized to fit. Locked sessions, secure desktops, protected surfaces and
disconnected RDP sessions are not guaranteed capturable: a failure reports the
real diagnostic and a valid black PNG is not evidence of any particular cause.
The capture runs only when asked; there is no continuous stream, no background
loop and no screenshot file left on the slave.

Captures are occasional and on demand; there is no continuous stream.

UVC frames are cropped to the active content box when the capture card adds
pillarbox or letterbox bars: rows and columns that stay at or below 20 in
every channel are borders, the crop is applied only when the remaining
content is at least 64x64 px and some border is at least 4 px thick, and an
all-black frame is never cropped — a "no signal" capture still comes back as
a black image. When a crop happens, `capture_resolution` is the cropped size
(which is what `mouse_abs` calibrates against), while `raw_resolution` and
`crop_box` in the metadata describe where the crop came from.
`TITELLA_SCREEN_AUTOCROP=false` disables it.

Every capture also reports how it changed since the previous one. The
metadata carries `frame_sha256`, `unchanged_since_previous` and `diff`:
`changed` (bool), `bbox_px` (`{x, y, width, height}` of the changed area or
null), `changed_pixels` and `diff_ratio`. The diff has a per-channel noise
floor of 12, so capture-card noise does not turn every frame into a
full-screen difference; a resolution change (or a frame that cannot be
decoded) reports the whole image as changed. The previous frame is kept
beside the current one, so a diff never has to be re-derived.

## Screen diff: screen_diff

`screen_diff(padding_px=16)` returns the region of the last capture that
changed since the previous one, cropped to `diff.bbox_px` expanded by
`padding_px` and clamped to the frame. Observable result: a PNG plus
`bbox_px` (the padded box actually returned), `raw_bbox_px`, the
`full_resolution` and the diff envelope. Without a previous capture it
answers `screen_required`; when nothing changed it answers `changed: false`
with no image. `measure_diff=true` on a HID action attaches the same
envelope to that action's result after a `settle_ms` wait, which is the
cheapest way to confirm that a keystroke or click landed.

## OCR: screen_ocr

`screen_ocr(scope="diff", bbox=None, prompt=None)` reads text from the last
capture with the OCR model named in configuration (`ocr_endpoint`,
`ocr_model`, optional `ocr_api_key` and `ocr_timeout_s`, or the matching
`TITELLA_OCR_*` variables). `scope="diff"` sends the changed region padded by
16 px; without a baseline frame it falls back to the whole frame, and when
nothing changed it answers `changed: false` with empty text without calling
the model. `scope="full"` sends the whole frame. `scope="bbox"` sends
`{x, y, width, height}` inside the frame. Observable result: `text`,
`scope` (the effective one), `bbox_px` (null for a full frame) and `model`.

Errors and recovery: `ocr_not_configured` — set `ocr_endpoint` and
`ocr_model` (there is no fallback OCR engine); `screen_required` — run
`screen` first; `invalid_argument` — a scope/bbox violation;
`coordinates_out_of_bounds` — the bbox leaves the frame;
`ocr_request_failed` — the endpoint was unreachable, timed out or answered
something that is not a chat completion; retry or fix the configuration.

## Lease: lock_acquire, lock_renew, lock_release, lock_status

The lease is the logical keyboard-and-mouse ownership; it prevents two
operators from typing at the slave simultaneously.

Tool sequence: `lock_acquire(owner)` → HID operations → `lock_release(owner)`.
`lock_renew(owner, lease_s)` extends; `lock_status(owner)` inspects.

Observable result: `lock_acquire` returns `locked: true` with the owner and
expiry; a second `lock_acquire` from another owner fails with
`hid_lease_busy` (retryable). `lock_release` returns `released` and does not
close the UART or the remote sessions.

Errors and recovery: `hid_lease_busy` — wait for expiry or for the other
owner to release, or agree on ownership; the lease is logical, not a
physical lock. `lock_release` from a non-owner fails; use `lock_status` to
find the current owner.

## HID: key, type, click, mouse_abs, mouse_rel, wheel, keyboard_layout, release_all

`mouse_abs(owner, x, y, button=None, state="keep")` moves to pixels of the
last capture; `mouse_rel(owner, x, y, button=None, state="keep")` moves by
deltas between -127 and 127. `click(owner, button="left", double=false,
state="click")` performs a complete press/release by default. The optional
`button` (`left`, `right`, `middle`) and `state` (`keep`, `down`, `up`) press
or release a button around a move; `state="down"`/`"up"` without a button
defaults to `left`. Press, move with the button carried (`state="keep"`), then
release, is the drag-and-drop sequence. `wheel(owner, delta)` scrolls.
`key(owner, key, mods)` presses one key with modifiers.

`type(owner, text, delay_ms=10, enter=false)` validates the whole string
against the active layout before the first keystroke. Any character the
layout cannot produce answers `untypeable_characters` with the complete
`unreachable` list and the `layout` it was checked against, and nothing is
sent. Pure 7-bit ASCII goes through the firmware's own ASCII map; dead keys,
accents, AltGr characters (`€`, `ñ`, `ç`, `@`...) and the ISO 102nd key
(`<`, `>`, `\`) are emitted as raw HID steps, chunked to fit a wire frame.
`\r\n` and a lone `\r` are typed as one Enter. `enter=true` presses Enter
after the text.

`keyboard_layout(owner, layout)` switches the firmware between `es_ES` and
`en_US`, and the tracked layout used for validation follows it; the
configured `keyboard_layout` only seeds the initial guess, so switching the
firmware is always an explicit call.

`release_all` releases every held key, modifier and mouse button on both
pointer devices. It is idempotent, owns no lease and takes no `owner`: use it
when a key or button is stuck, a lease expired mid-action, or another session
wedged the input.

Errors and recovery: `hid_lease_required`/`hid_lease_owned` — acquire the
lease as the same owner; `screen_required` — run `screen` before `mouse_abs`;
`coordinates_out_of_bounds` — the point leaves the captured resolution;
`invalid_argument` — an unknown button or state, or an out-of-range
`mouse_rel`/`wheel` delta. An `ok:true` proves only that the firmware emitted
the frame, not that the slave processed it: confirm with `screen()` or pass
`measure_diff=true`.

## Bootstrap: slave_bootstrap, slave_status, slave_stop

`slave_bootstrap(owner, layout=None, platform=None, cdc_port=None,
layout_confirmed=false, skip_launch=false)` deploys the ephemeral agent of the
selected platform over HID and the configured slave transport. The platform is
mandatory (argument, then configuration, then `slave_platform_required`). The
layout is mandatory and independent of configuration: `layout` names the
active keyboard layout (`es_ES` or `en_US`) verified on the target's focused
input window during this attempt, and `layout_confirmed=true` attests that
verification. The configured `keyboard_layout` is never a bootstrap fallback.

A missing layout answers `slave_layout_required`, an unsupported value
`invalid_argument` and an unconfirmed layout `slave_layout_unconfirmed`; all
three reject before any keystroke. `cdc_port` overrides the CDC choice on the
slave, and `skip_launch=true` types into an operator-prepared console without
opening a launcher.

With `slave_transport = "hid"` (selected through `titella_upsert`) the leg
carries the loader inside the ESP32 Feature `0x11` mailbox: the bootstrap
first queries a fresh `hid.health` for the firmware identity and protocol,
stages the compressed loader over the UART, and types only the bounded stage-0
line. That path rejects an explicit `cdc_port`, a missing `skip_launch`, a
non-Windows platform and an old firmware (`slave_transport_unsupported`)
before any keystroke, and fails as `hid_loader_too_large`/`hid_stage0_too_large`
when the payload or the typed line exceeds its fixed budget. No COM port,
clipboard or network participates. The verification procedure is in
`docs_read("getting-started")` and in the platform-specific bootstrap guides.
The attestation is a caller claim, not a measured proof: stop instead of
confirming when the layout cannot be established.

Tool sequence: `lock_acquire` → `slave_bootstrap` → `slave_status` until
`ready`. Observable result: `status: "ready"` with `session_id`, `platform`,
`implementation`, `size` and `sha256`; `slave_status` reports the phase
(`not_started`, `typing_loader`, `waiting_loader`, `transferring`, `ready`,
`error`, `stopped`) and the selected platform, layout and implementation.

The bootstrap is a staged pipeline; each stage has its own timeout and error
code. Stage-by-stage behavior, waits and the platform-specific gates are in
`docs_read("bootstrap-python")` and `docs_read("bootstrap-windows")`. On any
failure the state records `error` with `code`, `message` and `retryable`, and
the gate stays closed; recovery is to fix the named cause and bootstrap
again. `slave_stop` asks the agent to clean up and exit (processes,
transfers, temporaries, bootstrap directory); a disconnect longer than the
grace period produces the same cleanup on the slave side.

## Shell one-shot: remote_shell, remote_shell_result, remote_shell_cancel

`remote_shell(cmd, sync_timeout=10, timeout=60, cwd=None)` runs one command
on the slave; the Windows agent runs it through PowerShell, the Python agent
through the POSIX shell.

Tool sequence: `remote_shell` → optionally `remote_shell_result(job_id)` /
`remote_shell_cancel(job_id)`. Observable result: `finished` with `stdout`,
`stderr` and the real `returncode` when the command ends within
`sync_timeout`; otherwise `running` with a `job_id`.

Errors and recovery: `agent_not_ready` — bootstrap first. `request_timeout`
with a `job_id` — the job is still running; poll `remote_shell_result`.
`session_limit` — the slave shares a limit of 4 concurrent processes between
jobs and terminals; cancel or wait. A transport failure is never turned into
an empty result.

## Terminal: remote_terminal_open, remote_terminal_read, remote_terminal_write, remote_terminal_resize, remote_terminal_close

An interactive terminal (PTY on macOS/Linux, ConPTY on Windows). Tool
sequence: `open` → `write`/`read` loop → `resize` as needed → `close`.

Observable result: `open` returns a `session_id`; `read(session_id, after=0,
wait_seconds=1)` returns `next_offset` and the new bytes (base64 for exact
bytes with `encoding="base64"`), and `dropped_before` when the consumer fell
behind; `resize` confirms the new geometry; `close` returns `closed` and is
idempotent.

Errors and recovery: `not_found` — the session expired (idle timeout or
cleanup); reopen. The slave output queue is bounded (256 KiB): if you read
too slowly the difference is reported as `dropped_before`, not silently
discarded. On Windows the agent is a ConPTY: control sequences are VT, do not
expect byte-identical output to a Unix PTY. Closing with much output pending
must not hang; if it does, the slave bug is the next_steps item, not your
client.

## Files: remote_fs_read, remote_fs_write, remote_fs_list, remote_fs_delete, remote_fs_replace, remote_fs_search, remote_fs_find, remote_sysinfo

Per-operation limits: read 32 KiB default / 64 KiB maximum per page; write
64 KiB (atomic); list 200 entries; search 200 matches / find 500 names over
at most 4 MiB read; `replace` requires exactly one occurrence; `delete` is
file or symlink only, never recursive and never a directory. Paths are native
to the slave (`~` expands to the user profile; Windows keeps its own path
semantics).

Tool sequence: any `remote_fs_*` independently once the agent is ready.
Observable result: `read` returns `data` with `next_offset`; `write` returns
`written` (partial content is never visible: the agent writes a temporary in
the destination directory and renames); `list` returns sorted entries with
pagination; `search`/`find` return bounded result lists; `sysinfo` returns
system, runtime and user fields (`python`, `uid`, `gid` are null on
Windows).

Errors and recovery: `not_found` — the path does not exist;
`permission_denied` — the slave user lacks access; `io_error` — an I/O
failure, message names the underlying problem; `invalid_argument` — a limit
violated (shrink the page); `directory_delete_denied` — delete does not
remove directories. Larger payloads (>2048 B) travel over the `fs` stream;
that is internal behavior and needs no extra call.

## Transfers: remote_upload, remote_download, remote_transfer_status, remote_transfer_cancel

Transfers move up to 512 MiB in 512-byte blocks with an 8-block window,
per-block ACK, SHA-256 verification and an atomic rename on both ends.

Tool sequence: `remote_upload(local_path, remote_path)` or
`remote_download(remote_path, local_path, checksum=None)` → optionally
`remote_transfer_status(transfer_id)` / `remote_transfer_cancel(transfer_id)`.

Observable result: `finished` with `transfer_id`, `bytes_total`,
`bytes_transferred` and `sha256` when the transfer completes and verifies;
`running` with the same progress fields when it continues past
`sync_timeout`. `remote_download` also accepts a `checksum` to verify
against.

Errors and recovery: `checksum_mismatch` — the bytes did not verify; the
destination keeps the previous file and only the temporary is removed;
retry. `not_found` (status) — unknown `transfer_id`; transfers are not
persisted across agent restarts. `invalid_argument` — a declaration above
512 MiB is rejected before any byte is written. Cancellation removes the
partials of that transfer only.

## Stop: slave_stop, and what survives

`slave_stop` (or losing the link beyond the grace window) makes the agent
kill its process trees, cancel transfers, delete its temporaries and payload,
remove its bootstrap directory and exit. Observable result: `status:
"stopped"`; `slave_status` then reports `stopped`. The lease, the daemon and
the serial link survive: `lock_release` and a later `slave_bootstrap` start a
new ephemeral session.

## Window transport

An instance with `transport = "window"` drives a desktop window of the
master's own macOS session instead of a physical rig: a remote-desktop
client, a VM window or any other application becomes the simulated slave.
Everything below about `screen`, `screen_diff`, `screen_ocr`, the lease and
the HID tools still applies; only the transport underneath changes.

Selection: `window_app` matches a substring of `kCGWindowOwnerName`,
`window_title` matches a substring of `kCGWindowName`, and `window_id`
addresses an explicit `CGWindowID`. At least one is required
(`window_app, window_title or window_id is required with transport=window`),
an exact application match wins over a substring match, and the largest
matching window wins among equals, which discards splash and HUD panels.
Window discovery only considers normal windows (layer 0) larger than 16x16.

`window_activation` selects what an action does to the operator's desktop:

- `quiet` (default) never raises the target and never changes the frontmost
  application. Every keyboard and mouse event is delivered straight to the
  target process with `postToPid`, so the window can sit behind other windows,
  on another Space or on another display while the master keeps working in the
  foreground application. The host cursor is not warped either: a
  `mouse_abs`/`click` reaches the window without moving the pointer the
  operator is using.
- `raise` activates the target application first, which is what a
  remote-desktop client does when it takes over the desktop.

A minimized window has no key window inside its process and silently drops
injected events. With `quiet`, each HID operation restores it with
`kAXMinimizedAttribute = false` plus `AXRaise`, delivers the event and puts it
back minimized immediately; the application is never activated, so the
operator's frontmost window does not change. Capture needs no such dance:
`screencapture -l` reads a live frame of the window while it is minimized, on
another Space or behind other windows.

The slave-agent channel is `agent_protocol`: `serial`, `clipboard`, `visual`
or `none`. Without the key a window instance defaults to `none`, and a
`serial` or `wifi` instance to `serial`. This is independent of
`slave_transport`, which selects the ESP32→slave leg (`cdc` or `hid`) for a
physical rig:

- `none` keeps the screen and HID surface only. `slave_bootstrap`,
  `remote_shell`, the terminals, the files and the transfers answer
  `cdc_unavailable`, and `next_steps` omits the bootstrap step because that
  advice could not be followed.
- `clipboard` carries the agent over the shared clipboard. `visual` is
  reserved and answers `cdc_unavailable` with the missing implementation
  named.

Clipboard channel: `slave_bootstrap`, `remote_shell`, `remote_terminal_*`,
`remote_fs_*`, `remote_upload` and `remote_download` work exactly as on a
physical rig; only the bytes underneath change. Each side writes one packet
of UTF-8 text to the shared clipboard, `TITELLA_M2S:<link_id>:<m2s_seq>:
<ack_s2m_seq>` from the master and `TITELLA_S2M:<link_id>:<s2m_seq>:
<ack_m2s_seq>` from the slave, followed by newline-delimited protocol v1
frames, or no frame at all for a pure transport acknowledgement. The
`m2s_seq`/`s2m_seq` counters make a re-read idempotent, and a packet whose
acknowledgement was lost is rewritten rather than waiting.

The clipboard is the operator's, so the link treats it that way:

- Nothing is written while no bootstrap, request or stream is in flight, and
  no `control.heartbeat` frames are ever sent over it: an idle instance does
  not touch the pasteboard at all.
- Before the first packet of a burst replaces the pasteboard, the operator's
  own text is saved and written back once the burst ends. Packets carry the
  `org.nspasteboard.TransientType`, `AutoGeneratedType` and `ConcealedType`
  markers, so clipboard history managers (Raycast, Alfred, Maccy, Paste,
  1Password) ignore them completely.
- A slave that writes on its own (terminal output, a job finishing, the
  bootstrap stage handshake) is polled while an operation declares that it
  waits for one and, afterwards, while an agent is attached; a stopped agent
  ends that polling (`slave_stop` does it explicitly).
- The clipboard is a single slot with no delivery guarantee: a copy made by
  the operator while a transfer is running can replace a packet. The protocol
  survives it through the sequence numbers, at the cost of one retry.

Tool sequence: `kvm_status` → `screen` → `lock_acquire` → HID tools →
`slave_bootstrap` (only with `agent_protocol = "clipboard"`) → the
`remote_*` tools. The bootstrap of a clipboard instance types the Windows
stub (the clipboard channel has no serial device, so `cdc_port` and
`slave_cdc_port` are rejected: pass neither) and stages the loader in 2 KiB
chunks. The stub announces `stage.ready` on `TITELLA_S2M`, answers every
`loader.push` with the running total and hands the console over to the
loader, which continues on the same channel.

A clipboard instance has no idle heartbeat, so a daemon restarted while the
agent still runs does not know it: the gate is closed until an agent frame
arrives. Run `slave_bootstrap` again, or `slave_stop` before restarting the
daemon, to return to a known state.

Observable results:

- `kvm_status` reports `firmware.reachable: true` with
  `firmware: "window-1.0"`, `protocol: 1`, `transport: "window"`, the resolved
  `port` (`window:<id>`) and the target window in the health body. The
  `capture.source` is `window:<window_app|window_title|window_id>`.
- `screen` captures the window with `screencapture -l <id> -o -x` and applies
  the same UVC rules: autocrop when `TITELLA_SCREEN_AUTOCROP` is on, the same
  `frame_sha256`/`diff` envelope, and `source: "window:<id>"`. The capture
  writes `raw_resolution` and `crop_box` next to the calibration, so
  `mouse_abs` still lands on the exact window pixel when borders were removed
  and when the window is captured at Retina scale.
- `mouse_abs`, `mouse_rel`, `click` and `wheel` post `CGEvent`s inside the
  window bounds. `click(state="down")`, a move with the button carried
  (`mouse_abs(..., button="left", state="keep")`) and `click(state="up")`
  emit real dragged events, which remote-desktop clients read as
  drag-and-drop. `mouse_rel` deltas saturate at the window edge.
- `type` keeps the whole-string layout validation
  (`untypeable_characters` before any keystroke) and then types the accepted
  text with `CGEventKeyboardSetUnicodeString`, so `ñ`, `€`, `á`, `{`, `[`
  and `\` land as those characters regardless of the host's dead-key state.
  A `\r\n` or a lone `\r` is typed as one Enter; `enter=true` presses Enter
  after the text.
- `key` maps its names (`ENTER`, `ESC`, `TAB`, `SPACE`, `BACKSPACE`,
  `DELETE`, arrows, `HOME`, `END`, `PAGEUP`, `PAGEDOWN`, `F1`..`F12`, the
  modifiers and single characters) to macOS keycodes plus `CGEventFlags`.
  `keyboard_layout` still tracks the layout used for validation.
- `release_all` releases every tracked mouse button at the last pointer
  position and owns no lease, exactly as on the serial transport.

Errors and recovery:

- `window_not_found` with `retryable: true` means no window matches the
  selectors: open the target, fix `window_app`/`window_title`/`window_id`, and
  confirm the process has Screen Recording permission — without it,
  `kCGWindowName` is unavailable and capture is refused.
- `screen_capture_failed` means `screencapture` failed or wrote no image;
  `invalid_screen_capture` means the bytes are not a PNG.
- `focus_failed` means the owning application could not be activated, which
  is usually a missing Accessibility (Input Monitoring) permission.
- `cdc_unavailable` on `remote_shell`, terminals, files and transfers means
  that the instance has no agent channel: `agent_protocol` is `none` (the
  window default) or `visual`, so only screen and HID tools are available.
  Set `agent_protocol = "clipboard"` to enable them.
- `request_timeout` on a clipboard instance means no answer arrived in time:
  confirm the slave console runs the stub or agent, that clipboard sharing
  between master and slave is enabled, and raise `ack_timeout_s` when the
  round trip is slower than the default.
- `unsupported_hid_step` only appears on the `kb_raw` fallback when a HID
  usage has no macOS keycode; the ordinary `type` path always carries the
  literal text and is not affected.

The transport ships no PyObjC dependency: a small Swift helper is compiled on
first use with `xcrun swiftc -O` and cached under the temporary directory,
keyed by a digest of its own source, so any helper change recompiles
automatically. Set `TITELLA_WINDOW_HELPER` to an absolute path to pin the
binary: macOS Accessibility is granted to that file once and survives
restarts, while the temporary copy changes name whenever the source does. The
same helper reads and writes `NSPasteboard.general` for the clipboard channel
(`clip-read`, `clip-write`).

## Conventions across all operations

- Every tool answers a JSON envelope; errors carry `code`, `message`,
  `retryable` and, when they exist, `request_id`/`session_id`. A transport
  failure never becomes an empty success.
- `owner` strings are labels; the lease is the only ownership mechanism.
- Documentation tools and resources are always available and never gate on
  the rig; operational tools always gate on the physical prerequisites
  named by `kvm_status`.
