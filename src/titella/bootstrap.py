"""Ephemeral bootstrap of a single slave agent over HID and CDC.

The slave platform is an explicit selection (argument, then configuration,
then error): inferring it from the master would launch a payload the target
cannot run. Each platform profile fixes the implementation, the launcher
defaults and the waits its payload needs before announcing itself.

The target's active keyboard layout is a precondition of every attempt, not a
default: the caller verifies the focused window's input source, passes the
layout explicitly and attests that fresh verification. A missing, unsupported
or unconfirmed layout is rejected before any keystroke, because a mismatched
HID map garbles the loader from its first character.
"""
from __future__ import annotations

import base64
import gzip
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from titella_slave_assets.bundles import BundleError, load_bundle

from .agent_state import AgentGate
from .config import Config
from .hid import HidController
from .serial_link import SerialLink, TransportError

# HID typing limits shared by every platform: one request never exceeds 256
# characters. Windows needs 8 ms per character for the console to keep up.
_HID_CHUNK_CHARS = 256
_PYTHON_TYPE_DELAY_MS = 1
_LAUNCHER_TYPE_DELAY_MS = 8
_WINDOWS_TYPE_DELAY_MS = 8
# Longest base64 accumulation line typed into the Windows console.
_WINDOWS_LINE_CHARS = 512
_WINDOWS_LAUNCHER_READY_S = 1.2
_WINDOWS_SEARCH_SETTLE_S = 0.6
_WINDOWS_CONSOLE_READY_S = 3.5

_LINK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_WINDOWS_PORT_RE = re.compile(r"^COM[1-9][0-9]*$")


@dataclass(frozen=True)
class PlatformProfile:
    """Fixed bootstrap behaviour of one slave platform."""

    implementation: str
    # ``None`` means the operator must configure the binding (Linux has no
    # portable terminal shortcut). Otherwise it is the default that a
    # configured value overrides.
    launcher_mods: tuple[str, ...] | None
    launcher_key: str | None
    launcher_text: str
    loader_wait_s: float
    hello_wait_s: float


PLATFORM_PROFILES: dict[str, PlatformProfile] = {
    "macos": PlatformProfile(
        implementation="python",
        launcher_mods=("CMD",),
        launcher_key="SPACE",
        launcher_text="Terminal",
        loader_wait_s=20,
        hello_wait_s=20,
    ),
    "linux": PlatformProfile(
        implementation="python",
        launcher_mods=None,
        launcher_key=None,
        launcher_text="",
        loader_wait_s=20,
        hello_wait_s=20,
    ),
    "windows": PlatformProfile(
        # Ctrl+Esc opens Start without depending on the Windows key, which can
        # be intercepted by desktop software. Start search launches the
        # system Windows PowerShell 5.1.
        implementation="windows-dotnet",
        launcher_mods=("CTRL",),
        launcher_key="ESC",
        launcher_text="powershell",
        loader_wait_s=30,
        hello_wait_s=90,
    ),
}


def _check_identifier(kind: str, value: str) -> str:
    if not _LINK_ID_RE.match(value):
        raise ValueError(f"{kind} must be 32 lowercase hexadecimal characters")
    return value


def _render_loader(bundle_source: str, implementation: str, link_id: str,
                   session_id: str, cdc_port: str | None) -> str:
    """Substitutes the validated identifiers and the CDC override literal.

    The placeholder sits inside the template's own quotes, so only the literal
    *contents* are rendered: a Python repr (`None` or a quoted string) for the
    zipapp loader, and single-quote doubling for the PowerShell template.
    """
    _check_identifier("link_id", link_id)
    _check_identifier("session_id", session_id)
    port = str(cdc_port).strip() if cdc_port is not None else ""
    if implementation == "python":
        port_literal = repr(port) if port else "None"
    else:
        port_literal = port.replace("'", "''")
    return (
        bundle_source
        .replace("__LINK_ID__", link_id)
        .replace("__SESSION_ID__", session_id)
        .replace("__CDC_PORT__", port_literal)
    )


def loader_source(link_id: str, session_id: str, cdc_port: str | None = None,
                  implementation: str = "python") -> str:
    """Renders the loader shipped inside the slave-assets wheel."""
    _check_identifier("link_id", link_id)
    _check_identifier("session_id", session_id)
    bundle = load_bundle(implementation)
    return _render_loader(bundle.loader_source, implementation, link_id, session_id, cdc_port)


def _python_command(rendered: str) -> str:
    encoded = base64.b64encode(rendered.encode("utf-8")).decode("ascii")
    return f"python3 -c 'import base64;exec(base64.b64decode(\"{encoded}\"))'"


def _windows_lines(rendered: str) -> list[str]:
    compressed = gzip.compress(rendered.encode("utf-8"), mtime=0)
    encoded = base64.b64encode(compressed).decode("ascii")
    lines: list[str] = []
    for position in range(0, len(encoded), _WINDOWS_LINE_CHARS):
        chunk = encoded[position:position + _WINDOWS_LINE_CHARS]
        prefix = "$titellaLoader='" if position == 0 else "$titellaLoader+='"
        lines.append(f"{prefix}{chunk}'")
    lines.append(
        "$tb=[Convert]::FromBase64String($titellaLoader);"
        "$tm=[IO.MemoryStream]::new($tb);"
        "$tg=[IO.Compression.GzipStream]::new("
        "$tm,[IO.Compression.CompressionMode]::Decompress);"
        "$tr=[IO.StreamReader]::new($tg,[Text.Encoding]::UTF8);"
        ".([ScriptBlock]::Create($tr.ReadToEnd()))"
    )
    return lines


def windows_stub_command(link_id: str, cdc_port: str | None = None) -> str:
    """One line that opens the CDC, streams the loader with flow control, and runs it.

    The stub opens `$script:Sp` with DTR and RTS asserted, announces
    `stage.ready` over the stateless `SerialMux`, and reads `loader.push`
    chunks (512 Base64 characters each), replying with a `bootstrap` `response`
    frame per chunk so every frame is flow-controlled end to end without any
    staging buffer in the ESP32 firmware. The decompressed loader dot-sources
    in the same scope and reuses the open `$script:Sp` handle directly.

    Every frame carries the master's real `link_id`: the link only dispatches
    frames whose identifier matches, so the stub needs no special case.
    """
    _check_identifier("link_id", link_id)
    port = (cdc_port or "").strip()
    if port:
        if not _WINDOWS_PORT_RE.match(port):
            raise ValueError("cdc_port must match COM[1-9][0-9]*")
        select = "[IO.Ports.SerialPort]::new('" + port + "')"
    else:
        select = (
            "[IO.Ports.SerialPort]::new((Get-PnpDevice -Class Ports|"
            "?{$_.InstanceId -match 'VID_303A|TITELLA-' -and "
            "$_.FriendlyName -match '\\(COM[1-9][0-9]*\\)'}|"
            "%{'COM'+[regex]::Match($_.FriendlyName,'COM[1-9][0-9]*').Value}|"
            "Select-Object -First 1))"
        )
    return (
        "try{Set-PSReadLineOption -HistorySaveStyle SaveNothing}catch{};"
        "$p=" + select + ";$p.DtrEnable=$p.RtsEnable=1;$p.ReadTimeout=2e4;$p.Open();"
        "$p.DiscardInBuffer();$script:Sp=$p;"
        '$h=\'{"v":1,"channel":"bootstrap","type":"\';'
        '$p.WriteLine($h+\'event","link_id":"' + link_id + '","op":"stage.ready"}\');'
        "$b='';$t=1;while($b.Length-lt$t){$f=$p.ReadLine()|ConvertFrom-Json;if($f.op-eq'loader.push'){"
        '$t=$f.body.total;$b+=$f.body.data_b64;[Console]::Write(++$n);'
        '$p.WriteLine($h+\'response","link_id":"\'+$f.link_id+\'","request_id":\'+$f.request_id+\',"op":"loader.push","body":{"staged":\'+$b.Length+\'}}\')}};'
        "$r=[IO.StreamReader][IO.Compression.GzipStream]::new("
        "[IO.MemoryStream][Convert]::FromBase64String($b),"
        "[IO.Compression.CompressionMode]::Decompress);"
        "iex $r.ReadToEnd()"
    )


def windows_loader_blob(link_id: str, session_id: str,
                        cdc_port: str | None = None) -> str:
    """The gzip+base64 loader streamed to the Windows stub over the CDC."""
    rendered = loader_source(link_id, session_id, cdc_port,
                             implementation="windows-dotnet")
    return base64.b64encode(gzip.compress(rendered.encode("utf-8"), mtime=0)).decode("ascii")


def push_loader(
    link: SerialLink,
    blob: str,
    chunk_chars: int = 512,
    on_progress: Any | None = None,
) -> int:
    """Streams a loader blob to a stub over the stateless CDC mux.

    Every chunk is acknowledged by the stub with the running total before the
    next one is sent: end-to-end flow control with nothing buffered in firmware
    RAM. The same mechanism serves the Windows and POSIX stubs.
    """
    total = len(blob)
    offset = 0
    chunks_total = (total + chunk_chars - 1) // chunk_chars if total else 0
    chunk_index = 0
    if on_progress is not None:
        on_progress(0, total, 0, chunks_total)
    while offset < total:
        chunk = blob[offset:offset + chunk_chars]
        response = link.request(
            "bootstrap", "loader.push",
            {"offset": offset, "total": total, "data_b64": chunk},
            timeout=15,
        )
        body = dict(response.get("body") or {})
        staged = int(body.get("staged", -1))
        if staged != offset + len(chunk):
            raise TransportError(
                "loader_push_rejected",
                f"the stub staged {staged} characters, "
                f"expected {offset + len(chunk)}",
                True,
            )
        offset += len(chunk)
        chunk_index += 1
        if on_progress is not None:
            on_progress(offset, total, chunk_index, chunks_total)
    return offset


def push_windows_loader(
    link: SerialLink,
    blob: str,
    chunk_chars: int = 512,
    on_progress: Any | None = None,
) -> int:
    """Backwards-compatible alias of `push_loader` for the Windows path."""
    return push_loader(link, blob, chunk_chars, on_progress=on_progress)


_POSIX_STUB = '''import json as J,os,glob as G,base64 as B,gzip as Z,sys,termios as T,tty
L="__LINK_ID__";P=__CDC_PORT__
for p in([P]if P else sorted(G.glob("/dev/cu.usbmodem*"))+sorted(G.glob("/dev/ttyACM*"))):
 try:f=os.open(p,os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK);tty.setraw(f);a=T.tcgetattr(f);a[2]|=T.CREAD|T.CLOCAL;a[4]=a[5]=T.B115200;T.tcsetattr(f,T.TCSANOW,a);os.set_blocking(f,True);break
 except OSError:pass
else:sys.exit("cdc_not_found")
h='{"v":1,"channel":"bootstrap","type":"'
os.write(f,(h+'event","link_id":"'+L+'","op":"stage.ready"}\\n').encode())
b=[];n=0;t=1
for l in os.fdopen(f,"rb",closefd=False):
 try:o=J.loads(l)
 except Exception:continue
 if o.get("op")=="loader.push":
  y=o["body"];q=y["data_b64"];t=y["total"];b+=[q];n+=len(q);os.write(2,b".")
  os.write(f,(h+'response","link_id":"'+o["link_id"]+'","request_id":'+str(o["request_id"])+',"op":"loader.push","body":{"staged":'+str(n)+"}}\\n").encode())
  if n>=t:break
exec(Z.decompress(B.b64decode("".join(b))),{"__name__":"__titella_loader__","fd":f,"port":p,"L":L})
'''


def posix_stub_command(link_id: str, cdc_port: str | None = None) -> str:
    """One typed line that streams the loader over the CDC and runs it.

    Mirrors the Windows design: only this sub-1,000-character stub travels
    over HID, not the ~4,900-character loader source. The loader arrives over
    the stateless `SerialMux` with per-chunk flow control, and the CDC is
    opened exactly once per bootstrap. `cdc_port` is rendered as a Python
    literal; empty means "discover it on the slave".
    """
    _check_identifier("link_id", link_id)
    stub = (
        _POSIX_STUB
        .replace("__LINK_ID__", link_id)
        .replace("__CDC_PORT__", repr((cdc_port or "").strip()))
    )
    encoded = base64.b64encode(
        gzip.compress(stub.encode("utf-8"), mtime=0)
    ).decode("ascii")
    return (
        "python3 -c 'import base64,gzip;"
        f'exec(gzip.decompress(base64.b64decode("{encoded}")))'
        "'"
    )


def posix_loader_blob(link_id: str, session_id: str,
                      cdc_port: str | None = None) -> str:
    """The gzip+base64 Python loader streamed to the POSIX stub over the CDC."""
    rendered = loader_source(link_id, session_id, cdc_port, implementation="python")
    return base64.b64encode(gzip.compress(rendered.encode("utf-8"), mtime=0)).decode("ascii")


def loader_command(link_id: str, session_id: str, cdc_port: str | None = None) -> str:
    """One-line POSIX command that runs the rendered Python loader directly."""
    return _python_command(loader_source(link_id, session_id, cdc_port))


def windows_loader_lines(link_id: str, session_id: str,
                         cdc_port: str | None = None) -> list[str]:
    """Console lines that accumulate and execute the PowerShell loader.

    The readable template is compressed with GZip before Base64 transport so
    HID types only a small bootstrap stage. The C# payload still travels over
    CDC; compression is transport, not a second editable implementation.
    """
    return _windows_lines(
        loader_source(link_id, session_id, cdc_port, implementation="windows-dotnet")
    )


class BootstrapManager:
    def __init__(self, config: Config, link: SerialLink, hid: HidController,
                 gate: AgentGate | None = None) -> None:
        self.config = config
        self.link = link
        self.hid = hid
        self.gate = gate or AgentGate()
        self._state: dict[str, Any] = {"status": "not_started"}
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _set(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def _rejected(self, code: str, message: str) -> dict[str, Any]:
        """Argument error detected before any keystroke.

        It is not recorded in the state: a rejected call must not tear down or
        overwrite the status of a live agent that is still running.
        """
        return {
            "status": "error",
            "error": {"code": code, "message": message, "retryable": False},
        }

    def bootstrap(self, owner: str, layout: str | None = None,
                  platform: str | None = None, cdc_port: str | None = None, *,
                  layout_confirmed: bool = False) -> dict[str, Any]:
        selected_platform = platform if platform is not None else self.config.slave_platform
        # Platform and launcher validation happen before the bundle and before
        # any HID keystroke: a wrong selection must not type at the target.
        if selected_platform is None:
            return self._rejected(
                "slave_platform_required",
                "select the slave platform explicitly: macos, linux or windows",
            )
        if selected_platform not in PLATFORM_PROFILES:
            return self._rejected(
                "invalid_argument",
                f"unknown slave platform {selected_platform!r}: expected macos, linux or windows",
            )
        # The target's active keyboard layout is a precondition, not a default:
        # the layout configured for the rig can be wrong for the window that
        # has focus right now, and a mismatched HID map garbles every keystroke
        # of the loader. Configuration keeps its keyboard_layout for status
        # reporting, but bootstrap never falls back to it, and a fresh
        # verification on this attempt must be attested explicitly.
        if layout is None:
            return self._rejected(
                "slave_layout_required",
                "pass layout explicitly (es_ES or en_US) after verifying the "
                "active input source of the focused target window",
            )
        if layout not in {"es_ES", "en_US"}:
            return self._rejected(
                "invalid_argument",
                f"unsupported layout {layout!r}: expected es_ES or en_US",
            )
        if layout_confirmed is not True:
            return self._rejected(
                "slave_layout_unconfirmed",
                "verify the active input source of the focused target window on "
                "this attempt, then pass layout_confirmed=true",
            )
        selected_layout = layout
        profile = PLATFORM_PROFILES[selected_platform]
        selected_port = cdc_port if cdc_port is not None else self.config.slave_cdc_port
        if selected_port is not None:
            selected_port = str(selected_port).strip()
            if not selected_port:
                return self._rejected("invalid_argument", "slave_cdc_port cannot be empty")
            if selected_platform == "windows" and not _WINDOWS_PORT_RE.match(selected_port):
                return self._rejected(
                    "invalid_argument",
                    f"slave_cdc_port must match COM[1-9][0-9]* on windows, "
                    f"got {selected_port!r}",
                )

        # An explicitly selected platform with a built-in launcher must not
        # inherit a stale shortcut configured for another target OS. Generic
        # launcher overrides apply to the configured default platform and to
        # Linux, whose terminal shortcut has no portable default.
        configured_launcher = platform is None or profile.launcher_mods is None
        if configured_launcher:
            launcher_mods = self.config.terminal_mods
            launcher_key = self.config.terminal_key
            launcher_text = self.config.launcher_text
        else:
            launcher_mods = profile.launcher_mods
            launcher_key = profile.launcher_key
            launcher_text = profile.launcher_text
        if profile.launcher_mods is None:
            if launcher_mods is None or launcher_key is None:
                return self._rejected(
                    "slave_launcher_required",
                    "the linux slave needs terminal_mods and terminal_key to open a terminal",
                )
        else:
            if launcher_mods is None:
                launcher_mods = profile.launcher_mods
            if launcher_key is None:
                launcher_key = profile.launcher_key
        if launcher_text is None:
            launcher_text = profile.launcher_text

        # The payload comes from the immutable wheel artifact: no runtime build,
        # no agent-cache and no fallback to repository sources. A missing or
        # corrupt bundle fails before any keystroke instead of typing at nothing.
        self.hid.lease.require(owner)
        try:
            bundle = load_bundle(profile.implementation)
        except BundleError as exc:
            error = {"code": exc.code, "message": exc.message, "retryable": False}
            self.gate.set_ready(False)
            self._set(status="error", error=error)
            return {"status": "error", "error": error}
        payload = bundle.payload
        checksum = bundle.sha256
        session_id = secrets.token_hex(16)
        # Render once from the bundle already validated above; re-loading for
        # the command would give the loader a second chance to disagree with
        # the payload that is about to be transferred.
        rendered = _render_loader(
            bundle.loader_source, profile.implementation,
            self.link.link_id, session_id, selected_port,
        )
        stage_ready = threading.Event()
        loader_ready = threading.Event()
        loader_error: dict[str, Any] | None = None
        agent_hello = threading.Event()
        agent_body: dict[str, Any] = {}

        def on_bootstrap(frame: dict[str, Any]) -> None:
            nonlocal loader_error
            if frame.get("type") == "event" and frame.get("op") == "stage.ready":
                stage_ready.set()
                return
            if frame.get("session_id") != session_id:
                return
            if frame.get("type") == "event" and frame.get("op") == "loader.ready":
                stage_ready.set()
                loader_ready.set()
            elif frame.get("type") == "error":
                # The loader can fail before opening the CDC; without this the
                # failure would degrade into bootstrap_loader_timeout.
                loader_error = dict(frame.get("body") or {})
                stage_ready.set()
                loader_ready.set()

        def on_control(frame: dict[str, Any]) -> None:
            if frame.get("type") == "hello":
                agent_body.update(dict(frame.get("body") or {}))
                agent_hello.set()

        unsubscribe_bootstrap = self.link.subscribe("bootstrap", on_bootstrap)
        unsubscribe_control = self.link.subscribe("control", on_control)
        try:
            # A live agent from a previous run holds the CDC open; typing the
            # stub while the port is taken ends in a cryptic Open failure on
            # the target. Stop it first and wait for the port to be free.
            if self.gate.ready:
                self._set(status="stopping_previous_agent")
                try:
                    self.link.request("control", "agent.stop", {}, timeout=10)
                except TransportError as exc:
                    if exc.code not in {"request_timeout", "transport_disconnected"}:
                        raise
                    # The agent closing the CDC before replying is the
                    # expected exit path, not a failure.
                self.gate.set_ready(False)
                # The agent closes the CDC while exiting; when the request
                # above returns (or times out on the closed port) the COM
                # handle is already released, so the stub can reopen it.
            self._set(status="typing_loader", session_id=session_id, layout=selected_layout,
                      platform=selected_platform, implementation=profile.implementation)
            layout_result = self.hid.keyboard_layout(owner, selected_layout)
            if not layout_result.get("ok"):
                body = dict(layout_result.get("error") or {})
                raise TransportError(
                    str(body.get("code") or "keyboard_layout_failed"),
                    str(body.get("message") or "the HID layout could not be selected"),
                    bool(body.get("retryable", False)),
                    session_id=session_id,
                )
            blob = (
                windows_loader_blob(self.link.link_id, session_id, selected_port)
                if selected_platform == "windows"
                else posix_loader_blob(self.link.link_id, session_id, selected_port)
            )
            self.hid.key(owner, launcher_key, list(launcher_mods))
            time.sleep(
                _WINDOWS_LAUNCHER_READY_S if selected_platform == "windows" else 0.4
            )
            # macOS opens Spotlight and the app has to be typed; Windows opens
            # Start search; on Linux the configured shortcut opens the terminal
            # directly and the default text is empty.
            if launcher_text:
                self.hid.type_text(owner, launcher_text, _LAUNCHER_TYPE_DELAY_MS)
                if selected_platform == "windows":
                    time.sleep(_WINDOWS_SEARCH_SETTLE_S)
                self.hid.key(owner, "ENTER", [])
            time.sleep(
                _WINDOWS_CONSOLE_READY_S if selected_platform == "windows" else 0.8
            )
            # Reset Windows Console Host input before sending the loader. A
            # stale QuickEdit selection can consume Ctrl+C, so Escape must
            # leave selection mode first. Ctrl+C is harmless at a clean
            # PowerShell prompt and aborts any incomplete command otherwise.
            def on_stage_progress(
                staged_chars: int, total_chars: int, chunk_idx: int, chunks_total: int,
            ) -> None:
                self._set(
                    status="staging_loader",
                    stage_chars_sent=staged_chars,
                    stage_chars_total=total_chars,
                    stage_chunks_acked=chunk_idx,
                    stage_chunks_total=chunks_total,
                )

            if selected_platform == "windows":
                self.hid.key(owner, "ESC", [])
                self.hid.key(owner, "C", ["CTRL"])
                time.sleep(0.2)
                stub = windows_stub_command(self.link.link_id, selected_port)
                for position in range(0, len(stub), _HID_CHUNK_CHARS):
                    self.hid.type_text(
                        owner, stub[position:position + _HID_CHUNK_CHARS],
                        _WINDOWS_TYPE_DELAY_MS,
                    )
                self.hid.key(owner, "ENTER", [])
                self._set(status="waiting_stage")
                if not stage_ready.wait(profile.loader_wait_s) and not loader_ready.is_set():
                    raise TransportError(
                        "bootstrap_loader_timeout",
                        "the stub has not opened the CDC",
                        True,
                        session_id=session_id,
                    )
                if loader_error is None:
                    staged = push_windows_loader(
                        self.link, blob, on_progress=on_stage_progress,
                    )
                    self._set(status="loader_staged", staged=staged)
            else:
                # POSIX mirrors Windows: only the microloader stub is typed.
                # The loader arrives over the stateless SerialMux with
                # per-chunk flow control, so the CDC is opened once.
                stub = posix_stub_command(self.link.link_id, selected_port)
                for position in range(0, len(stub), _HID_CHUNK_CHARS):
                    self.hid.type_text(
                        owner, stub[position:position + _HID_CHUNK_CHARS],
                        _PYTHON_TYPE_DELAY_MS,
                    )
                self.hid.key(owner, "ENTER", [])
                self._set(status="waiting_stage")
                if not stage_ready.wait(profile.loader_wait_s) and not loader_ready.is_set():
                    raise TransportError(
                        "bootstrap_loader_timeout",
                        "the stub has not opened the CDC",
                        True,
                        session_id=session_id,
                    )
                if loader_error is None:
                    staged = push_loader(
                        self.link, blob, on_progress=on_stage_progress,
                    )
                    self._set(status="loader_staged", staged=staged)
            self._set(status="waiting_loader")
            loader_ready.wait(profile.loader_wait_s)
            if loader_error is not None:
                raise TransportError(
                    str(loader_error.get("code") or "loader_failed"),
                    str(loader_error.get("message") or "loader error"),
                    bool(loader_error.get("retryable")),
                    session_id=session_id,
                )
            if not loader_ready.is_set():
                raise TransportError(
                    "bootstrap_loader_timeout",
                    "the loader has not opened the CDC",
                    True,
                    session_id=session_id,
                )

            self._set(status="transferring", bytes_total=len(payload), bytes_sent=0)
            self.link.request("bootstrap", "begin", {"session_id": session_id, "size": len(payload), "sha256": checksum}, timeout=10)
            seq = 0
            for position in range(0, len(payload), 512):
                chunk = payload[position:position + 512]
                self.link.send_data("bootstrap", session_id, seq, chunk)
                seq += 1
                self._set(bytes_sent=position + len(chunk))
            commit = self.link.request("bootstrap", "commit", {"session_id": session_id}, timeout=10)
            if not agent_hello.wait(profile.hello_wait_s):
                raise TransportError(
                    "agent_hello_timeout",
                    f"the {profile.implementation} agent has not announced agent.hello",
                    True,
                    session_id=session_id,
                )
            ready = self.link.request("control", "bind", {"link_id": self.link.link_id}, timeout=10)
            body = dict(ready.get("body") or {})
            if body.get("status") != "ready":
                raise TransportError("agent_bind_failed", "the agent has not confirmed control.ready", True, session_id=session_id)
            result = {
                "status": "ready", "session_id": session_id, "size": len(payload),
                "sha256": checksum, "platform": selected_platform,
                "implementation": profile.implementation,
                "agent": agent_body, "commit": commit.get("body", {}),
            }
            self.gate.set_ready(True)
            self._set(**result)
            return result
        except BaseException as exc:
            if isinstance(exc, TransportError):
                error = exc.as_dict()
            else:
                error = {"code": "bootstrap_failed", "message": str(exc), "retryable": False}
            self.gate.set_ready(False)
            self._set(status="error", error=error)
            return {"status": "error", "error": error, "session_id": session_id}
        finally:
            unsubscribe_bootstrap()
            unsubscribe_control()

    def stop(self) -> dict[str, Any]:
        try:
            response = self.link.request("control", "agent.stop", {}, timeout=10)
            result = dict(response.get("body") or {})
        except TransportError as exc:
            if exc.code in {"request_timeout", "transport_disconnected"}:
                result = {"status": "stopped", "detail": "the agent closed the CDC before replying"}
            else:
                raise
        self.gate.set_ready(False)
        self._set(status="stopped")
        return result
