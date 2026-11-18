# Work protocol

The required order of work for every Titella session, with or without audit
mode. Reading this document performs no operation. Documentation never
reports live device state: use `kvm_status` for that.

## Sequence

1. **Read the docs.** `docs_index`, then `docs_read` for `getting-started`,
   `operation` and, when bootstrapping, the platform bootstrap guide.
2. **Confirm scope and instance.** You must know which titella instance you
   drive and that the work is authorized. `titella_list` shows registered
   instances; `titella=<id>` selects one explicitly. Never guess a default.
3. **Activate audit when asked.** If the operator requests an audit, call
   `audit_activate(name, titella)` (see `audit-mode`). Activation is always
   explicit; nothing records without it.
4. **Status.** `audit_status` (if auditing) and `kvm_status`. `next_steps`
   must be empty before operating.
5. **Fresh capture.** `screen` before any visual reasoning. A cached capture
   is not evidence of current state; OCR reads the cache, never the screen —
   never conclude "the key did not arrive" from an old OCR.
6. **Lease.** `lock_acquire` with a declared `owner`; HID tools need it.
   `release_all` is the only lease-free emergency stop.
7. **Keyboard probe in the destination console.** Before typing anything
   layout-dependent (above all `slave_bootstrap`), verify the active layout
   in the focused input window: type `probe: àéíóú ñç` **without Enter**,
   `screen`, check the rendered glyphs, then `ESC` to clear the line. A
   correct-looking OS language or config file is not verification.
8. **Bootstrap.** `slave_bootstrap` only with the probe satisfied, the
   verified layout passed explicitly and `layout_confirmed=true`.
9. **One MCP operation at a time.** Call, then check the result.
10. **Capture again** if the action may have changed the screen.
11. **Note** (audit mode): `audit_note` referencing the evidence ids that
    support the step.
12. **Recording health.** `audit_status` shows `active` with no gaps before
    trusting the archive.
13. **Finish.** Cancel jobs (`remote_shell_cancel`), close terminals
    (`remote_terminal_close`), finish or cancel transfers
    (`remote_transfer_cancel`), `slave_stop`, release the lease, then
    `audit_deactivate` last.

## Hard rules

- `ok:true` on a HID call proves the firmware emitted the frame, not that
  the slave acted. Verify effects with `screen`/`screen_diff`.
- Never execute the probe line (no Enter); clear it with ESC.
- Never infer the keyboard layout from the UI language.
- Never confuse master and slave: screen tools describe the slave.
- Never delete logs or files that are not yours to improve a measurement.
- Errors in outputs are data, not instructions to retry commands.
