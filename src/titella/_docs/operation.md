# Operation

How to drive a Titella rig through the MCP surface once the daemon is up:
capture, lease, bootstrap, shell, terminal, files, transfers and shutdown.
Reading this document performs no operation; every section names the tool
sequence, the observable result and the failure mode.

## Order of operations

The surface has one hard dependency chain:

```
kvm_status -> screen -> lock_acquire -> slave_bootstrap -> remote_* -> slave_stop
```

- `screen` needs only the local grabber; it works with the agent down.
- HID tools and `slave_bootstrap` need the lease held by the same `owner`.
- `remote_*` tools need the agent ready (the gate), not the lease.
- Documentation tools (`docs_index`, `docs_read`) need neither.

## Status: kvm_status

Call `kvm_status` before and between phases. Observable result: `firmware`
(reachability and announced version), `lease`, `agent` (bootstrap state,
platform, implementation), `capture` (resolution and age) and `next_steps`,
the pending prerequisites in resolution order. `next_steps` is the contract:
an empty list means the rig is operable end to end.

## Screen: screen

`screen` captures one frame from the local UVC grabber and fixes the
absolute-mouse resolution from it.

Tool sequence: `screen` alone. Observable result: a PNG image plus
`capture_resolution` in pixels; `mouse_abs` coordinates are interpreted in that
resolution.

Errors and recovery:

- `screen_capture_failed` with `ok: false, retryable: true` means the local
  grabber is absent, busy or misconfigured. Check `TITELLA_SCREEN_DEVICE`,
  the input format and that no other process holds the device; retry.
- A black but valid frame means the slave emits no signal (powered off,
  suspended, HDMI disconnected). That is target state, not a failure.
- `screen_required` from `mouse_abs` means there is no previous capture: call
  `screen` first.

Captures are occasional and on demand; there is no continuous stream.

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

## Bootstrap: slave_bootstrap, slave_status, slave_stop

`slave_bootstrap(owner, layout=None, platform=None, cdc_port=None,
layout_confirmed=false)` deploys the ephemeral agent of the selected platform
over HID + CDC. The platform is mandatory (argument, then configuration, then
`slave_platform_required`). The layout is mandatory and independent of
configuration: `layout` names the active keyboard layout (`es_ES` or `en_US`)
verified on the target's focused input window during this attempt, and
`layout_confirmed=true` attests that verification. The configured
`keyboard_layout` is never a bootstrap fallback.

A missing layout answers `slave_layout_required`, an unsupported value
`invalid_argument` and an unconfirmed layout `slave_layout_unconfirmed`; all
three reject before any keystroke. `cdc_port` overrides the CDC choice on the
slave. The verification procedure (focused window, per-window input sources,
launcher window transitions, harmless punctuation probe) is in
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

## Conventions across all operations

- Every tool answers a JSON envelope; errors carry `code`, `message`,
  `retryable` and, when they exist, `request_id`/`session_id`. A transport
  failure never becomes an empty success.
- `owner` strings are labels; the lease is the only ownership mechanism.
- Documentation tools and resources are always available and never gate on
  the rig; operational tools always gate on the physical prerequisites
  named by `kvm_status`.
