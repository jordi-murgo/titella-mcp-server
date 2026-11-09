"""Shared FastMCP core of the HID auditor."""
from __future__ import annotations

import atexit
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP, Image
from pydantic import Field

from . import __version__
from .config import Config, load_config
from .documentation import SERVER_INSTRUCTIONS, register_documentation
from .firmware_info import FirmwareInfo
from .registry import TitellaRegistry
from .serial_link import SerialLink, TransportError


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, TransportError):
        return {"ok": False, "status": "error", "error": exc.as_dict()}
    if isinstance(exc, KeyError):
        message = exc.args[0] if exc.args else "not_found"
        return {"ok": False, "status": "error", "error": {
            "code": str(message), "message": str(message), "retryable": False,
        }}
    if isinstance(exc, (ValueError, TypeError)):
        return {"ok": False, "status": "error", "error": {
            "code": "invalid_argument", "message": str(exc), "retryable": False,
        }}
    return {"ok": False, "status": "error", "error": {
        "code": "internal_error", "message": str(exc), "retryable": False,
    }}


def create_server(
    config: Config | None = None,
    link: SerialLink | None = None,
    firmware: FirmwareInfo | None = None,
    registry: TitellaRegistry | None = None,
) -> FastMCP:
    config = config or load_config()
    reg = registry or TitellaRegistry(config, link=link, firmware=firmware)
    server = FastMCP("titella", instructions=SERVER_INSTRUCTIONS, log_level="ERROR")
    # FastMCP does not propagate `version` to its internal server: without this,
    # initialize announces the MCP SDK version instead of the service version and
    # the wrapper cannot tell an old daemon from a new one.
    server._mcp_server.version = __version__
    # Documentation tools and resources are static package assets: they are
    # registered once here and never gate on the rig, the lease or the agent.
    register_documentation(server)

    def shutdown() -> None:
        # The slave outlives the shared HTTP daemon. Preserve the correlation
        # id so the next daemon can bind the already-running agent instead of
        # presenting a new link as an attempted takeover.
        reg.close_all()

    atexit.register(shutdown)

    # ── multi-titella registry and discovery ────────────────────────────
    @server.tool(
        description=(
            "Lists all registered [titella.<id>] instances, which one is the default, "
            "and each instance's transport, connection, lease and agent readiness."
        ),
    )
    def titella_list() -> dict[str, Any]:
        try:
            return reg.list_titellas()
        except BaseException as exc:
            return _error(exc)

    @server.tool(
        description=(
            "Safely discovers candidate serial ports (WCH 0x1a86 / Espressif 0x303a) "
            "and local UVC video capture devices without opening or writing to unknown ports."
        ),
    )
    def titella_discover() -> dict[str, Any]:
        try:
            return reg.discover()
        except BaseException as exc:
            return _error(exc)

    @server.tool(
        description=(
            "Registers or updates a [titella.<id>] entry in config.toml and reloads it "
            "in place without interrupting other active titellas."
        ),
    )
    def titella_upsert(
        titella: str,
        transport: str | None = None,
        serial_port: str | None = None,
        serial_baud: int | None = None,
        wifi_host: str | None = None,
        wifi_port: int | None = None,
        screen_device: str | None = None,
        screen_input_format: str | None = None,
        screen_pixel_format: str | None = None,
        screen_framerate: int | None = None,
        keyboard_layout: str | None = None,
        slave_platform: str | None = None,
        slave_cdc_port: str | None = None,
        terminal_mods: list[str] | str | None = None,
        terminal_key: str | None = None,
        launcher_text: str | None = None,
        set_default: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        try:
            return reg.upsert(
                titella,
                transport=transport,
                serial_port=serial_port,
                serial_baud=serial_baud,
                wifi_host=wifi_host,
                wifi_port=wifi_port,
                screen_device=screen_device,
                screen_input_format=screen_input_format,
                screen_pixel_format=screen_pixel_format,
                screen_framerate=screen_framerate,
                keyboard_layout=keyboard_layout,
                slave_platform=slave_platform,
                slave_cdc_port=slave_cdc_port,
                terminal_mods=terminal_mods,
                terminal_key=terminal_key,
                launcher_text=launcher_text,
                set_default=set_default,
                force=force,
            )
        except BaseException as exc:
            return _error(exc)

    @server.tool(
        description=(
            "Removes a [titella.<id>] entry from config.toml and closes its link. "
            "Refuses with titella_busy if its slave agent is active unless force=True."
        ),
    )
    def titella_remove(titella: str, force: bool = False) -> dict[str, Any]:
        try:
            return reg.remove(titella, force=force)
        except BaseException as exc:
            return _error(exc)

    @server.tool(
        description="Sets the default [titella.<id>] used when tool calls omit the titella parameter.",
    )
    def titella_set_default(titella: str) -> dict[str, Any]:
        try:
            return reg.set_default(titella)
        except BaseException as exc:
            return _error(exc)

    # ── screen and lease ────────────────────────────────────────────────
    @server.tool(
        description=(
            "Captures the screen from the local UVC capture card and fixes the mouse_abs resolution. "
            "ok:true with a black image means the slave is not emitting a signal (powered off, suspended or "
            "HDMI disconnected), not a capture failure: the card always produces a valid frame."
        ),
        structured_output=False,
    )
    def screen(titella: str | None = None) -> list[Any]:
        try:
            inst = reg.resolve(titella)
        except BaseException as exc:
            return [_error(exc)]
        metadata, image = inst.capture.capture()
        if image is None:
            return [metadata]
        return [Image(data=image, format="png"), metadata]

    @server.tool(description="Acquires the logical keyboard and mouse lease.")
    def lock_acquire(owner: str, lease_s: int = 600, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).lease.acquire(owner, lease_s)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Renews the logical HID lease.")
    def lock_renew(owner: str, lease_s: int = 600, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).lease.renew(owner, lease_s)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Releases only the HID lease; it does not close the UART or the remote sessions.")
    def lock_release(owner: str, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).lease.release(owner)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Returns the HID lease status.")
    def lock_status(owner: str = "", titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).lease.status(owner)
        except BaseException as exc:
            return _error(exc)

    # ── HID ─────────────────────────────────────────────────────────────
    @server.tool(
        description=(
            "Moves the cursor to pixels of the last capture. Returns screen_required when there is no "
            "previous capture and coordinates_out_of_bounds outside its resolution."
        ),
    )
    def mouse_abs(owner: str, x: int, y: int, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).hid.mouse_abs(owner, x, y)
        except BaseException as exc:
            return _error(exc)

    @server.tool(
        description=(
            "Moves the cursor with deltas between -127 and 127. The deltas are relative: at the edge "
            "they saturate and produce no change, so alternate the sign before concluding that the HID has failed."
        ),
    )
    def mouse_rel(owner: str, x: int, y: int, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).hid.mouse_rel(owner, x, y)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Performs a left, right or middle click.")
    def click(owner: str, button: str = "left", double: bool = False, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).hid.click(owner, button, double)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Sends vertical scroll between -127 and 127.")
    def wheel(owner: str, delta: int, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).hid.wheel(owner, delta)
        except BaseException as exc:
            return _error(exc)

    @server.tool(
        description=(
            "Presses a key with modifiers. An ok:true only proves that the firmware emitted the "
            "frame, not that the slave processed it: verify with screen()."
        ),
    )
    def key(owner: str, key: str, mods: list[str] | None = None, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).hid.key(owner, key, mods)
        except BaseException as exc:
            return _error(exc)

    @server.tool(name="type", description="Types literal text over HID.")
    def type_text(owner: str, text: str, delay_ms: int = 10, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).hid.type_text(owner, text, delay_ms)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Selects the es_ES or en_US HID layout.")
    def keyboard_layout(owner: str, layout: str, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).hid.keyboard_layout(owner, layout)
        except BaseException as exc:
            return _error(exc)

    # ── agent bootstrap ─────────────────────────────────────────────────
    @server.tool(
        description=(
            "Deploys the ephemeral slave agent over HID + CDC. Before every attempt, verify "
            "the target's active keyboard layout in the focused input window with screen(): "
            "the OS UI language, the configured layout and an earlier screenshot are not "
            "evidence for the window that will receive the keystrokes. Pass the verified layout "
            "explicitly (es_ES or en_US) and attest the fresh verification with "
            "layout_confirmed=true; the attestation is a caller claim, not a measured proof, "
            "because the server cannot inspect the target's input source. A missing layout "
            "answers slave_layout_required, an unsupported one invalid_argument and an "
            "unconfirmed one slave_layout_unconfirmed, each before any keystroke. If the "
            "focused window's layout cannot be proven, or a launcher window transition could "
            "change it, stop instead of confirming. The slave platform must also be selected "
            "explicitly (macos, linux or windows); it is never inferred from the master. On "
            "macOS the launcher is Spotlight, on Linux the configured terminal binding, on "
            "Windows Start search plus PowerShell 5.1. bootstrap_loader_timeout usually means "
            "the launcher typed into the wrong window, a CDC permission problem, or a Windows "
            "policy block, not a protocol failure."
        ),
    )
    def slave_bootstrap(
        owner: str,
        layout: str | None = None,
        platform: str | None = None,
        cdc_port: str | None = None,
        layout_confirmed: Annotated[bool, Field(strict=True)] = False,
        titella: str | None = None,
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).bootstrap.bootstrap(
                owner, layout, platform=platform, cdc_port=cdc_port,
                layout_confirmed=layout_confirmed,
            )
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Status of the last slave agent bootstrap.")
    def slave_status(titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).bootstrap.status()
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Asks the slave agent to clean up and exit.")
    def slave_stop(titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).bootstrap.stop()
        except BaseException as exc:
            return _error(exc)

    # ── shell one-shot ──────────────────────────────────────────────────
    @server.tool(
        description=(
            "Runs a remote command; returns the result if it finishes within sync_timeout and otherwise "
            "a job_id for remote_shell_result. Requires the slave agent to be running (slave_bootstrap)."
        ),
    )
    def remote_shell(
        cmd: str,
        sync_timeout: float = 10,
        timeout: float = 60,
        cwd: str | None = None,
        titella: str | None = None,
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).shells.remote_shell(cmd, sync_timeout, timeout, cwd)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Returns the status of a one-shot job.")
    def remote_shell_result(job_id: str, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).shells.shell_result(job_id)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Cancels a one-shot job; idempotent.")
    def remote_shell_cancel(job_id: str, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).shells.shell_cancel(job_id)
        except BaseException as exc:
            return _error(exc)

    # ── terminal PTY ────────────────────────────────────────────────────
    @server.tool(description="Opens an interactive PTY on the slave.")
    def remote_terminal_open(
        cwd: str | None = None,
        shell: str | None = None,
        rows: int = 40,
        cols: int = 120,
        idle_timeout: int = 900,
        titella: str | None = None,
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).shells.terminal_open(cwd, shell, rows, cols, idle_timeout)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Reads PTY output from an absolute offset.")
    def remote_terminal_read(
        session_id: str,
        after: int = 0,
        wait_seconds: float = 1,
        max_bytes: int = 32768,
        encoding: str = "utf-8",
        titella: str | None = None,
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).shells.terminal_read(
                session_id, after, wait_seconds, max_bytes, encoding
            )
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Writes to the PTY; accepts exact bytes in base64.")
    def remote_terminal_write(
        session_id: str,
        data: str,
        encoding: str = "utf-8",
        titella: str | None = None,
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).shells.terminal_write(session_id, data, encoding)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Resizes the remote PTY.")
    def remote_terminal_resize(
        session_id: str, rows: int, cols: int, titella: str | None = None
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).shells.terminal_resize(session_id, rows, cols)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Closes the PTY and its process group; idempotent.")
    def remote_terminal_close(session_id: str, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).shells.terminal_close(session_id)
        except BaseException as exc:
            return _error(exc)

    # ── files and system ────────────────────────────────────────────────
    @server.tool(description="Reads bytes of a remote file by offset, up to 64 KiB.")
    def remote_fs_read(
        path: str,
        offset: int = 0,
        length: int = 32768,
        encoding: str = "utf-8",
        titella: str | None = None,
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.read(path, offset, length, encoding)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Writes up to 64 KiB atomically; for more, use remote_upload.")
    def remote_fs_write(
        path: str, data: str, encoding: str = "utf-8", titella: str | None = None
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.write(path, data, encoding)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Lists a directory with pagination of up to 200 entries.")
    def remote_fs_list(
        path: str, offset: int = 0, limit: int = 200, titella: str | None = None
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.list(path, offset, limit)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Deletes a file or symlink; never recursive and never directories.")
    def remote_fs_delete(path: str, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.delete(path)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Replaces exactly one occurrence in a file.")
    def remote_fs_replace(
        path: str, old: str, new: str, titella: str | None = None
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.replace(path, old, new)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Searches for a string in a file, up to 200 matches.")
    def remote_fs_search(
        path: str, query: str, limit: int = 200, titella: str | None = None
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.search(path, query, limit)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Searches names by glob pattern, up to 500 results.")
    def remote_fs_find(
        path: str, pattern: str, limit: int = 500, titella: str | None = None
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.find(path, pattern, limit)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="System, Python version, uid and home of the slave.")
    def remote_sysinfo(titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.sysinfo()
        except BaseException as exc:
            return _error(exc)

    # ── transfers ───────────────────────────────────────────────────────
    @server.tool(description="Uploads a file in 512-byte blocks with SHA-256 and an atomic rename.")
    def remote_upload(
        local_path: str,
        remote_path: str,
        sync_timeout: float = 10,
        titella: str | None = None,
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.upload(local_path, remote_path, sync_timeout)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Downloads a file with SHA-256 verification and an atomic rename.")
    def remote_download(
        remote_path: str,
        local_path: str,
        checksum: str | None = None,
        sync_timeout: float = 10,
        titella: str | None = None,
    ) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.download(
                remote_path, local_path, checksum, sync_timeout
            )
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Status and progress of a transfer.")
    def remote_transfer_status(transfer_id: str, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.transfer_status(transfer_id)
        except BaseException as exc:
            return _error(exc)

    @server.tool(description="Cancels a transfer and removes its partials.")
    def remote_transfer_cancel(transfer_id: str, titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).files.transfer_cancel(transfer_id)
        except BaseException as exc:
            return _error(exc)

    @server.tool(
        description=(
            "Aggregated KVM status and next steps: firmware, lease, agent, capture and what is "
            "missing to operate. Use it before diagnosing blindly."
        ),
    )
    def kvm_status(titella: str | None = None) -> dict[str, Any]:
        try:
            return reg.resolve(titella).kvm_status()
        except BaseException as exc:
            return _error(exc)

    return server
