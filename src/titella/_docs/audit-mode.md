# Audit mode

Evidence recording for Titella operations: activation by name and instance,
integrity rules, error codes and export. Originals stay private and intact;
an external skill renders exports to HTML/DOCX/PDF — this server never
generates or publishes those formats.

## MCP signatures

```python
audit_activate(name: str, titella: str | None = None) -> dict[str, Any]
audit_deactivate(titella: str | None = None) -> dict[str, Any]
audit_status(titella: str | None = None) -> dict[str, Any]
audit_list() -> dict[str, Any]
audit_note(audit_id: str, text: str, evidence_ids: list[str] | None = None) -> dict[str, Any]
audit_search(audit_id: str, query: str = "", *, titella: str | None = None,
             kind: str | None = None, since: str | None = None, until: str | None = None,
             cursor: str | None = None, limit: int = 50) -> dict[str, Any]
audit_read(audit_id: str, evidence_id: str, offset: int = 0,
           length: int = 65536) -> list[Any]
audit_export(audit_id: str, evidence_ids: list[str]) -> dict[str, Any]
```

`audit_status` selects the explicit/default instance; the archival tools do not
resolve a live instance. No delete, automatic retention, report generation or
automatic audit activation is exposed.

## Activation and archive

- `audit_activate(name, titella=None)` — `name` is the `audit_id`
  (`^[a-z0-9][a-z0-9_-]{0,63}$`, never a path or URL). The archive lives in
  `<audit_root>/<audit_id>/`. Returns
  `{ok, status:"active", audit_id, titella, directory, run_id}`.
- `audit_root` comes from config (`audit_root` key or `TITELLA_AUDIT_ROOT`),
  default `~/.local/state/titella/audits`, always absolute. It is
  deliberately independent of `state_dir` (which defaults to TEMP).
- Directories are 0700, files 0600. Symlinks and `..` are rejected; a
  foreign directory or invalid manifest answers `audit_invalid_directory`
  and is never overwritten.
- Association is per titella instance, shared by every client of the daemon.
  Two instances may record two audits; changing audit requires
  `audit_deactivate` first (`audit_already_active`).
- Idempotent re-activation of the same id returns the same `run_id` and adds
  no event, even with jobs in flight. Activation with pending jobs,
  terminals or transfers answers `audit_busy` — finish or cancel them with
  the ordinary tools first.
- Agent or camera absence never blocks activate, status, search or export.

## Events and evidence

`events.jsonl` is append-only UTF-8, one JSON line per event:
`schema_version:1, audit_id, event_id, seq (monotonic), ts_utc (RFC3339 Z,
master clock), run_id, titella, action_id, kind, data`. Kinds:
`audit.activated, audit.deactivated, daemon.interrupted, tool.started,
tool.finished, tool.failed, operation.started, operation.bound,
operation.finished, operation.interrupted, stream.chunk, stream.closed,
evidence.created, capture.recorded, ocr.recorded, note, export.created,
recording.gap`.

Artifacts live in `artifacts/` as `<evidence_id>.json` (arguments/results
intact, never trimmed even when the MCP answer is), `.png` (captures, diff
crops, OCR images) and `.bin` (exact stream bytes). `evidence.created`
carries `{evidence_id, relative_path, mime_type, role, size, sha256,
sealed, action_id, related_evidence_ids}`. Finite artifacts are sealed on
creation. Streams are created empty (`sealed:false`, hash of the empty
string) and stay readable while running; `stream.chunk` updates the
confirmed prefix; `stream.closed` fixes final size/hash and `sealed:true`
with `complete` and `reason` separate — sealed is not success. Binaries are
only ever appended. Exit codes, timeouts, cancellations and visible
truncation are stored verbatim, never reinterpreted. Secrets that appear in
commands or output are preserved literally (the operator chose intact
originals); OCR credentials never enter the archive.

## Integrity and failure

- No digital signature or tamper proof against someone who can rewrite the
  whole directory: the archive is a trusted-local-daemon record.
- `active.json` (`{schema_version:1, bindings:{<titella>:{audit_id,run_id}}}`)
  is replaced atomically. After a daemon restart the bindings become
  `recovery_required`; re-activating the same name appends
  `daemon.interrupted`, marks the previous run's open operations unknown and
  opens a new `run_id`. `audit_deactivate` can also close a
  `recovery_required` binding explicitly.
- A corrupt last JSONL line is kept byte-intact: reads and exports serve the
  valid prefix with a warning (`audit_corrupt`); nothing is truncated or
  repaired automatically.
- One writer per manager; a bounded 64 MiB queue shared by open archives;
  a second writer process is refused with `audit_root_busy`. Activate,
  deactivate and export fsync before answering. A disk/queue failure moves
  the binding to `faulted`, records an explicit gap, preserves the real
  result already executed and blocks further audited actions with
  `audit_recording_failed` — never a fake success, silent drop or
  re-execution. Stop/cancel/close/release tools stay available so you can
  wind down safely.

## Capture and OCR provenance

Every capture the instance produces is archived with its own evidence id,
hash, source, resolution and diff — each observation has its own id even
when the PNG bytes are identical. `screen_diff` archives its crop as derived
evidence referencing the full frame. Under audit, `screen_ocr` and
`screen_diff` require a fresh capture of the same audit and run with a hash
matching the current cache bytes; anything older, foreign or modified
answers `audit_capture_required` before any crop or model call — ask for
`screen` again. OCR results carry `source_evidence_id` and `frame_sha256`.

## Errors

`audit_not_found, evidence_not_found, audit_invalid_directory,
audit_already_active, audit_busy, audit_root_busy, audit_recording_failed,
audit_recovery_required, audit_corrupt, audit_integrity_failed,
audit_capture_required, audit_export_failed`; validation failures use
`invalid_argument`. `audit_read` verifies size/hash of sealed artifacts and
answers `audit_integrity_failed` on any difference.

## Query and export (rig off)

`audit_list`, `audit_note`, `audit_search`, `audit_read` and `audit_export`
use the local archive without resolving a live rig; the ID-specific tools work
after the instance was removed. Search is literal Unicode casefold
(≤512 chars) over events, arguments, results, notes, OCR and
stdout/stderr/terminal streams with cross-block matches, byte offsets and
an opaque cursor; empty query lists chronology. `audit_read` serves
text/binary pages (`data_b64`, UTF-8 view, `next_offset`, `eof`; PNG at
offset 0 only). `audit_export(audit_id, evidence_ids)` copies an explicit
selection of 1..500 ids into `<audit>/exports/<uuid>/` with `assets/`,
filtered `events.jsonl`, `manifest.json` (hashes, sizes, completeness,
gaps) and an `evidence.md` index — an index, never a report. The package
contains raw originals (`contains_raw_evidence:true`); references to
unselected evidence are enumerated, not silently included.

## Example (`cliente-demo` on `lab`; synthetic identifiers)

```
audit_activate(name="cliente-demo", titella="lab")   # -> run_id r1
remote_shell(cmd="whoami", titella="lab")            # tool.started/finished
screen(titella="lab")                                # capture.recorded + png
audit_search(audit_id="cliente-demo", query="whoami")
audit_read(audit_id="cliente-demo", evidence_id="<stdout id>")
audit_note(audit_id="cliente-demo", text="step verified", evidence_ids=[...])
audit_deactivate(titella="lab")
audit_export(audit_id="cliente-demo", evidence_ids=[...])
```

The audit name or owner is not an authorization mechanism.
