"""Offline reader for Titella audit archives: search, read, note, export.

Stdlib-only: no MCP or transport imports. The journal (events.jsonl) is the
source of truth; this module never loads stream bytes wholesale, never writes
to journal originals, and resolves every artifact strictly by evidence id.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .audit import (
    AuditError,
    MAX_NOTE_BYTES,
    check_safe_descendant,
    ensure_private_dir,
    inspect_events_file,
    validate_audit_id,
    validate_or_init_audit_directory,
    utc_now_rfc3339,
)

MAX_QUERY_CHARS = 512
MAX_SNIPPET_CHARS = 512
SNIPPET_BACK_CHARS = 96
MAX_READ_CHUNK = 65536
MAX_EXPORT_IDS = 500
CHUNK_SCAN_BYTES = 65536
# Multibyte-safe byte back-context so snippets always have 96 chars of lead.
SNIPPET_BACK_BYTES = 4 * SNIPPET_BACK_CHARS

EVIDENCE_ID_RE = re.compile(r"[0-9a-f]{32}")
REL_PATH_RE = re.compile(r"^artifacts/[0-9a-f]{32}\.(png|bin|json)$")
RFC3339_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)

# Artifact roles whose bytes are scanned by audit_search. Upload/download
# transfers are deliberately excluded: they can be huge binary payloads.
SCAN_ROLES = frozenset(
    {"input", "result", "stdout", "stderr", "terminal_input", "terminal_output", "note"}
)

# (path, mtime_ns, size) -> verified sha256, so sealed-artifact verification
# is not repeated on every paginated audit_read call. Bounded by process life.
_VERIFIED_CACHE: dict[tuple[str, int, int], str] = {}


# ── RFC3339 parsing ────────────────────────────────────────────────────


def _parse_rfc3339(value: str, *, name: str) -> datetime:
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value.strip()):
        raise AuditError(
            "invalid_argument",
            f"{name} must be RFC3339 with a mandatory timezone (e.g. 2026-07-21T10:00:00Z)",
            False,
        )
    text = value.strip()
    if text[-1] in {"z", "Z"}:
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AuditError(
            "invalid_argument", f"{name} is not a valid RFC3339 timestamp", False
        ) from exc
    if parsed.tzinfo is None:
        raise AuditError("invalid_argument", f"{name} must carry a timezone", False)
    return parsed.astimezone(timezone.utc)


def _event_datetime(event: dict[str, Any]) -> datetime | None:
    ts = event.get("ts_utc")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── archive view and evidence index ────────────────────────────────────


@dataclass
class _EvidenceOrigin:
    event_id: str | None
    seq: int | None
    ts_utc: str | None
    titella: str | None


@dataclass
class _EvidenceMeta:
    evidence_id: str
    relative_path: str
    mime_type: str
    role: str
    size: int | None
    sha256: str | None
    sealed: bool
    complete: bool
    action_id: str | None
    related_evidence_ids: list[str] = field(default_factory=list)
    confirmed_prefix: int | None = None
    prefix_sha256: str | None = None
    chunks: list[tuple[int, int, str]] | None = None
    close_reason: str | None = None
    origin: _EvidenceOrigin | None = None


@dataclass
class _ArchiveView:
    audit_id: str
    directory: Path
    root: Path
    events: list[dict[str, Any]]
    through_seq: int
    journal_last_seq: int
    valid_bytes: int
    corrupt_tail: bool
    corrupt_warning: str | None
    evidence_index: dict[str, _EvidenceMeta]
    evidence_order: list[str]


def _hex32(value: Any) -> str | None:
    if isinstance(value, str) and EVIDENCE_ID_RE.fullmatch(value):
        return value
    return None


def _origin_of(event: dict[str, Any]) -> _EvidenceOrigin:
    return _EvidenceOrigin(
        event_id=event.get("event_id"),
        seq=event.get("seq"),
        ts_utc=event.get("ts_utc"),
        titella=event.get("titella"),
    )


def _build_evidence_index(events: list[dict[str, Any]]) -> tuple[dict[str, _EvidenceMeta], list[str]]:
    """Builds the evidence registry from the journal prefix only.

    Recognizes evidence.created, note, ocr.recorded (result artifacts have
    no evidence.created event of their own), stream.chunk and stream.closed.
    Stream bytes are never read here: open-stream lengths come from the
    confirmed_prefix recorded in stream.chunk events.
    """
    index: dict[str, _EvidenceMeta] = {}

    def _fallback(eid: str) -> _EvidenceMeta:
        entry = _EvidenceMeta(
            evidence_id=eid,
            relative_path=f"artifacts/{eid}.bin",
            mime_type="application/octet-stream",
            role="stdout",
            size=None,
            sha256=None,
            sealed=False,
            complete=False,
            action_id=None,
        )
        index[eid] = entry
        return entry

    for event in events:
        kind = event.get("kind")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        eid = _hex32(data.get("evidence_id"))
        if kind == "evidence.created" and eid is not None:
            sealed = bool(data.get("sealed"))
            explicit_complete = data.get("complete")
            index[eid] = _EvidenceMeta(
                evidence_id=eid,
                relative_path=str(data.get("relative_path", f"artifacts/{eid}.bin")),
                mime_type=str(data.get("mime_type", "application/octet-stream")),
                role=str(data.get("role", "stdout")),
                size=data.get("size") if isinstance(data.get("size"), int) else None,
                sha256=data.get("sha256") if isinstance(data.get("sha256"), str) else None,
                sealed=sealed,
                # Finite artifacts are complete by construction; streams stay
                # incomplete until their stream.closed event says otherwise.
                complete=bool(explicit_complete) if isinstance(explicit_complete, bool) else sealed,
                action_id=data.get("action_id") if isinstance(data.get("action_id"), str) else None,
                related_evidence_ids=[
                    ref for ref in (data.get("related_evidence_ids") or []) if _hex32(ref)
                ],
                confirmed_prefix=data.get("confirmed_prefix")
                if isinstance(data.get("confirmed_prefix"), int)
                else None,
                origin=_origin_of(event),
            )
        elif kind == "note" and eid is not None:
            # The note event mirrors the artifact and carries its references;
            # keep size/hash/relative path recorded by evidence.created.
            entry = index.get(eid)
            if entry is None:
                entry = _EvidenceMeta(
                    evidence_id=eid,
                    relative_path=f"artifacts/{eid}.json",
                    mime_type="application/json",
                    role="note",
                    size=None,
                    sha256=None,
                    sealed=True,
                    complete=True,
                    action_id=event.get("action_id")
                    if isinstance(event.get("action_id"), str)
                    else None,
                )
                index[eid] = entry
            entry.role = "note"
            entry.mime_type = "application/json"
            entry.sealed = True
            entry.complete = True
            reference_ids = data.get("related_evidence_ids")
            if not isinstance(reference_ids, list):
                reference_ids = data.get("evidence_ids")
            if isinstance(reference_ids, list):
                entry.related_evidence_ids = [ref for ref in reference_ids if _hex32(ref)]
            entry.origin = _origin_of(event)
        elif kind == "ocr.recorded":
            rid = _hex32(data.get("result_evidence_id"))
            if rid is not None and rid not in index:
                # The OCR result artifact is written by the store without a
                # matching evidence.created event; size/sha256 are not in the
                # journal, so they are resolved from the file when needed.
                related = [data["image_evidence_id"]] if _hex32(data.get("image_evidence_id")) else []
                index[rid] = _EvidenceMeta(
                    evidence_id=rid,
                    relative_path=f"artifacts/{rid}.json",
                    mime_type="application/json",
                    role="result",
                    size=None,
                    sha256=None,
                    sealed=True,
                    complete=True,
                    action_id=data.get("action_id") if isinstance(data.get("action_id"), str) else None,
                    related_evidence_ids=related,
                    origin=_origin_of(event),
                )
        elif kind == "stream.chunk" and eid is not None:
            entry = index.get(eid)
            if entry is None:
                entry = _fallback(eid)
                entry.origin = _origin_of(event)
            prefix = data.get("confirmed_prefix")
            if isinstance(prefix, int):
                entry.confirmed_prefix = (
                    prefix
                    if entry.confirmed_prefix is None
                    else max(entry.confirmed_prefix, prefix)
                )
            prefix_sha = data.get("prefix_sha256")
            if isinstance(prefix_sha, str):
                entry.prefix_sha256 = prefix_sha
            offset = data.get("offset")
            length = data.get("length")
            chunk_sha = data.get("chunk_sha256")
            if (
                isinstance(offset, int)
                and isinstance(length, int)
                and isinstance(chunk_sha, str)
            ):
                if entry.chunks is None:
                    entry.chunks = []
                entry.chunks.append((offset, length, chunk_sha))
            if entry.action_id is None and isinstance(event.get("action_id"), str):
                entry.action_id = event["action_id"]
        elif kind == "stream.closed" and eid is not None:
            entry = index.get(eid)
            if entry is None:
                entry = _fallback(eid)
                entry.origin = _origin_of(event)
            entry.sealed = True
            if isinstance(data.get("size"), int):
                entry.size = data["size"]
            if isinstance(data.get("sha256"), str):
                entry.sha256 = data["sha256"]
            entry.complete = bool(data.get("complete"))
            entry.close_reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    return index, list(index.keys())


def _slice_view(view: _ArchiveView, through_seq: int) -> _ArchiveView:
    """Narrows an already-loaded view to a previous snapshot without a second
    journal read: the valid prefix is append-only, so filtering by seq is
    deterministic and the evidence index rebuilds identically."""
    if through_seq == view.through_seq:
        return view
    prefix = [e for e in view.events if isinstance(e.get("seq"), int) and e["seq"] <= through_seq]
    index, order = _build_evidence_index(prefix)
    return _ArchiveView(
        audit_id=view.audit_id,
        directory=view.directory,
        root=view.root,
        events=prefix,
        through_seq=through_seq,
        journal_last_seq=view.journal_last_seq,
        valid_bytes=view.valid_bytes,
        corrupt_tail=view.corrupt_tail,
        corrupt_warning=view.corrupt_warning,
        evidence_index=index,
        evidence_order=order,
    )


def load_archive(root: Path, audit_id: str, *, through_seq: int | None = None) -> _ArchiveView:
    """Loads journal metadata (never stream bytes) as a stable snapshot.

    through_seq pins the snapshot: only events with seq <= through_seq are
    visible, which makes cursor pagination immune to concurrent appends.
    """
    root = Path(root)
    validate_audit_id(audit_id)
    state = validate_or_init_audit_directory(root, audit_id, create=False)
    journal_last_seq, valid_bytes, corrupt, warning, events = inspect_events_file(
        state.directory / "events.jsonl"
    )
    if through_seq is None:
        through_seq = journal_last_seq
    if not isinstance(through_seq, int) or isinstance(through_seq, bool):
        raise AuditError("invalid_argument", "through_seq must be an integer", False)
    through_seq = max(0, min(through_seq, journal_last_seq))
    prefix = [e for e in events if isinstance(e.get("seq"), int) and e["seq"] <= through_seq]
    index, order = _build_evidence_index(prefix)
    return _ArchiveView(
        audit_id=audit_id,
        directory=state.directory,
        root=root,
        events=prefix,
        through_seq=through_seq,
        journal_last_seq=journal_last_seq,
        valid_bytes=valid_bytes,
        corrupt_tail=corrupt,
        corrupt_warning=warning,
        evidence_index=index,
        evidence_order=order,
    )


# ── cursor codec ───────────────────────────────────────────────────────


def _encode_cursor(doc: dict[str, Any]) -> str:
    raw = json.dumps(doc, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str,
    *,
    audit_id: str,
    filters_hash: str,
    view: _ArchiveView,
) -> dict[str, Any]:
    """Validates every cursor field; rejects foreign audits, filters and
    snapshots that no longer match the journal on disk."""
    try:
        raw = base64.urlsafe_b64decode(
            cursor.encode("ascii") + b"=" * ((-len(cursor)) % 4)
        )
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("v") != 1:
            raise ValueError("unsupported cursor version")
    except Exception as exc:
        raise AuditError(
            "invalid_argument", f"cursor is not a valid opaque pagination token: {exc}", False
        ) from exc
    if doc.get("a") != audit_id:
        raise AuditError("invalid_argument", "cursor belongs to a different audit", False)
    if doc.get("f") != filters_hash:
        raise AuditError("invalid_argument", "cursor was created with different filters", False)
    seq = doc.get("s")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise AuditError("invalid_argument", "cursor snapshot seq is invalid", False)
    if seq > view.journal_last_seq:
        raise AuditError(
            "invalid_argument",
            "cursor snapshot no longer matches the journal (it shrank or was rewritten)",
            False,
        )
    phase = doc.get("p")
    if phase not in {"e", "a"}:
        raise AuditError("invalid_argument", "cursor phase is invalid", False)
    if phase == "e":
        ep = doc.get("e", 0)
        if not isinstance(ep, int) or isinstance(ep, bool) or ep < 0:
            raise AuditError("invalid_argument", "cursor event position is invalid", False)
        if ep > len(view.events):
            raise AuditError("invalid_argument", "cursor event position is out of range", False)
        return {"phase": "e", "ep": ep, "through_seq": seq}
    eid = doc.get("i")
    fb = doc.get("b", 0)
    if eid is not None and _hex32(eid) is None:
        raise AuditError("invalid_argument", "cursor artifact id is invalid", False)
    if not isinstance(fb, int) or isinstance(fb, bool) or fb < 0:
        raise AuditError("invalid_argument", "cursor artifact position is invalid", False)
    if eid is None and fb != 0:
        raise AuditError("invalid_argument", "cursor artifact position is invalid", False)
    if eid is not None and eid not in view.evidence_index:
        raise AuditError("invalid_argument", "cursor artifact is not in the snapshot", False)
    return {"phase": "a", "eid": eid, "fb": fb, "through_seq": seq}


# ── incremental UTF-8 / casefold scanner ────────────────────────────────


def _is_utf8_prefix(seq: bytes) -> bool:
    """True when seq is a strict prefix (possibly complete) of a UTF-8 char."""
    if not seq:
        return False
    b0 = seq[0]
    if b0 & 0xE0 == 0xC0:
        ln = 2
    elif b0 & 0xF0 == 0xE0:
        ln = 3
    elif b0 & 0xF8 == 0xF0:
        ln = 4
    else:
        return False
    if len(seq) >= ln:
        try:
            seq[:ln].decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    return all((b & 0xC0) == 0x80 for b in seq[1:])


def _decode_window(block: bytes, *, at_eof: bool) -> tuple[str, list[int] | None, int]:
    """Decodes one window with per-byte replacement, never raising.

    Returns (text, per-char relative byte starts or None for ASCII,
    decoded_end) where decoded_end is the byte length forming complete
    characters; a trailing incomplete sequence is deferred to the next
    window unless we are at the end of the scanned range.
    """
    if block.isascii():
        return block.decode("ascii"), None, len(block)
    chars: list[str] = []
    starts: list[int] = []
    i = 0
    n = len(block)
    while i < n:
        b = block[i]
        if b < 0x80:
            chars.append(chr(b))
            starts.append(i)
            i += 1
            continue
        if b & 0xE0 == 0xC0:
            ln = 2
        elif b & 0xF0 == 0xE0:
            ln = 3
        elif b & 0xF8 == 0xF0:
            ln = 4
        else:
            ln = 0
        if ln == 0:
            chars.append("\ufffd")
            starts.append(i)
            i += 1
            continue
        if i + ln > n:
            if not at_eof and _is_utf8_prefix(block[i:]):
                break  # defer: the next window decodes it with its successors
            chars.append("\ufffd")
            starts.append(i)
            i += 1
            continue
        seq = block[i : i + ln]
        try:
            ch = seq.decode("utf-8")
        except UnicodeDecodeError:
            ch = None
        if ch is None:
            chars.append("\ufffd")
            starts.append(i)
            i += 1
            continue
        chars.append(ch)
        starts.append(i)
        i += ln
    return "".join(chars), starts, i


def _folded_with_map(text: str, starts: list[int]) -> tuple[str, list[tuple[int, int]]]:
    """Casefolds text keeping, for every folded char, the (char_index,
    relative_byte_start) of the original character it came from."""
    folded_chars: list[str] = []
    index_map: list[tuple[int, int]] = []
    for ci, ch in enumerate(text):
        folded = ch.casefold()
        for fc in folded:
            folded_chars.append(fc)
            index_map.append((ci, starts[ci]))
    return "".join(folded_chars), index_map


def _align_back(stream: Any, target: int) -> int:
    """Largest character-start byte position <= target."""
    if target <= 0:
        return 0
    lo = max(0, target - 3)
    stream.seek(lo)
    buf = stream.read(target - lo + 1)
    best = 0
    for i, b in enumerate(buf):
        if (b & 0xC0) != 0x80:
            best = lo + i
    return best


def _char_byte_len(starts: list[int], ci: int, dec_end: int) -> int:
    if ci + 1 < len(starts):
        return starts[ci + 1] - starts[ci]
    return dec_end - starts[ci]


def _clip_snippet(text: str, char_idx: int) -> str:
    begin = max(0, char_idx - SNIPPET_BACK_CHARS)
    return text[begin : begin + MAX_SNIPPET_CHARS]


def _iter_artifact_hits(
    path: Path,
    cap: int,
    needle_folded: str,
    *,
    report_from: int = 0,
) -> Iterator[tuple[int, str]]:
    """Yields (byte_offset, snippet) for literal casefold matches whose whole
    byte span lies inside [0, cap), scanning incrementally in 64 KiB windows.

    Windows overlap by (needle span + snippet back-context) so matches that
    straddle block boundaries or multibyte characters are found exactly once
    with correct byte offsets: each window only reports matches that start
    at or before its cut point, and the next window resumes after that cut.
    """
    if cap <= 0 or not needle_folded:
        return
    span_guard = 4 * len(needle_folded) + 4
    with path.open("rb") as stream:
        try:
            file_len = os.fstat(stream.fileno()).st_size
        except OSError:
            return
        scan_limit = min(cap, file_len)
        if report_from >= scan_limit:
            return
        pos = _align_back(stream, report_from - SNIPPET_BACK_BYTES) if report_from else 0
        min_report = report_from
        while pos < scan_limit:
            stream.seek(pos)
            want = min(CHUNK_SCAN_BYTES, scan_limit - pos)
            block = stream.read(want)
            if not block:
                break
            window_raw_end = pos + len(block)
            is_last = window_raw_end >= scan_limit or len(block) < want
            text, starts, dec_end = _decode_window(block, at_eof=is_last)
            if not text:
                # Degenerate window (only a deferred incomplete sequence).
                if is_last:
                    break
                pos = pos + max(1, dec_end)
                continue
            if block.isascii():
                folded = text.casefold()
                if len(folded) == len(text):
                    fmap = None  # folded index == char index == byte offset
                else:  # defensive: ASCII never expands, but stay correct
                    starts = list(range(len(text)))
                    folded, fmap = _folded_with_map(text, starts)
            else:
                folded, fmap = _folded_with_map(text, starts)
            cut = None if is_last else pos + max(0, dec_end - span_guard)
            needle_len = len(needle_folded)
            k = 0
            while True:
                k = folded.find(needle_folded, k)
                if k < 0:
                    break
                if fmap is None:
                    char_idx = k
                    byte_start = pos + k
                    byte_end = byte_start + needle_len
                else:
                    char_idx, rel_bs = fmap[k]
                    byte_start = pos + rel_bs
                    last_ci = fmap[k + needle_len - 1][0]
                    byte_end = pos + starts[last_ci] + _char_byte_len(starts, last_ci, dec_end)
                if byte_start < min_report:
                    k += 1
                    continue
                if cut is not None and byte_start > cut:
                    break  # match is complete but reserved for the next window
                if byte_end > cap:
                    k += 1
                    continue  # extends beyond the confirmed prefix
                yield byte_start, _clip_snippet(text, char_idx)
                k += 1
            if is_last:
                return
            nxt = cut - SNIPPET_BACK_BYTES if cut is not None else pos
            nxt = _align_back(stream, nxt) if nxt > 0 else 0
            if nxt <= pos:
                # Cannot happen for full 64 KiB windows; kept as a hard
                # progress guarantee for degenerate caps.
                nxt = pos + max(1, dec_end - span_guard)
                if nxt <= pos:
                    nxt = pos + 1
            pos = nxt
            if cut is not None:
                min_report = max(min_report, cut + 1)


_MISSING = object()


class _Peekable:
    """One-item lookahead wrapper used to detect page-exact artifact ends."""

    def __init__(self, iterator: Iterator[tuple[int, str]]) -> None:
        self._iter = iterator
        self._buf: list[tuple[int, str]] = []

    def peek(self) -> Any:
        if not self._buf:
            try:
                self._buf.append(next(self._iter))
            except StopIteration:
                return _MISSING
        return self._buf[0]

    def advance(self) -> None:
        if self._buf:
            self._buf.pop()


# ── search ─────────────────────────────────────────────────────────────


def _filters_hash(
    query: str,
    titella: str | None,
    kind: str | None,
    since_dt: datetime | None,
    until_dt: datetime | None,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "q": query,
                "t": titella,
                "k": kind,
                "s": since_dt.isoformat() if since_dt else None,
                "u": until_dt.isoformat() if until_dt else None,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _event_hit(event: dict[str, Any], needle_folded: str) -> tuple[bool, str]:
    if not needle_folded:
        return True, ""
    haystack = json.dumps(event, ensure_ascii=False, default=repr)
    idx = haystack.casefold().find(needle_folded)
    if idx < 0:
        return False, ""
    return True, _clip_snippet(haystack, idx)


def _origin_in_range(meta: _EvidenceMeta, since_dt: datetime | None, until_dt: datetime | None) -> bool:
    origin = meta.origin
    ts = origin.ts_utc if origin is not None else None
    if since_dt is None and until_dt is None:
        return True
    if not isinstance(ts, str):
        return False
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if since_dt is not None and moment < since_dt:
        return False
    if until_dt is not None and moment > until_dt:
        return False
    return True


def _scan_cap(meta: _EvidenceMeta, path: Path) -> int:
    """Readable byte cap for scanning: sealed artifacts use their recorded
    size (falling back to the on-disk size when the journal recorded none);
    open streams use the contiguously journal-confirmed prefix."""
    if meta.sealed:
        if isinstance(meta.size, int) and meta.size >= 0:
            return meta.size
        try:
            return path.stat().st_size
        except OSError:
            return 0
    span, _sha = _confirmed_chunk_span(meta)
    return span


def _artifact_eligible(
    meta: _EvidenceMeta,
    titella: str | None,
    kind: str | None,
    since_dt: datetime | None,
    until_dt: datetime | None,
) -> bool:
    if meta.role not in SCAN_ROLES:
        return False
    if meta.relative_path.rsplit(".", 1)[-1] not in {"bin", "json"}:
        return False
    if titella is not None and (meta.origin is None or meta.origin.titella != titella):
        return False
    if not _origin_in_range(meta, since_dt, until_dt):
        return False
    if kind is not None and meta.role != kind:
        return False
    return True


def audit_search(
    root: Path,
    audit_id: str,
    query: str = "",
    *,
    titella: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Literal Unicode casefold search over the journal snapshot and the
    text artifacts it references. Empty query lists chronology. Journal hits
    carry the event kind; artifact hits carry the artifact role as ``kind``,
    so the kind filter selects event kinds and roles respectively. Pagination
    is snapshot-stable: through_seq and artifact caps are pinned by the first
    call and carried inside the opaque cursor."""
    root = Path(root)
    validate_audit_id(audit_id)
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise AuditError(
            "invalid_argument", f"query must be at most {MAX_QUERY_CHARS} characters", False
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise AuditError("invalid_argument", "limit must be between 1 and 200", False)
    if titella is not None and not isinstance(titella, str):
        raise AuditError("invalid_argument", "titella filter must be a string", False)
    if kind is not None and not isinstance(kind, str):
        raise AuditError("invalid_argument", "kind filter must be a string", False)
    since_dt = _parse_rfc3339(since, name="since") if since else None
    until_dt = _parse_rfc3339(until, name="until") if until else None
    if since_dt and until_dt and since_dt > until_dt:
        raise AuditError("invalid_argument", "since is after until", False)

    fhash = _filters_hash(query, titella, kind, since_dt, until_dt)
    token: dict[str, Any] | None = None
    view: _ArchiveView | None = None
    if cursor:
        # Validating the cursor requires the snapshot size, so load first.
        base = load_archive(root, audit_id)
        token = _decode_cursor(cursor, audit_id=audit_id, filters_hash=fhash, view=base)
        view = _slice_view(base, token["through_seq"])
        if token["phase"] == "e" and token["ep"] > len(view.events):
            raise AuditError(
                "invalid_argument", "cursor event position is out of range", False
            )
        if (
            token["phase"] == "a"
            and token["eid"] is not None
            and token["eid"] not in view.evidence_index
        ):
            raise AuditError(
                "invalid_argument", "cursor artifact is not in the snapshot", False
            )
    else:
        view = load_archive(root, audit_id)
    assert view is not None
    through_seq = view.through_seq

    needle_folded = query.casefold()
    hits: list[dict[str, Any]] = []

    def _hit_from_event(event: dict[str, Any], snippet: str) -> dict[str, Any]:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        return {
            "event_id": event.get("event_id"),
            "seq": event.get("seq"),
            "ts_utc": event.get("ts_utc"),
            "titella": event.get("titella"),
            "action_id": event.get("action_id"),
            "kind": event.get("kind"),
            "evidence_id": _hex32(data.get("evidence_id")),
            "role": data.get("role") if isinstance(data.get("role"), str) else None,
            "snippet": snippet,
            "byte_offset": None,
        }

    def _hit_from_artifact(meta: _EvidenceMeta, offset: int, snippet: str) -> dict[str, Any]:
        origin = meta.origin
        return {
            "event_id": origin.event_id if origin else None,
            "seq": origin.seq if origin else None,
            "ts_utc": origin.ts_utc if origin else None,
            "titella": origin.titella if origin else None,
            "action_id": meta.action_id,
            "kind": meta.role,
            "evidence_id": meta.evidence_id,
            "role": meta.role,
            "snippet": snippet,
            "byte_offset": offset,
        }

    start_ep = 0
    start_artifact: str | None = None
    start_fb = 0
    if token is not None:
        if token["phase"] == "e":
            start_ep = token["ep"]
        else:
            start_ep = len(view.events)
            start_artifact = token["eid"]
            start_fb = token["fb"]

    # Phase 1: journal events (arguments/results metadata, notes, OCR refs).
    ep = start_ep
    while ep < len(view.events) and len(hits) < limit:
        event = view.events[ep]
        ep += 1
        if titella is not None and event.get("titella") != titella:
            continue
        if kind is not None and event.get("kind") != kind:
            continue
        ts = _event_datetime(event)
        if ts is not None:
            if since_dt is not None and ts < since_dt:
                continue
            if until_dt is not None and ts > until_dt:
                continue
        matched, snippet = _event_hit(event, needle_folded)
        if not matched:
            continue
        hits.append(_hit_from_event(event, snippet))

    next_cursor: str | None = None
    if len(hits) >= limit:
        if ep < len(view.events):
            next_cursor = _encode_cursor(
                {"v": 1, "a": audit_id, "f": fhash, "s": through_seq, "p": "e", "e": ep}
            )
        elif needle_folded and any(
            _artifact_eligible(
                view.evidence_index[candidate], titella, kind, since_dt, until_dt
            )
            for candidate in view.evidence_order
        ):
            next_cursor = _encode_cursor(
                {"v": 1, "a": audit_id, "f": fhash, "s": through_seq, "p": "a", "i": None, "b": 0}
            )
    elif needle_folded:
        # Phase 2: text artifacts referenced by the snapshot, in journal
        # order, capped to sealed sizes or confirmed stream prefixes.
        art_idx = 0
        if start_artifact is not None:
            try:
                art_idx = view.evidence_order.index(start_artifact)
            except ValueError:
                raise AuditError(
                    "invalid_argument", "cursor artifact is not in the snapshot", False
                )
        resume: tuple[str | None, int] | None = None
        while art_idx < len(view.evidence_order):
            eid = view.evidence_order[art_idx]
            art_idx += 1
            fb = start_fb if (start_artifact is not None and eid == start_artifact) else 0
            meta = view.evidence_index[eid]
            if not _artifact_eligible(meta, titella, kind, since_dt, until_dt):
                continue
            try:
                path = _artifact_path(view, meta)
            except AuditError:
                continue
            if not path.is_file():
                continue
            cap = _scan_cap(meta, path)
            it = _Peekable(_iter_artifact_hits(path, cap, needle_folded, report_from=fb))
            last_offset: int | None = None
            while True:
                nxt = it.peek()
                if nxt is _MISSING:
                    break
                if len(hits) >= limit:
                    resume = (eid, (last_offset + 1) if last_offset is not None else fb)
                    break
                offset, snippet = nxt
                hits.append(_hit_from_artifact(meta, offset, snippet))
                last_offset = offset
                it.advance()
            if resume is not None:
                break
        if resume is not None:
            next_cursor = _encode_cursor(
                {"v": 1, "a": audit_id, "f": fhash, "s": through_seq, "p": "a", "i": resume[0], "b": resume[1]}
            )

    result: dict[str, Any] = {
        "ok": True,
        "audit_id": audit_id,
        "through_seq": through_seq,
        "hits": hits,
        "next_cursor": next_cursor,
    }
    if view.corrupt_tail:
        result["warning"] = (
            f"audit_corrupt: the journal ends with an incomplete record; "
            f"{view.through_seq} complete events were searched. {view.corrupt_warning}"
        )
    return result


# ── read ───────────────────────────────────────────────────────────────


def _stream_sha256(path: Path, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = limit
    with path.open("rb") as stream:
        while True:
            want = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            if want <= 0:
                break
            block = stream.read(want)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest()


def _confirmed_chunk_span(meta: "_EvidenceMeta") -> tuple[int, str | None]:
    """Contiguously journal-confirmed prefix length and its recorded hash.

    Chunk events tile the stream from offset 0; a gap in the tiling means the
    journal did not confirm further bytes, so the readable prefix stops there.
    The prefix hash is only reported when it describes exactly that span.
    """
    confirmed = meta.confirmed_prefix if isinstance(meta.confirmed_prefix, int) else 0
    chunks = meta.chunks or []
    tiled = 0
    for offset, length, _chunk_sha in chunks:
        if offset != tiled:
            break
        tiled += length
    if chunks:
        span = min(confirmed, tiled) if confirmed else tiled
    else:
        span = max(0, confirmed)
    sha = meta.prefix_sha256 if span == confirmed and isinstance(meta.prefix_sha256, str) else None
    return max(0, span), sha


def _verify_open_prefix(path: Path, meta: "_EvidenceMeta", start: int, end: int) -> None:
    """Verifies the on-disk bytes of [start, end) against the per-chunk hashes
    recorded in the journal. Bytes lost after a crash, or tampered, surface as
    audit_integrity_failed instead of being served as confirmed evidence."""
    if end <= start:
        return
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AuditError(
            "audit_integrity_failed", f"artifact {meta.evidence_id} is unreadable", False
        ) from exc
    if size < end:
        raise AuditError(
            "audit_integrity_failed",
            f"artifact {meta.evidence_id} is shorter than its confirmed prefix",
            False,
            expected_size=end,
            actual_size=size,
        )
    with path.open("rb") as stream:
        for offset, length, chunk_sha in meta.chunks or []:
            chunk_end = offset + length
            if chunk_end <= start:
                continue
            if offset >= end:
                break
            stream.seek(offset)
            digest_state = hashlib.sha256()
            remaining = length
            while remaining:
                block = stream.read(min(65536, remaining))
                if not block:
                    raise AuditError("audit_integrity_failed", f"artifact {meta.evidence_id} has a truncated chunk", False)
                digest_state.update(block)
                remaining -= len(block)
            digest = digest_state.hexdigest()
            if digest != chunk_sha:
                raise AuditError(
                    "audit_integrity_failed",
                    f"artifact {meta.evidence_id} chunk at {offset} differs from the journal",
                    False,
                    expected_sha256=chunk_sha,
                    actual_sha256=digest,
                )


def _verify_sealed(path: Path, meta: _EvidenceMeta) -> None:
    """Streamed sha256/size verification of a sealed artifact before use."""
    if not meta.sealed:
        return
    expected_sha = meta.sha256
    expected_size = meta.size
    if expected_sha is None and expected_size is None:
        return  # sealed without a recorded digest (OCR result artifacts)
    st = path.stat()
    if expected_size is not None and st.st_size != expected_size:
        raise AuditError(
            "audit_integrity_failed",
            f"artifact {meta.evidence_id} size differs from the sealed record",
            False,
            expected_sha256=expected_sha,
            actual_size=st.st_size,
        )
    if expected_sha is None:
        return
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _VERIFIED_CACHE.get(key)
    digest = cached if cached is not None else _stream_sha256(path)
    if cached is None:
        _VERIFIED_CACHE[key] = digest
    if digest != expected_sha:
        raise AuditError(
            "audit_integrity_failed",
            f"artifact {meta.evidence_id} hash differs from the sealed record",
            False,
            expected_sha256=expected_sha,
            actual_sha256=digest,
        )


def _artifact_path(view: _ArchiveView, meta: _EvidenceMeta) -> Path:
    rel = str(meta.relative_path)
    if not REL_PATH_RE.fullmatch(rel):
        raise AuditError(
            "audit_invalid_directory",
            f"evidence {meta.evidence_id} has an invalid relative path {rel!r}",
            False,
        )
    return check_safe_descendant(view.root, view.directory / rel)


def audit_read(
    root: Path,
    audit_id: str,
    evidence_id: str,
    offset: int = 0,
    length: int = 65536,
) -> list[Any]:
    """Reads one artifact by id (never by path). Returns [payload_dict].

    Sealed artifacts are hash-verified before serving; open streams are
    capped to the journal-confirmed prefix, hashed over that prefix and
    reported complete:false. PNG evidence is served whole at offset 0.
    """
    root = Path(root)
    validate_audit_id(audit_id)
    if _hex32(evidence_id) is None:
        raise AuditError(
            "evidence_not_found", f"evidence id {evidence_id!r} is not valid", False
        )
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise AuditError("invalid_argument", "offset must be >= 0", False)
    if not isinstance(length, int) or isinstance(length, bool) or not 1 <= length <= MAX_READ_CHUNK:
        raise AuditError(
            "invalid_argument", f"length must be between 1 and {MAX_READ_CHUNK}", False
        )
    view = load_archive(root, audit_id)
    meta = view.evidence_index.get(evidence_id)
    if meta is None:
        raise AuditError(
            "evidence_not_found",
            f"evidence {evidence_id} is not registered in audit {audit_id!r}",
            False,
        )
    path = _artifact_path(view, meta)
    if not path.is_file():
        raise AuditError(
            "evidence_not_found",
            f"evidence {evidence_id} artifact is missing from the archive",
            False,
        )
    rel = str(meta.relative_path)
    if rel.endswith(".png"):
        if offset != 0:
            raise AuditError(
                "invalid_argument", "PNG evidence only supports offset=0", False
            )
        _verify_sealed(path, meta)
        data = path.read_bytes()
        return [
            {
                "type": "image",
                "mime_type": "image/png",
                "data_b64": base64.b64encode(data).decode("ascii"),
                "evidence_id": evidence_id,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "sealed": meta.sealed,
                "complete": meta.complete,
                "role": meta.role,
            }
        ]

    # Binary/text pagination.
    file_len = path.stat().st_size
    if meta.sealed:
        _verify_sealed(path, meta)
        readable = meta.size if isinstance(meta.size, int) and meta.size >= 0 else file_len
        readable = min(readable, file_len)
        complete = meta.complete
        prefix_sha = meta.sha256 or _stream_sha256(path)
    else:
        readable, prefix_sha = _confirmed_chunk_span(meta)
        complete = False
    if offset >= readable:
        return [
            {
                "type": "bytes",
                "evidence_id": evidence_id,
                "mime_type": meta.mime_type,
                "role": meta.role,
                "offset": offset,
                "length": 0,
                "size": readable,
                "sealed": meta.sealed,
                "complete": complete,
                "data_b64": "",
                "text_utf8": "",
                "eof": True,
                "next_offset": readable,
                "sha256": prefix_sha,
                "confirmed_prefix": readable,
            }
        ]
    want = min(length, readable - offset)
    if not meta.sealed:
        # Hash every intersecting chunk, including bytes outside this page,
        # before returning any portion of the confirmed evidence.
        _verify_open_prefix(path, meta, offset, offset + want)
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(want)
    next_offset = offset + len(data)
    return [
        {
            "type": "bytes",
            "evidence_id": evidence_id,
            "mime_type": meta.mime_type,
            "role": meta.role,
            "offset": offset,
            "length": len(data),
            "size": readable,
            "sealed": meta.sealed,
            "complete": complete,
            "data_b64": base64.b64encode(data).decode("ascii"),
            "text_utf8": data.decode("utf-8", errors="replace"),
            "eof": next_offset >= readable,
            "next_offset": next_offset,
            "sha256": prefix_sha,
            "confirmed_prefix": readable,
        }
    ]


# ── note ───────────────────────────────────────────────────────────────


def audit_note(
    root: Path,
    manager: Any,
    audit_id: str,
    text: str,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    validate_audit_id(audit_id)
    if not isinstance(text, str) or not text.strip():
        raise AuditError("invalid_argument", "note text must be non-empty UTF-8", False)
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_NOTE_BYTES:
        raise AuditError("invalid_argument", f"note exceeds {MAX_NOTE_BYTES} bytes", False)
    ids = list(evidence_ids or [])
    if len(set(ids)) != len(ids):
        raise AuditError("invalid_argument", "evidence_ids contains duplicates", False)
    view = load_archive(Path(root), audit_id)
    if view.corrupt_tail:
        raise AuditError(
            "audit_corrupt",
            f"journal tail is corrupt; refusing to append a note without "
            f"repairing originals: {view.corrupt_warning}",
            False,
        )
    for eid in ids:
        if _hex32(eid) is None or eid not in view.evidence_index:
            raise AuditError(
                "evidence_not_found",
                f"evidence {eid!r} does not belong to audit {audit_id!r}",
                False,
            )
    note_id = manager.record_note(audit_id, text, ids)
    return {"ok": True, "audit_id": audit_id, "evidence_id": note_id, "references": ids}


# ── export ─────────────────────────────────────────────────────────────


def _collect_hex_ids(value: Any, out: set[str]) -> None:
    if isinstance(value, str):
        if EVIDENCE_ID_RE.fullmatch(value):
            out.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_hex_ids(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_hex_ids(item, out)


def _event_mentions(event: dict[str, Any]) -> set[str]:
    mentions: set[str] = set()
    data = event.get("data")
    if isinstance(data, dict):
        _collect_hex_ids(data, mentions)
    return mentions


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_export_file(target: Path, payload: bytes) -> None:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _render_evidence_md(manifest: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Evidence index — audit `{manifest['audit_id']}`")
    lines.append("")
    lines.append(
        f"Export `{manifest['export_id']}` created {manifest['created_at_utc']}; "
        f"snapshot through seq {manifest['through_seq']}. "
        "This package contains raw, unredacted originals: confidential."
    )
    lines.append("")
    for entry in manifest["evidence"]:
        eid = entry["evidence_id"]
        origin = entry.get("origin") or {}
        state = "sealed" if entry["sealed"] else "open/partial"
        complete = "complete" if entry.get("complete") else "incomplete"
        lines.append(
            f"## `{eid}` — role **{entry['role']}** ({state}, {complete})"
        )
        if origin.get("ts_utc"):
            lines.append(
                f"- recorded {origin['ts_utc']} (seq {origin.get('seq')}, "
                f"titella `{origin.get('titella')}`)"
            )
        lines.append(f"- size: {entry['size']} bytes")
        lines.append(f"- sha256: `{entry['sha256']}`")
        lines.append(f"- file: [`{entry['file']}`]({entry['file']})")
        if entry.get("mime_type") == "image/png":
            lines.append("")
            lines.append(f"![evidence {eid}]({entry['file']})")
        lines.append("")
    omitted = manifest.get("omitted_references") or []
    lines.append("## Referenced but omitted")
    lines.append("")
    if not omitted:
        lines.append("(none)")
    else:
        lines.extend(f"- {entry}" for entry in omitted)
    lines.append("")
    warnings = manifest.get("warnings") or []
    lines.append("## Warnings")
    lines.append("")
    if not warnings:
        lines.append("(none)")
    else:
        lines.extend(f"- {entry}" for entry in warnings)
    return "\n".join(lines) + "\n"


def audit_export(
    root: Path,
    manager: Any,
    audit_id: str,
    evidence_ids: list[str],
) -> dict[str, Any]:
    """Builds a confidential export package of the selected evidence only.

    The write barrier runs before the snapshot view is built, assets are
    copied with streamed hashing (sealed artifacts are verified), the
    package is assembled in a private temporary directory and published
    with a single atomic rename after fsync. Journal originals are never
    modified; a failure never leaves a seemingly-complete package behind.
    """
    root = Path(root)
    validate_audit_id(audit_id)
    if not isinstance(evidence_ids, list) or not 1 <= len(evidence_ids) <= MAX_EXPORT_IDS:
        raise AuditError(
            "invalid_argument",
            f"evidence_ids must list between 1 and {MAX_EXPORT_IDS} entries",
            False,
        )
    if len(set(evidence_ids)) != len(evidence_ids):
        raise AuditError("invalid_argument", "evidence_ids contains duplicates", False)
    barrier_warning: str | None = None
    try:
        manager.flush_barrier()
    except AuditError as exc:
        barrier_warning = f"write barrier failed before export: {exc.message}"
    view = load_archive(root, audit_id)
    for eid in evidence_ids:
        if _hex32(eid) is None or eid not in view.evidence_index:
            raise AuditError(
                "evidence_not_found",
                f"evidence {eid!r} does not belong to audit {audit_id!r}",
                False,
            )

    export_id = uuid.uuid4().hex
    selected = list(dict.fromkeys(evidence_ids))
    exports_dir = view.directory / "exports"
    ensure_private_dir(exports_dir, root=root)
    tmp_dir = Path(tempfile.mkdtemp(prefix=".tmp-", dir=exports_dir))
    published = False
    try:
        check_safe_descendant(root, tmp_dir)
        assets_dir = tmp_dir / "assets"
        ensure_private_dir(assets_dir, root=root)

        warnings: list[str] = []
        omitted_references: list[str] = []
        if barrier_warning:
            warnings.append(barrier_warning)
        manifest_entries: list[dict[str, Any]] = []

        for eid in selected:
            meta = view.evidence_index[eid]
            path = _artifact_path(view, meta)
            if not path.is_file():
                raise AuditError(
                    "evidence_not_found", f"artifact {eid} is missing from the archive", False
                )
            file_len = path.stat().st_size
            if meta.sealed:
                cap = meta.size if isinstance(meta.size, int) and meta.size >= 0 else file_len
                if isinstance(meta.size, int) and meta.size != file_len:
                    raise AuditError(
                        "audit_integrity_failed",
                        f"artifact {eid} size differs from the sealed record",
                        False,
                    )
            else:
                cap, _sha = _confirmed_chunk_span(meta)
                if meta.chunks:
                    _verify_open_prefix(path, meta, 0, cap)
                warnings.append(
                    f"evidence {eid} was still open; frozen at {cap} bytes and marked partial"
                )
            dest = assets_dir / f"{eid}{path.suffix}"
            digest = hashlib.sha256()
            copied = 0
            out_fd = os.open(
                dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with path.open("rb") as reader, os.fdopen(out_fd, "wb") as writer:
                while copied < cap:
                    want = min(1024 * 1024, cap - copied)
                    block = reader.read(want)
                    if not block:
                        break
                    writer.write(block)
                    digest.update(block)
                    copied += len(block)
                writer.flush()
                os.fsync(writer.fileno())
            if copied != cap:
                raise AuditError(
                    "audit_integrity_failed",
                    f"artifact {eid} ended before its recorded length",
                    False,
                )
            if meta.sealed and meta.sha256 is not None:
                if digest.hexdigest() != meta.sha256:
                    raise AuditError(
                        "audit_integrity_failed",
                        f"artifact {eid} hash differs from the sealed record during export",
                        False,
                    )
            related = list(meta.related_evidence_ids)
            for other in related:
                if other not in selected:
                    omitted_references.append(
                        f"evidence `{eid}` references `{other}`, which was not selected for export"
                    )
            origin = meta.origin
            manifest_entries.append(
                {
                    "evidence_id": eid,
                    "file": f"assets/{dest.name}",
                    "role": meta.role,
                    "mime_type": meta.mime_type,
                    "size": copied,
                    "sha256": digest.hexdigest(),
                    "sealed": meta.sealed,
                    "complete": bool(meta.sealed and meta.complete),
                    "related_evidence_ids": related,
                    "origin": {
                        "event_id": origin.event_id if origin else None,
                        "seq": origin.seq if origin else None,
                        "ts_utc": origin.ts_utc if origin else None,
                        "titella": origin.titella if origin else None,
                    },
                }
            )

        selected_set = set(selected)
        kept_events = [
            event for event in view.events if _event_mentions(event) & selected_set
        ]
        events_out = tmp_dir / "events.jsonl"
        payload = b"".join(
            (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            for event in kept_events
        )
        _write_export_file(events_out, payload)

        journal_gaps = sum(1 for e in view.events if e.get("kind") == "recording.gap")
        any_incomplete = any(not entry["complete"] for entry in manifest_entries)
        if view.corrupt_tail:
            warnings.append(
                f"audit_corrupt: journal tail is incomplete; exported through seq "
                f"{view.through_seq}. {view.corrupt_warning}"
            )
        complete = not (view.corrupt_tail or journal_gaps > 0 or any_incomplete)
        manifest = {
            "schema_version": 1,
            "export_id": export_id,
            "audit_id": audit_id,
            "created_at_utc": utc_now_rfc3339(),
            "through_seq": view.through_seq,
            "selection": selected,
            "contains_raw_evidence": True,
            "events_included": len(kept_events),
            "gaps": journal_gaps,
            "corrupt_tail": view.corrupt_tail,
            "complete": complete,
            "evidence": manifest_entries,
            "omitted_references": omitted_references,
            "warnings": warnings,
        }
        manifest_path = tmp_dir / "manifest.json"
        _write_export_file(
            manifest_path,
            (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        index_path = tmp_dir / "evidence.md"
        _write_export_file(index_path, _render_evidence_md(manifest).encode("utf-8"))

        _fsync_dir(assets_dir)
        _fsync_dir(tmp_dir)
        final_dir = exports_dir / export_id
        os.rename(tmp_dir, final_dir)
        published = True
        _fsync_dir(exports_dir)
    finally:
        if not published:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    recorded = True
    record_warning: str | None = None
    try:
        manager.record_export(audit_id, export_id, selected, str(exports_dir / export_id))
    except AuditError as exc:
        recorded = False
        record_warning = f"export.created event could not be recorded: {exc.message}"
    if record_warning:
        # The package on disk is complete; surface the journaling failure
        # explicitly instead of reporting an unqualified success.
        manifest_path = exports_dir / export_id / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.setdefault("warnings", []).append(record_warning)
            manifest["recorded"] = False
            _write_export_file(
                manifest_path,
                (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
            _write_export_file(
                exports_dir / export_id / "evidence.md",
                _render_evidence_md(manifest).encode("utf-8"),
            )
            _fsync_dir(exports_dir / export_id)
        except (OSError, ValueError):
            pass

    result = {
        "ok": True,
        "export_id": export_id,
        "directory": str(exports_dir / export_id),
        "manifest_path": str(exports_dir / export_id / "manifest.json"),
        "index_path": str(exports_dir / export_id / "evidence.md"),
        "through_seq": view.through_seq,
        "complete": complete,
        "warnings": warnings + ([record_warning] if record_warning else []),
        "recorded": recorded,
    }
    return result


__all__ = [
    "audit_export",
    "audit_note",
    "audit_read",
    "audit_search",
    "load_archive",
]
