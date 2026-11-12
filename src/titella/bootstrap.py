"""Ephemeral bootstrap of a single slave agent over HID and CDC.

The slave platform is an explicit selection (argument, then configuration,
then error): inferring it from the master would launch a payload the target
cannot run. Each platform profile fixes the implementation, the launcher
defaults and the waits its payload needs before announcing itself.

The ESP32→slave leg is the ``slave_transport`` selection: ``cdc`` streams the
loader over the native CDC port the stub opens, and ``hid`` stages the same
compressed loader in the firmware Feature 0x11 mailbox and types only the
stage-0 microloader, so the target needs no COM port, clipboard or network.

The target's active keyboard layout is a precondition of every attempt, not a
default: the caller verifies the focused window's input source, passes the
layout explicitly and attests that fresh verification. A missing, unsupported
or unconfirmed layout is rejected before any keystroke, because a mismatched
HID map garbles the loader from its first character.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from titella_slave_assets.bundles import BundleError, load_bundle

from .agent_state import AgentGate, require_cdc_channel
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
# The clipboard sentinel replaces the CDC port: the stub never opens a serial
# device, so the loader receives it as its binding and skips port discovery.
CLIPBOARD_PORT = "CLIPBOARD"
# A clipboard round trip carries 2 KiB without effort, which stages the loader
# in a handful of exchanges instead of dozens over the CDC-sized chunks.
_CLIPBOARD_CHUNK_CHARS = 2048
# The typed part of a clipboard bootstrap is exactly this line. Everything else
# —the stub, the loader, the agent— arrives over the clipboard, so the keystroke
# budget stops being a constraint and a client that mangles brackets (XQuartz
# turns `[` into a backtick) cannot corrupt anything: the line has none.
CLIPBOARD_STAGE1 = "iex (Get-Clipboard -Raw)"
# The HID binding names the vendor data collection of the rig: exactly the
# twelve uppercase hexadecimal characters of the USB serial. It is an internal
# sentinel of the rendered loader, never a public COM port value.
_HID_BINDING_RE = re.compile(r"^HID:[0-9A-F]{12}$")
_HID_SERIAL_RE = re.compile(r"^[0-9A-F]{12}$")
_HID_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_HID_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# The firmware Feature mailbox holds at most this much compressed loader.
_HID_SNAPSHOT_MAX_BYTES = 32768
# Separator of the HID container: rendered loader, marker, transport unit.
# Stage-0 splits on the first occurrence and compiles the unit from a file,
# so no Base64 transport source is ever registered as script text.
_HID_CONTAINER_MARKER = "#@#"
# Longest stage-0 console line typed over HID. At 8 ms/character the budget
# costs ~26.6 s of pacing plus execution. The container split (loader part +
# Add-Type of the transport unit from a file) needed this headroom after the
# trims; validation semantics are never cut to stay under it.
_HID_STAGE0_MAX_CHARS = 3328
# The firmware identifies this rig with a fixed vendor/product pair; the HID
# bootstrap requires exactly it, so an unrelated 0xFF00 collection is refused.
_HID_USB_VID = 6127
_HID_USB_PID = 41871
_HID_TRANSPORT_PROTOCOL = 1
# Fresh health answers within this budget before any keystroke is typed; the
# FirmwareInfo cache deliberately does not serve this query.
_HID_HEALTH_TIMEOUT_S = 3.0
_STAGE_CHUNK_BYTES = 512
# Budget for an acknowledged stop to actually release its destination. The
# firmware answers route_error once the destination is really gone, so this is
# observable evidence, not a guessed sleep.
_STOP_TEARDOWN_S = 8.0


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
                   session_id: str, cdc_port: str | None, *,
                   hid_transport_source: str | None = None) -> str:
    """Substitutes the validated identifiers and the CDC override.

    The placeholder sits inside the template's own quotes, so only the literal
    *contents* are rendered: a Python repr (`None` or a quoted string) for the
    zipapp loader, and single-quote doubling for the PowerShell template.

    `HID:<serial>` is an internal sentinel of the port field (the same pattern
    as `CLIPBOARD`): it is never a public COM value. It requires the validated
    `HidByteStream` source — which is NOT embedded here: the transport unit
    travels behind the container marker of the HID snapshot (see
    `_hid_container`), so the stage-0 loader compiles it from a file instead
    of registering ~87 KB of Base64 script text. The placeholder is always
    rendered empty; the coupling (binding requires source, source requires
    binding) is still enforced here in both directions.
    """
    _check_identifier("link_id", link_id)
    _check_identifier("session_id", session_id)
    port = str(cdc_port).strip() if cdc_port is not None else ""
    if port.startswith("HID:"):
        if implementation != "windows-dotnet":
            raise ValueError("the HID binding requires the windows-dotnet loader")
        if not _HID_BINDING_RE.match(port):
            raise ValueError(
                "the HID binding must be HID:<12 uppercase hexadecimal characters>"
            )
        if hid_transport_source is None:
            raise ValueError("the HID binding requires hid_transport_source")
    elif hid_transport_source is not None:
        raise ValueError("hid_transport_source requires the HID:<serial> binding")
    if hid_transport_source is not None:
        if not isinstance(hid_transport_source, str) or not hid_transport_source.strip():
            raise ValueError("hid_transport_source must be a non-empty string")
        if bundle_source.count(_HID_CONTAINER_MARKER) != 0:
            raise ValueError("the loader template must not carry the container marker")
    if bundle_source.count("__HID_TRANSPORT_SOURCE_B64__") != 1 and "HID" in port:
        raise ValueError("the loader template must carry exactly one HID transport placeholder")
    if implementation == "python":
        port_literal = repr(port) if port else "None"
    else:
        port_literal = port.replace("'", "''")
    return (
        bundle_source
        .replace("__LINK_ID__", link_id)
        .replace("__SESSION_ID__", session_id)
        .replace("__CDC_PORT__", port_literal)
        .replace("__HID_TRANSPORT_SOURCE_B64__", "")
    )


def _hid_container(rendered_loader: str, hid_transport_source: str) -> str:
    """The single blob of the HID snapshot: loader, marker, transport unit.

    The container is one raw UTF-8 string — ``#@#`` never carries Base64 —
    gzip-compressed once by the caller. The stage-0 loader splits it on the
    marker: the part before is the loader script (with an empty transport
    source), the part after is the ``HidByteStream`` unit compiled from a
    temporary file. The marker must appear exactly once in the container, so
    it may appear in neither half.
    """
    if rendered_loader.count(_HID_CONTAINER_MARKER) != 0:
        raise ValueError("the rendered loader must not carry the container marker")
    if hid_transport_source.count(_HID_CONTAINER_MARKER) != 0:
        raise ValueError("the transport unit must not carry the container marker")
    if not isinstance(hid_transport_source, str) or not hid_transport_source.strip():
        raise ValueError("hid_transport_source must be a non-empty string")
    return (
        rendered_loader + "\n" + _HID_CONTAINER_MARKER + "\n" + hid_transport_source
    )


def loader_source(link_id: str, session_id: str, cdc_port: str | None = None,
                  implementation: str = "python", *,
                  hid_transport_source: str | None = None) -> str:
    """Renders the loader shipped inside the slave-assets wheel."""
    _check_identifier("link_id", link_id)
    _check_identifier("session_id", session_id)
    bundle = load_bundle(implementation)
    return _render_loader(bundle.loader_source, implementation, link_id, session_id, cdc_port,
                          hid_transport_source=hid_transport_source)


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
        select = "'" + port + "'"
    else:
        # An explicit COM port above always wins. Without one, only the CDC
        # of this rig is accepted: its identity is the fixed vendor/product
        # pair (17EF:A38F), it must be present, and exactly one candidate is
        # usable. Zero is cdc_not_found and several is cdc_ambiguous, both
        # raised here instead of opening an unrelated serial device.
        select = (
            "$($ports=@(Get-PnpDevice -PresentOnly -Class Ports|"
            "?{$_.InstanceId -match 'VID_17EF&PID_A38F' -and "
            "$_.FriendlyName -match '\\(COM[1-9][0-9]*\\)'}|"
            "%{[regex]::Match($_.FriendlyName,'COM[1-9][0-9]*').Value}|"
            "Sort-Object -Unique);"
            "if(-not $ports){throw 'cdc_not_found'};"
            "if(@($ports).Count-gt 1){throw 'cdc_ambiguous'};"
            "@($ports)[0])"
        )
    return (
        "$p=$null;try{try{Set-PSReadLineOption -HistorySaveStyle SaveNothing}catch{};"
        "$p=[IO.Ports.SerialPort]::new(" + select + ",2000000,[IO.Ports.Parity]::None,8,[IO.Ports.StopBits]::One);"
        "$p.DtrEnable=$p.RtsEnable=1;$p.ReadTimeout=2e4;$p.Open();"
        "$p.DiscardInBuffer();$script:Sp=$p;"
        '$h=\'{"v":1,"channel":"bootstrap","type":"\';'
        '$p.WriteLine($h+\'event","link_id":"' + link_id + '","op":"stage.ready"}\');'
        "$b='';$t=1;while($b.Length-lt$t){$f=$p.ReadLine()|ConvertFrom-Json;if($f.op-eq'loader.push'){"
        "$t=$f.body.total;$b+=$f.body.data_b64;[Console]::Write('.');"
        '$p.WriteLine($h+\'response","link_id":"\'+$f.link_id+\'","request_id":\'+$f.request_id+\',"op":"loader.push","body":{"staged":\'+$b.Length+\'}}\')}};'
        "$r=[IO.StreamReader][IO.Compression.GzipStream]::new("
        "[IO.MemoryStream][Convert]::FromBase64String($b),"
        "[IO.Compression.CompressionMode]::Decompress);"
        "iex $r.ReadToEnd()"
        "}catch{if($p){try{$p.Dispose()}catch{}};$script:Sp=$null;throw}"
    )


def windows_clipboard_stub_command(link_id: str) -> str:
    """One line that stages the loader over the shared clipboard and runs it.

    The window transport has no CDC device, so the stub speaks the clipboard
    protocol instead: it announces `stage.ready` on `TITELLA_S2M`, reads the
    `TITELLA_M2S` packets of the master, accumulates every `loader.push` chunk
    (2 KiB each) and answers each one with a `bootstrap` `response` frame that
    carries the running total. A packet the master re-writes because its
    acknowledgement was lost is answered again without appending its bytes
    twice, so a slow clipboard sync cannot corrupt the loader.

    The decompressed loader runs in the same console scope with
    `$script:Pin = 'CLIPBOARD'` and the packet counters already initialised,
    so the agent continues the dialogue the stub started. The line stays pure
    ASCII: it is typed over HID with the target's Spanish layout active.
    """
    _check_identifier("link_id", link_id)
    return (
        "$L='" + link_id + "';$s='TITELLA_S2M:'+$L;"
        '$q=\'{"v":1,"channel":"bootstrap","type":"\';'
        "$m=0;$k=1;"
        "Set-Clipboard ($s+':1:0'+\"`n\"+$q+"
        '\'event","link_id":"\'+$L+\'","op":"stage.ready"}\');'
        "$b='';$t=1;"
        "while($b.Length-lt$t){"
        # The clipboard rewrites every LF as CRLF; without this the index, the
        # identifiers and the JSON body all carry a trailing carriage return and
        # no packet ever parses.
        "$c=(Get-Clipboard -Raw)-replace\"`r`n\",\"`n\";$w++;"
        "if($c-match('(?s)TITELLA_M2S:'+$L+':(\\d+):.*\"data_b64\"')){"
        '$n=[int]$Matches[1];$i=$c.IndexOf("`n");'
        '$f=($c.Substring($i+1)-split"`n")[0]|ConvertFrom-Json;'
        "if($n-gt$m){$m=$n;$t=$f.body.total;$b+=$f.body.data_b64;$k++;"
        # Half-duplex: the slave writes only when it has just consumed a new
        # master packet. Rewriting on a timer would overwrite a master packet
        # that arrived but has not been matched yet, and the shared clipboard
        # has no second slot to keep it in.
        "Set-Clipboard ($s+':'+$k+':'+$n+\"`n\"+$q+"
        '\'response","link_id":"\'+$L+\'","request_id":\'+$f.request_id+'
        '\',"op":"loader.push","body":{"staged":\'+$b.Length+\'}}\')}}'
        "sleep -m 25};"
        "$script:L=$L;$script:Pin='CLIPBOARD';"
        "$script:M2S=$m;$script:S2M=$k;"
        "$r=[IO.StreamReader][IO.Compression.GzipStream]::new("
        "[IO.MemoryStream][Convert]::FromBase64String($b),"
        "[IO.Compression.CompressionMode]0);"
        "iex $r.ReadToEnd()"
    )


def windows_hid_stage0_command(stage0_source: str, token: str, size: int,
                               sha256: str) -> str:
    """One console line that runs the USB-only stage-0 microloader.

    This line is the ONLY text a HID bootstrap types. It embeds the rendered
    stage-0 template gzip+base64 and executes it in the current console: no
    child process, no clipboard, no CDC, no network, no staged file. The
    stage-0 loader pulls the #@# container (rendered loader, marker,
    transport unit) out of the firmware Feature 0x11 snapshot, validates the
    declared token, size and SHA-256, compiles the transport unit from a
    temporary file and only then runs the loader part.

    Exactly the three values are substituted; the whole serialized line stays
    ASCII and free of the `es_ES` dead keys (backtick, circumflex, tilde), and
    the bootstrap flow enforces the 3328-character budget (26.6 s of pacing)
    before publishing the snapshot or typing.
    """
    if not isinstance(stage0_source, str) or not stage0_source:
        raise ValueError("stage0_source must be a non-empty string")
    if not isinstance(token, str) or not _HID_TOKEN_RE.match(token):
        raise ValueError("token must be 16 hexadecimal characters")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= _HID_SNAPSHOT_MAX_BYTES:
        raise ValueError("size must be an integer in 1..32768")
    if not isinstance(sha256, str) or not _HID_SHA256_RE.match(sha256):
        raise ValueError("sha256 must be 64 hexadecimal characters")
    for placeholder in ("__HID_TOKEN__", "__HID_SIZE__", "__HID_SHA256__"):
        if placeholder not in stage0_source:
            raise ValueError(f"the stage-0 template lacks the {placeholder} placeholder")
    rendered = (
        stage0_source
        .replace("__HID_TOKEN__", token.upper())
        .replace("__HID_SIZE__", str(size))
        .replace("__HID_SHA256__", sha256.lower())
    )
    encoded = base64.b64encode(gzip.compress(rendered.encode("utf-8"), mtime=0)).decode("ascii")
    line = (
        "$b='" + encoded + "';"
        "$m=[IO.MemoryStream]::new([Convert]::FromBase64String($b));"
        "$r=[IO.StreamReader]::new([IO.Compression.GzipStream]::new($m,([IO.Compression.CompressionMode]0)));"
        "iex $r.ReadToEnd()"
    )
    if not line.isascii():
        raise ValueError("the stage-0 line must be ASCII to survive the HID layout")
    for banned in ("`", "^", "~", "\n", "\r"):
        if banned in line:
            raise ValueError(f"the stage-0 line must be typable on es_ES, found {banned!r}")
    return line


def windows_loader_blob(link_id: str, session_id: str,
                        cdc_port: str | None = None, *,
                        hid_transport_source: str | None = None) -> str:
    """The gzip+base64 loader streamed to the Windows stub over the CDC.

    The HID snapshot is NOT these bytes: it is the `_hid_container` blob
    (loader, marker, transport unit) compressed by the bootstrap flow, so
    passing a HID binding here is rejected by `_render_loader`'s coupling
    unless a source is supplied — and even then the unit is never embedded
    in the loader anymore."""
    rendered = loader_source(link_id, session_id, cdc_port,
                             implementation="windows-dotnet",
                             hid_transport_source=hid_transport_source)
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
                      cdc_port: str | None = None, *,
                      hid_transport_source: str | None = None) -> str:
    """The gzip+base64 Python loader streamed to the POSIX stub over the CDC."""
    rendered = loader_source(link_id, session_id, cdc_port, implementation="python",
                             hid_transport_source=hid_transport_source)
    return base64.b64encode(gzip.compress(rendered.encode("utf-8"), mtime=0)).decode("ascii")


def loader_command(link_id: str, session_id: str, cdc_port: str | None = None) -> str:
    """One-line POSIX command that runs the rendered Python loader directly."""
    return _python_command(loader_source(link_id, session_id, cdc_port))


def windows_loader_lines(link_id: str, session_id: str,
                         cdc_port: str | None = None, *,
                         hid_transport_source: str | None = None) -> list[str]:
    """Console lines that accumulate and execute the PowerShell loader.

    The readable template is compressed with GZip before Base64 transport so
    HID types only a small bootstrap stage. The C# payload still travels over
    CDC; compression is transport, not a second editable implementation.
    """
    return _windows_lines(
        loader_source(link_id, session_id, cdc_port, implementation="windows-dotnet",
                      hid_transport_source=hid_transport_source)
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
                  layout_confirmed: bool = False, skip_launch: bool = False) -> dict[str, Any]:
        # A transport whose agent_protocol is "none" or "visual" has no channel
        # for a loader, so a bootstrap could only type the payload into the
        # window and then fail to load it. Refuse before the first keystroke
        # instead; the clipboard protocol passes this gate.
        try:
            require_cdc_channel(self.link)
        except TransportError as exc:
            return self._rejected(exc.code, exc.message)
        agent_protocol = getattr(self.link, "agent_protocol", "serial")
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
        # The clipboard channel has no serial device at all: only the Windows
        # stub speaks it (the POSIX stubs open a CDC port), and a configured or
        # explicit COM port contradicts it instead of silently degrading.
        if agent_protocol == "clipboard":
            if selected_platform != "windows":
                return self._rejected(
                    "invalid_argument",
                    "agent_protocol='clipboard' bootstraps the windows stub; "
                    f"select platform='windows', got {selected_platform!r}",
                )
            if cdc_port is not None or self.config.slave_cdc_port:
                return self._rejected(
                    "invalid_argument",
                    "cdc_port and slave_cdc_port are not used with agent_protocol='clipboard'",
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
        # The ESP32→slave leg is a configuration selection, never an argument:
        # the tool keeps its public signature and the operator switches it with
        # titella_upsert. HID carries the whole loader through the firmware
        # Feature mailbox, so it needs the serial agent channel, a Windows
        # target and an operator-prepared console; a COM pin contradicts it and
        # a fresh launcher would open a terminal in the wrong session.
        slave_transport = self.config.slave_transport
        hid_binding: str | None = None
        if slave_transport == "hid":
            if selected_platform != "windows":
                return self._rejected(
                    "invalid_argument",
                    "slave_transport='hid' bootstraps the windows HID loader; "
                    f"select platform='windows', got {selected_platform!r}",
                )
            if agent_protocol != "serial":
                return self._rejected(
                    "invalid_argument",
                    "slave_transport='hid' needs the serial agent channel; "
                    f"agent_protocol is {agent_protocol!r}",
                )
            if cdc_port is not None or self.config.slave_cdc_port:
                return self._rejected(
                    "invalid_argument",
                    "cdc_port and slave_cdc_port are not used with slave_transport='hid'",
                )
            if skip_launch is not True:
                return self._rejected(
                    "invalid_argument",
                    "slave_transport='hid' requires skip_launch=true and an "
                    "operator-prepared Windows console: the HID bootstrap types "
                    "stage-0 into the focused console and launches nothing",
                )
        if agent_protocol == "clipboard":
            # The clipboard sentinel reaches the loader as its binding, so the
            # loader never probes a port on the slave.
            selected_port: str | None = CLIPBOARD_PORT
        elif slave_transport == "hid":
            # Filled from the fresh health below: the binding must name the USB
            # serial the firmware actually reports, never a configured COM.
            selected_port = None
        else:
            selected_port = cdc_port if cdc_port is not None else self.config.slave_cdc_port
        if selected_port is not None:
            selected_port = str(selected_port).strip()
            if not selected_port:
                return self._rejected("invalid_argument", "slave_cdc_port cannot be empty")
            if (
                selected_platform == "windows"
                and agent_protocol != "clipboard"
                and not _WINDOWS_PORT_RE.match(selected_port)
            ):
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
            # A new attempt starts here: the previous attempt's binding is not
            # this one's identity, so it is cleared instead of surviving the
            # status merge.
            self._set(status="error", error=error, slave_transport=slave_transport,
                      hid_binding=None)
            return {"status": "error", "error": error,
                    "slave_transport": slave_transport}
        payload = bundle.payload
        checksum = bundle.sha256
        # The HID leg needs the live firmware identity before anything else:
        # the binding is the serial the firmware reports, and an old firmware
        # without the HID data protocol would only swallow the staged loader.
        # A fresh query, not the five-second status cache, so a reflashed chip
        # is never mistaken for the previous one on this attempt.
        if slave_transport == "hid":
            health = self._fresh_health()
            unsupported = self._hid_health_rejection(health)
            if unsupported is not None:
                self.gate.set_ready(False)
                self._set(status="error", error=unsupported,
                          slave_transport=slave_transport, hid_binding=None)
                return {"status": "error", "error": unsupported,
                        "slave_transport": slave_transport}
            hid_binding = f"HID:{health['usb_serial']}"
            if not _HID_BINDING_RE.match(hid_binding):
                message = (
                    "the firmware reported an unusable USB serial "
                    f"{health['usb_serial']!r}: the HID binding needs 12 uppercase "
                    "hexadecimal characters"
                )
                error = {"code": "slave_transport_unsupported", "message": message,
                         "retryable": False}
                self.gate.set_ready(False)
                self._set(status="error", error=error, slave_transport=slave_transport,
                          hid_binding=None)
                return {"status": "error", "error": error,
                        "slave_transport": slave_transport}
            # The binding is this attempt's identity now: record it before any
            # later status write so a failure still names the device it used.
            self._set(hid_binding=hid_binding)
            selected_port = hid_binding
        session_id = secrets.token_hex(16)
        # Render once from the bundle already validated above; re-loading for
        # the command would give the loader a second chance to disagree with
        # the payload that is about to be transferred. The HID binding also
        # embeds the validated transport unit, which travels inside the same
        # rendered bytes whose hash is committed below.
        rendered = _render_loader(
            bundle.loader_source, profile.implementation,
            self.link.link_id, session_id, selected_port,
            hid_transport_source=(
                bundle.hid_transport_source if slave_transport == "hid" else None
            ),
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
        stage_token: str | None = None
        started = time.monotonic()
        try:
            # A live agent from a previous run holds the CDC open (or the HID
            # stream and its mutex); typing the stub while the port is taken
            # ends in a cryptic Open failure on the target, and a second HID
            # consumer answers hid_busy. Stop it first and wait for its
            # transport to be torn down.
            #
            # The gate is NOT authoritative here: it closes on any missed
            # heartbeat or reconnecting state, while the agent process may
            # still be alive holding the port. Gating the stop on it left a
            # stale agent owning the CDC for ever, and every later bootstrap
            # died as bootstrap_loader_timeout. The stop is therefore
            # unconditional and its failure to answer is tolerated: an absent
            # agent answers route_error, and one already exiting closes the
            # CDC before replying.
            self._set(status="stopping_previous_agent")
            try:
                self.link.request("control", "agent.stop", {}, timeout=10)
            except TransportError as exc:
                if exc.code in {"request_timeout", "transport_disconnected"}:
                    # The agent closing the CDC before replying is the
                    # expected exit path, not a failure.
                    pass
                elif exc.code in {"route_error", "agent_unreachable", "unknown_op"}:
                    # No agent is running: nothing holds the port.
                    pass
                else:
                    raise
            # A reply — or its absence — never proves the stream is free: a
            # timed-out stop can leave a running agent attached, and the router
            # also answers route_error when its output queue is full. The close
            # is proven for the destination the firmware has selected now,
            # before any selection change or typed byte.
            self._await_agent_gone()
            self.gate.set_ready(False)
            # The destination is chosen explicitly before any typed byte. The
            # old firmware knows only the CDC, so an unknown_op selection is
            # tolerated there: its default destination already is the CDC. HID
            # never takes that path — the fresh health above already proved the
            # protocol, and a refused selection aborts before typing. Only the
            # serial transport reaches the firmware router; a window instance
            # has no such operation.
            if getattr(self.link, "transport", "serial") == "serial":
                destination = "hid" if slave_transport == "hid" else "cdc"
                try:
                    self.link.request("hid", "slave_transport",
                                      {"transport": destination}, timeout=10)
                except TransportError as exc:
                    if slave_transport == "hid":
                        raise
                    if exc.code not in {"unknown_op", "unsupported_hid_op"}:
                        raise
            self._set(status="typing_loader", session_id=session_id, layout=selected_layout,
                      platform=selected_platform, implementation=profile.implementation,
                      slave_transport=slave_transport,
                      hid_binding=hid_binding)
            layout_result = self.hid.keyboard_layout(owner, selected_layout)
            if not layout_result.get("ok"):
                body = dict(layout_result.get("error") or {})
                raise TransportError(
                    str(body.get("code") or "keyboard_layout_failed"),
                    str(body.get("message") or "the HID layout could not be selected"),
                    bool(body.get("retryable", False)),
                    session_id=session_id,
                )
            # The loader blob is derived from the already validated render, not
            # from a second render: the hash committed to the loader must cover
            # exactly the bytes that travel. The HID snapshot is the container
            # (loader with an empty transport source, marker, transport unit),
            # so its hash covers the unit without embedding it as Base64.
            if slave_transport == "hid":
                staged_source = _hid_container(
                    rendered, str(bundle.hid_transport_source)
                )
            else:
                staged_source = rendered
            compressed = gzip.compress(staged_source.encode("utf-8"), mtime=0)
            blob = base64.b64encode(compressed).decode("ascii")
            stage0_command: str | None = None
            if slave_transport == "hid":
                # Everything the stage-0 line and the snapshot need is settled
                # before the slot is published or a key is typed: an oversized
                # payload must fail as a named error, never as a timeout after
                # publishing a half-usable snapshot.
                if len(compressed) > _HID_SNAPSHOT_MAX_BYTES:
                    raise TransportError(
                        "hid_loader_too_large",
                        f"the compressed loader is {len(compressed)} bytes; the "
                        f"firmware snapshot holds at most {_HID_SNAPSHOT_MAX_BYTES}",
                        False,
                        session_id=session_id,
                    )
                stage0_source = bundle.hid_stage0_source
                if not stage0_source:
                    raise TransportError(
                        "slave_transport_unsupported",
                        "the windows bundle carries no stage-0 microloader for the HID transport",
                        False,
                        session_id=session_id,
                    )
                stage_token = secrets.token_hex(8)
                stage0_command = windows_hid_stage0_command(
                    stage0_source, stage_token, len(compressed),
                    hashlib.sha256(compressed).hexdigest(),
                )
                if len(stage0_command) > _HID_STAGE0_MAX_CHARS:
                    raise TransportError(
                        "hid_stage0_too_large",
                        f"the typed stage-0 line is {len(stage0_command)} characters; "
                        f"the keystroke budget is {_HID_STAGE0_MAX_CHARS}",
                        False,
                        session_id=session_id,
                    )
            # The clipboard protocol with an operator-provided console launches
            # nothing: typing the Start-menu shortcut would open a terminal in
            # the launcher session instead of the console the operator prepared.
            # The same holds for any bootstrap with skip_launch=true.
            console_ready = not skip_launch
            if console_ready:
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

            if slave_transport == "hid":
                # Events are already subscribed: publish the snapshot over the
                # UART first, and only then type the single stage-0 line. The
                # console is the one the operator prepared, so nothing is
                # launched or focused here.
                stage_started = time.monotonic()
                self._stage_hid_snapshot(stage_token, compressed)
                self._set(status="stage0_typed",
                          stage_elapsed_s=round(time.monotonic() - stage_started, 3))
                for position in range(0, len(stage0_command), _HID_CHUNK_CHARS):
                    self.hid.type_text(
                        owner, stage0_command[position:position + _HID_CHUNK_CHARS],
                        _WINDOWS_TYPE_DELAY_MS,
                    )
                self.hid.key(owner, "ENTER", [])
                self._set(status="waiting_loader")
                with self._clipboard_hold():
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
                        "the stage-0 microloader did not run the loader: check that the "
                        f"prepared console received the typed line on the {hid_binding} "
                        "HID binding, that its active keyboard layout matches, and that "
                        "PowerShell 5.1 with FullLanguage is allowed",
                        True,
                        session_id=session_id,
                    )
            elif selected_platform == "windows":
                # The clipboard protocol types only the 24-character stage-1
                # launcher: the stub itself travels as clipboard text, so it
                # arrives intact regardless of the client's key handling and the
                # keystroke budget stops being a constraint.
                if agent_protocol == "clipboard":
                    self._write_clipboard_stage1(owner)
                    stub = CLIPBOARD_STAGE1
                else:
                    if not skip_launch:
                        self._focus_remote_console(owner)
                    stub = windows_stub_command(self.link.link_id, selected_port)
                for position in range(0, len(stub), _HID_CHUNK_CHARS):
                    self.hid.type_text(
                        owner, stub[position:position + _HID_CHUNK_CHARS],
                        _WINDOWS_TYPE_DELAY_MS,
                    )
                self.hid.key(owner, "ENTER", [])
                self._set(status="waiting_stage")
                with self._clipboard_hold():
                    if not stage_ready.wait(profile.loader_wait_s) and not loader_ready.is_set():
                        raise TransportError(
                            "bootstrap_loader_timeout",
                            (
                                "the stub has not announced stage.ready over the clipboard:"
                                " check that the target console runs the typed stub, that the"
                                " active keyboard layout matches, and that clipboard sharing"
                                " between master and slave is enabled"
                                if agent_protocol == "clipboard"
                                else
                                "the stub has not opened the CDC"
                                f" {selected_port or 'of this rig'}: the port is held by another"
                                " process (usually a previous agent still running) or the COM"
                                " number is wrong"
                            ),
                            True,
                            session_id=session_id,
                        )
                if loader_error is None:
                    staged = push_windows_loader(
                        self.link, blob,
                        chunk_chars=(
                            _CLIPBOARD_CHUNK_CHARS if agent_protocol == "clipboard" else 512
                        ),
                        on_progress=on_stage_progress,
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
                with self._clipboard_hold():
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
            if slave_transport != "hid":
                self._set(status="waiting_loader")
                with self._clipboard_hold():
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
            with self._clipboard_hold():
                announced = agent_hello.wait(profile.hello_wait_s)
            if not announced:
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
                "slave_transport": slave_transport,
                # Null on the CDC/clipboard legs: the status merge must not
                # keep a binding left by a previous HID attempt.
                "hid_binding": hid_binding,
                "agent": agent_body, "commit": commit.get("body", {}),
                "bootstrap_elapsed_s": round(time.monotonic() - started, 3),
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
            # The failed attempt still names the transport it used; a HID
            # attempt whose binding was proven reports it, and a CDC attempt
            # clears any binding a previous HID run left in the merged status.
            self._set(status="error", error=error, slave_transport=slave_transport,
                      hid_binding=hid_binding)
            failure: dict[str, Any] = {
                "status": "error", "error": error, "session_id": session_id,
                "slave_transport": slave_transport,
            }
            if hid_binding is not None:
                failure["hid_binding"] = hid_binding
            return failure
        finally:
            if stage_token is not None:
                # The snapshot slot belongs to this attempt: invalidate it even
                # when the run failed, so a later token never reads stale
                # bytes. A clear error never replaces the original outcome.
                try:
                    self.link.request("hid", "loader_stage_clear",
                                      {"token": stage_token}, timeout=10)
                except BaseException:
                    pass
            unsubscribe_bootstrap()
            unsubscribe_control()

    def _fresh_health(self) -> dict[str, Any]:
        """Queries hid.health without the FirmwareInfo cache.

        The status cache is five seconds old by design; a bootstrap that just
        reflashed the chip, or that runs right after another attempt, must not
        decide on the previous chip's capabilities. Nothing is typed when this
        query fails: the transport cannot be proven.
        """
        try:
            frame = self.link.request("hid", "health", {}, timeout=_HID_HEALTH_TIMEOUT_S)
        except TransportError as exc:
            return {"reachable": False,
                    "error": {"code": exc.code, "message": exc.message}}
        health = dict(frame.get("body") or {})
        health["reachable"] = True
        return health

    @staticmethod
    def _hid_health_rejection(health: dict[str, Any]) -> dict[str, Any] | None:
        """Rejects an old or mismatched firmware before the HID bootstrap types.

        The HID leg needs the firmware data protocol, the HID capability, the
        project's USB identity and the twelve-hexadecimal serial the master
        uses as its binding. An older firmware answers without those fields;
        there is no fallback, because the CDC path was not selected.
        """
        def reject(message: str) -> dict[str, Any]:
            return {"code": "slave_transport_unsupported", "message": message,
                    "retryable": False}

        if not health.get("reachable"):
            error = dict(health.get("error") or {})
            return reject(
                "hid.health did not answer "
                f"({error.get('code') or 'unreachable'}): the firmware identity "
                "and the HID capability cannot be proven"
            )
        if health.get("hid_data_protocol") != _HID_TRANSPORT_PROTOCOL:
            return reject(
                "the firmware does not carry the HID data protocol "
                f"({health.get('hid_data_protocol')!r}); reflash 1.6.0 or newer"
            )
        transports = health.get("slave_transports")
        if not isinstance(transports, list) or "hid" not in transports:
            return reject(
                "the firmware does not advertise the HID slave transport "
                f"({transports!r}); reflash 1.6.0 or newer"
            )
        if (
            health.get("usb_vid") != _HID_USB_VID
            or health.get("usb_pid") != _HID_USB_PID
        ):
            return reject(
                "the firmware reports USB identity "
                f"{health.get('usb_vid')}:{health.get('usb_pid')}, expected "
                f"{_HID_USB_VID}:{_HID_USB_PID}"
            )
        serial = health.get("usb_serial")
        if not isinstance(serial, str) or not _HID_SERIAL_RE.match(serial):
            return reject(
                f"the firmware reports an unusable USB serial {serial!r}: the HID "
                "binding needs 12 uppercase hexadecimal characters"
            )
        return None

    def _stage_hid_snapshot(self, token: str, compressed: bytes) -> None:
        """Stages the compressed loader in the firmware Feature 0x11 mailbox.

        The three operations are the firmware's own staging protocol: begin
        reserves the slot for this link and token, chunk copies a contiguous
        block, and commit publishes the snapshot only when the size and digest
        it was opened with match exactly. Progress is reported per block so the
        caller can tell a slow transfer from a stuck one.
        """
        size = len(compressed)
        digest = hashlib.sha256(compressed).hexdigest()
        self.link.request(
            "hid", "loader_stage_begin",
            {"token": token, "size": size, "sha256": digest}, timeout=10,
        )
        for offset in range(0, size, _STAGE_CHUNK_BYTES):
            chunk = compressed[offset:offset + _STAGE_CHUNK_BYTES]
            self.link.request(
                "hid", "loader_stage_chunk",
                {"token": token, "offset": offset,
                 "data_b64": base64.b64encode(chunk).decode("ascii")},
                timeout=10,
            )
            self._set(status="staging_loader", stage_bytes_sent=offset + len(chunk),
                      stage_bytes_total=size)
        self.link.request("hid", "loader_stage_commit", {"token": token}, timeout=10)

    def _await_agent_gone(self, deadline_s: float | None = None) -> None:
        """Waits until the firmware observes the selected slave stream closed.

        A stop reply only proves the agent received the request; a timed-out or
        erroring reply proves nothing at all (the agent may still hold the
        stream, or the router may have refused the frame because its output
        queue was full). The only observable transport state is a fresh
        ``hid.health``: the firmware reports ``slave_connected`` for whichever
        destination it currently forwards to, and both legs are meaningful —
        the HID data endpoint opens with its epoch, and the core's CDC
        ``operator bool`` tracks ``DTR && RTS`` (``USBCDC::_onLineState``), so it
        is false once the host closes the line, not merely when the device is
        unplugged. ``slave_connected: false`` for a selected ``cdc`` or ``hid``
        destination proves the close; ``true`` means an owner is still
        attached; a missing, unreachable or otherwise unknown state proves
        nothing and must end as ``agent_stop_timeout``.

        This wait exists for a HID-configured instance, which may have to stop
        an agent that opened either leg (the firmware keeps forwarding to the
        CDC until the destination is switched). CDC- and window-configured
        instances keep their previous behaviour: their stubs retry their own
        open, and they have no such state to consult.
        """
        if self.config.slave_transport != "hid":
            return
        if getattr(self.link, "transport", "serial") != "serial":
            return
        budget = _STOP_TEARDOWN_S if deadline_s is None else deadline_s
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                frame = self.link.request("hid", "health", {}, timeout=2)
            except TransportError:
                # A failed query is not evidence either way; the deadline is.
                time.sleep(0.05)
                continue
            body = dict(frame.get("body") or {})
            if (
                body.get("slave_transport") in {"cdc", "hid"}
                and body.get("slave_connected") is False
            ):
                return
            time.sleep(0.05)
        raise TransportError(
            "agent_stop_timeout",
            "the slave transport is still attached, or its state could not be "
            "observed: hid.health did not report slave_connected=false for the "
            "selected cdc/hid destination, so the previous agent cannot be "
            "proven stopped",
            True,
        )

    def _write_clipboard_stage1(self, owner: str) -> None:
        """Publishes the full stub as clipboard text before stage-1 is typed.

        The typed line is only `iex (Get-Clipboard -Raw)`: a client that mangles
        brackets or commas (XQuartz delivers `[` as a backtick) still launches a
        correct stub, because the stub never passes through the keyboard.
        """
        write = getattr(self.link, "clipboard_write_text", None)
        if write is None:
            raise TransportError(
                "clipboard_unavailable",
                "the clipboard protocol needs a link that can write pasteboard text",
                False,
            )
        write(windows_clipboard_stub_command(self.link.link_id), transient=True)

    def _focus_remote_console(self, owner: str) -> None:
        """Gives the remote desktop a pointer click so it takes the keyboard.

        A remote-desktop client shows the guest's desktop, so a keystroke types
        into whichever window the guest has focused. One click in the middle of
        the client's content restores that focus; without it the stub is typed
        into nothing and the bootstrap waits out its timeout.

        The escapes leave Windows Console Host's QuickEdit selection mode, which
        would otherwise swallow the first characters of the stub, and Ctrl+C
        aborts any incomplete command left at the prompt.
        """
        self.hid.key(owner, "ESC", [])
        time.sleep(0.2)
        bounds = getattr(self.hid, "screen", None)
        dimensions = bounds.resolution() if bounds else None
        if dimensions:
            self.hid.mouse_abs(owner, dimensions[0] // 2, dimensions[1] // 2)
            self.hid.click(owner, "left", state="click")
            time.sleep(0.5)
        self.hid.key(owner, "ESC", [])
        self.hid.key(owner, "C", ["CTRL"])
        time.sleep(0.2)

    @contextmanager
    def _clipboard_hold(self):
        """Keeps the clipboard exchange polling across a silent wait.

        The stub announces its stages on its own, after the keystrokes that
        typed it and with no request in flight; the link only keeps reading
        the pasteboard while an operation or stream is declared to be open.
        A link without the clipboard channel ignores the hold.
        """
        hold = getattr(self.link, "clipboard_activity", None)
        if hold is None:
            yield
            return
        with hold():
            yield

    def stop(self) -> dict[str, Any]:
        result: dict[str, Any]
        try:
            response = self.link.request("control", "agent.stop", {}, timeout=10)
            result = dict(response.get("body") or {})
        except TransportError as exc:
            if exc.code in {"request_timeout", "transport_disconnected", "route_error"}:
                # The reply proves nothing: the agent may still be running, or
                # the router may have refused the frame for a full queue. The
                # close is proven below from the device state, never inferred
                # from this failure.
                result = {"status": "stopped",
                          "detail": "the stop was sent but not acknowledged"}
            else:
                raise
        # Whether or not the stop was acknowledged, the transport must be
        # observed closed before reporting success.
        self._await_agent_gone()
        # The agent is gone: the clipboard link must stop reading for it.
        detach = getattr(self.link, "clipboard_detach", None)
        if detach is not None:
            detach()
        self.gate.set_ready(False)
        self._set(status="stopped")
        return result
