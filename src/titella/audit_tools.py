"""Rig-independent evidence queries and explicit per-instance activation."""
from __future__ import annotations

import base64
from typing import Any

from mcp.server.fastmcp import Image

from .audit import AuditError
from .audit_query import audit_export as _export, audit_note as _note, audit_read as _read, audit_search as _search
from .serial_link import TransportError


def _error(exc: Exception) -> dict:
    if isinstance(exc, (AuditError, TransportError)):
        error = exc.as_dict()
    else:
        code = "invalid_argument" if isinstance(exc, (ValueError, TypeError)) else "internal_error"
        error = {"code": code, "message": str(exc), "retryable": False}
    return {"ok": False, "status": "error", "error": error}


def register_audit(server: Any, reg: Any) -> None:
    manager = reg.audit

    def busy(tid: str) -> list[str]:
        inst = reg.resolve(tid)
        pending = [f"job:{key}" for key, job in list(inst.shells.jobs.items()) if job.status in {"running", "reconnecting"}]
        pending += [f"terminal:{key}" for key, terminal in list(inst.shells.terminals.items()) if terminal.status in {"running", "reconnecting"}]
        pending += [f"transfer:{key}" for key, transfer in list(inst.files._transfers.items()) if not transfer.done.is_set()]
        return pending

    @server.tool(description="Explicitly activate a named audit on one instance. Read audit-mode first; pending operations must finish before activation.")
    def audit_activate(name: str, titella: str | None = None) -> dict[str, Any]:
        try:
            return manager.activate(name, reg.resolve(titella).titella_id, busy)
        except Exception as exc:
            return _error(exc)

    @server.tool(description="Deactivate this instance's audit without stopping the agent or releasing the lease. Refuses pending operations.")
    def audit_deactivate(titella: str | None = None) -> dict[str, Any]:
        try:
            return manager.deactivate(reg.resolve(titella).titella_id, busy)
        except Exception as exc:
            return _error(exc)

    @server.tool(description="Return this instance's audit association, pending IDs and recording health; no sensitive output.")
    def audit_status(titella: str | None = None) -> dict[str, Any]:
        try:
            return manager.status(reg.resolve(titella).titella_id)
        except Exception as exc:
            return _error(exc)

    @server.tool(description="List local audit archives ordered by creation, without requiring a rig or agent.")
    def audit_list() -> dict[str, Any]:
        try:
            return manager.list_audits()
        except Exception as exc:
            return _error(exc)

    @server.tool(description="Append an operator note (nonempty UTF-8, at most 16384 bytes), optionally referencing evidence in the same audit.")
    def audit_note(audit_id: str, text: str, evidence_ids: list[str] | None = None) -> dict[str, Any]:
        try:
            manager.flush_barrier()
            return _note(reg.audit_root, manager, audit_id, text, evidence_ids)
        except Exception as exc:
            return _error(exc)

    @server.tool(description="Search literal Unicode text over this audit's arguments, results, notes, OCR and shell streams. Empty query lists chronology; timezone-aware UTC filters and snapshot pagination.")
    def audit_search(audit_id: str, query: str = "", *, titella: str | None = None, kind: str | None = None,
                     since: str | None = None, until: str | None = None, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        try:
            manager.flush_barrier()
            return _search(reg.audit_root, audit_id, query, titella=titella, kind=kind,
                           since=since, until=until, cursor=cursor, limit=limit)
        except Exception as exc:
            return _error(exc)

    @server.tool(description="Read one registered evidence ID, never a path. Exact base64 bytes plus UTF-8 view; sealed hashes verified. PNG returns an image at offset zero only.", structured_output=False)
    def audit_read(audit_id: str, evidence_id: str, offset: int = 0, length: int = 65536) -> list[Any]:
        try:
            manager.flush_barrier()
            values = _read(reg.audit_root, audit_id, evidence_id, offset, length)
            result = []
            for value in values:
                if isinstance(value, dict) and value.get("type") == "image":
                    metadata = dict(value)
                    image = base64.b64decode(metadata.pop("data_b64"), validate=True)
                    result.extend([Image(data=image, format="png"), metadata])
                else:
                    result.append(value)
            return result
        except Exception as exc:
            return [_error(exc)]

    @server.tool(description="Export an explicit selection of 1..500 evidence IDs to a private atomic package. Contains sensitive raw originals, not an anonymized report.")
    def audit_export(audit_id: str, evidence_ids: list[str]) -> dict[str, Any]:
        try:
            return _export(reg.audit_root, manager, audit_id, evidence_ids)
        except Exception as exc:
            return _error(exc)
