"""Evidence boundary around the SDK dispatch; operational results are never replaced."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from .audit import AuditError, reset_current_audit_call, set_current_audit_call
from .serial_link import TransportError

NON_OPERATIONAL_TOOLS = frozenset({"docs_index", "docs_read", "titella_list", "titella_discover", "titella_set_default", "titella_upsert", "titella_remove"})
SAFETY_TOOLS = frozenset({"release_all", "slave_stop", "lock_release", "remote_shell_cancel", "remote_terminal_close", "remote_transfer_cancel"})


def _request_metadata(context: Any) -> dict[str, Any]:
    request_id = client = None
    try:
        request_id = context.request_id
        params = context.session.client_params
        if params is not None:
            client = params.clientInfo.model_dump(mode="json")
    except (ValueError, LookupError, AttributeError):
        pass
    return {"request_id": request_id, "clientInfo": client, "identity_is_declared": True}


def _classify_payload(result: Any) -> dict[str, Any] | None:
    values = [result]
    if isinstance(result, tuple) and len(result) == 2:
        values = [result[1], result[0]]
    for value in values:
        if isinstance(value, dict):
            if value.get("ok") is False or value.get("status") == "error":
                error = value.get("error")
                return dict(error) if isinstance(error, dict) else {"code": "error", "retryable": False}
        elif isinstance(value, (tuple, list)):
            for block in value:
                if isinstance(block, dict):
                    found = _classify_payload(block)
                else:
                    text = getattr(block, "text", None)
                    try:
                        found = _classify_payload(json.loads(text)) if isinstance(text, str) else None
                    except (ValueError, TypeError):
                        found = None
                if found is not None:
                    return found
    return None


class AuditFastMCP(FastMCP):
    def __init__(self, *args: Any, audit_registry: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.audit_registry = audit_registry

    def _operational(self, name: str) -> bool:
        if name.startswith("audit_") or name in NON_OPERATIONAL_TOOLS:
            return False
        tool = self._tool_manager.get_tool(name)
        return bool(tool and "titella" in tool.parameters.get("properties", {}))

    def _rejection(self, name: str, exc: BaseException) -> Any:
        code = getattr(exc, "code", "invalid_argument")
        if isinstance(exc, KeyError):
            code = "titella_not_found"
        elif isinstance(exc, LookupError):
            code = "titella_required"
        payload = {"ok": False, "status": "error", "error": {"code": code, "message": str(exc), "retryable": bool(getattr(exc, "retryable", False))}}
        content = [TextContent(type="text", text=json.dumps(payload))]
        tool = self._tool_manager.get_tool(name)
        return (content, payload) if tool and tool.output_schema is not None else content

    async def _warn_recording(self) -> None:
        try:
            await self.get_context().warning("Audit evidence recording failed. The operational result is unchanged; stop safely and inspect audit_status. Do not repeat a remote action to repair evidence.")
        except Exception:
            pass

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        reg = self.audit_registry
        if reg is None or not self._operational(name):
            return await super().call_tool(name, arguments)
        manager = reg.audit
        call = None
        tid = None
        entered = False
        delegated = dict(arguments)
        try:
            # Resolution and reservation share the registry's short exclusion
            # with mutations/association transitions. No I/O under this lock.
            with manager._selection_lock:
                instance = reg.resolve(arguments.get("titella"))
                tid = instance.titella_id
                delegated["titella"] = tid
                manager.enter_instance(tid)
                entered = True
                try:
                    call = manager.reserve_call(tid, name, _request_metadata(self.get_context()))
                except AuditError:
                    if name not in SAFETY_TOOLS:
                        raise
            if call is not None:
                try:
                    await asyncio.to_thread(manager.persist_call, call, {"original": arguments, "resolved": delegated,
                                                                          "transport": instance.config.transport if hasattr(instance, "config") else None})
                except AuditError:
                    if name not in SAFETY_TOOLS:
                        raise
                    call = None
        except (AuditError, TransportError, KeyError, LookupError, ValueError) as exc:
            if entered:
                manager.exit_instance(tid)
            return self._rejection(name, exc)

        token = set_current_audit_call(call)
        try:
            try:
                result = await super().call_tool(name, delegated)
            except BaseException as exc:
                if call is not None:
                    try:
                        await asyncio.to_thread(manager.finish_call, call, error={"code": getattr(exc, "code", "dispatch_error"),
                                               "message": str(exc), "retryable": getattr(exc, "retryable", False), "phase": "dispatch"})
                    except BaseException:
                        await self._warn_recording()
                raise
            if call is not None:
                try:
                    await asyncio.to_thread(manager.finish_call, call, result, _classify_payload(result))
                except BaseException:
                    await self._warn_recording()
                if manager.faulted(tid):
                    await self._warn_recording()
            return result
        finally:
            reset_current_audit_call(token)
            manager.exit_instance(tid)
