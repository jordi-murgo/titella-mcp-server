"""Canonical Titella documentation served through MCP.

Two halves, one catalog: the four server documents travel inside the
``titella`` wheel under ``_docs/`` and the three slave documents travel inside
the pinned ``titella-slave-assets`` wheel under ``_data/docs/``. Every read
goes through ``importlib.resources``: no checkout path, URL, network request
or caller-supplied path is ever consulted, and an identifier outside the
closed catalog is never interpreted as a path.

The module keeps only stdlib and package imports at module scope so the
catalog and its error behavior can be exercised without the MCP SDK; the
FastMCP type is imported for typing only.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files as _resource_files
from typing import TYPE_CHECKING, Any

from titella_slave_assets import __version__ as _slave_assets_version

from . import __version__ as _server_version

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at run time
    from mcp.server.fastmcp import FastMCP

MAX_DOCUMENT_BYTES = 32 * 1024
MIME_TYPE = "text/markdown"
URI_PREFIX = "titella://docs/"

DOC_NOT_FOUND = "doc_not_found"
DOCS_UNAVAILABLE = "docs_unavailable"

SOURCE_TITELLA = "titella"
SOURCE_SLAVE_ASSETS = "titella-slave-assets"

SERVER_INSTRUCTIONS = (
    "Use docs_index to discover Titella procedures and docs_read to read them. "
    "Read getting-started before operating a rig and the platform-specific "
    "bootstrap guide before slave_bootstrap. Before each bootstrap, check the "
    "target window's active keyboard layout with screen, pass that layout "
    "explicitly and set layout_confirmed=true only if freshly verified; stop "
    "if it cannot be established. Documentation does not report live device "
    "state; use kvm_status for that."
)


class DocumentationError(RuntimeError):
    """A documentation request cannot be served; there is no fallback."""

    def __init__(self, code: str, message: str) -> None:
        RuntimeError.__init__(self, message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class _Document:
    """One catalog entry and where its bytes are packaged."""

    document_id: str
    title: str
    description: str
    source: str
    package: str
    directory: tuple[str, ...]
    filename: str

    @property
    def uri(self) -> str:
        return f"{URI_PREFIX}{self.document_id}"


_CATALOG: tuple[_Document, ...] = (
    _Document(
        document_id="bootstrap-python",
        title="Bootstrap the Python slave agent",
        description=(
            "Python 3.9+ on macOS and Linux: prerequisites, launcher and CDC "
            "permissions, loader handshake, footprint, cleanup and error codes."
        ),
        source=SOURCE_SLAVE_ASSETS,
        package="titella_slave_assets",
        directory=("_data", "docs"),
        filename="bootstrap-python.md",
    ),
    _Document(
        document_id="bootstrap-windows",
        title="Bootstrap the Windows slave agent",
        description=(
            "Windows 10 1809+/11 with PowerShell 5.1 and .NET Framework 4.7.2+: "
            "COM resolution, the USB-only HID stage-0 bootstrap, C# payload, "
            "ConPTY, job containment, timeouts and policy refusals."
        ),
        source=SOURCE_SLAVE_ASSETS,
        package="titella_slave_assets",
        directory=("_data", "docs"),
        filename="bootstrap-windows.md",
    ),
    _Document(
        document_id="getting-started",
        title="Titella getting started",
        description=(
            "Topology, prerequisites, explicit slave platform selection and the "
            "first session tool by tool."
        ),
        source=SOURCE_TITELLA,
        package="titella",
        directory=("_docs",),
        filename="getting-started.md",
    ),
    _Document(
        document_id="mcp-transport",
        title="Shared MCP transport",
        description=(
            "HTTP daemon and stdio wrappers, concurrent startup, states, "
            "recovery, documentation availability over both transports and "
            "rollback."
        ),
        source=SOURCE_TITELLA,
        package="titella",
        directory=("_docs",),
        filename="mcp-transport.md",
    ),
    _Document(
        document_id="operation",
        title="Operating a Titella rig",
        description=(
            "Screen capture, HID lease, bootstrap, shell, terminal, files, "
            "transfers and stop: inputs, observable results and errors."
        ),
        source=SOURCE_TITELLA,
        package="titella",
        directory=("_docs",),
        filename="operation.md",
    ),
    _Document(
        document_id="serial-protocol",
        title="Serial protocol v1",
        description=(
            "Canonical v1 contract: framing and limits, envelope, allowed "
            "directions, ACK and windows, heartbeat, bind and lifecycle, and the "
            "cdc/hid slave-transport selection with its Feature mailbox bootstrap."
        ),
        source=SOURCE_SLAVE_ASSETS,
        package="titella_slave_assets",
        directory=("_data", "docs"),
        filename="serial-protocol.md",
    ),
    _Document(
        document_id="troubleshooting",
        title="Titella troubleshooting",
        description=(
            "Gate not ready, lease owner, loader versus hello timeouts, POSIX "
            "CDC permissions, Windows COM and policies, reconnection rules."
        ),
        source=SOURCE_TITELLA,
        package="titella",
        directory=("_docs",),
        filename="troubleshooting.md",
    ),
)

_BY_ID: dict[str, _Document] = {entry.document_id: entry for entry in _CATALOG}
_CATALOG_IDS = tuple(entry.document_id for entry in _CATALOG)


def _version_for(source: str) -> str:
    return _server_version if source == SOURCE_TITELLA else _slave_assets_version


def _metadata(entry: _Document) -> dict[str, Any]:
    return {
        "id": entry.document_id,
        "title": entry.title,
        "description": entry.description,
        "uri": entry.uri,
        "source": entry.source,
        "version": _version_for(entry.source),
    }


def _read_text_resource(resource: Any, document_id: str) -> str:
    """Validates one packaged resource: present, nonempty, bounded, UTF-8.

    ``resource`` is an ``importlib.resources`` Traversable (a ``pathlib.Path``
    also satisfies the protocol), which keeps the validation testable against
    synthetic directories without touching the installed packages.
    """
    try:
        if not resource.is_file():
            raise DocumentationError(
                DOCS_UNAVAILABLE,
                f"document {document_id!r} is not packaged in the installed wheel",
            )
        payload = resource.read_bytes()
    except DocumentationError:
        raise
    except Exception as exc:  # unreadable resource: no truncation, no fallback
        raise DocumentationError(
            DOCS_UNAVAILABLE,
            f"document {document_id!r} cannot be read from the installed wheel: {exc}",
        ) from exc
    if not payload:
        raise DocumentationError(
            DOCS_UNAVAILABLE, f"document {document_id!r} is empty"
        )
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise DocumentationError(
            DOCS_UNAVAILABLE,
            f"document {document_id!r} is {len(payload)} bytes, above the "
            f"{MAX_DOCUMENT_BYTES}-byte limit",
        )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentationError(
            DOCS_UNAVAILABLE, f"document {document_id!r} is not valid UTF-8"
        ) from exc


def _resolve(entry: _Document) -> Any:
    """The packaged Traversable of one catalog entry, or ``docs_unavailable``."""
    try:
        root = _resource_files(entry.package)
    except Exception as exc:
        raise DocumentationError(
            DOCS_UNAVAILABLE,
            f"document {entry.document_id!r} needs the {entry.package} package, "
            f"which is not installed: {exc}",
        ) from exc
    for part in entry.directory:
        root = root.joinpath(part)
    return root.joinpath(entry.filename)


def list_documents() -> dict[str, Any]:
    """The discovery catalog: versions plus one brief entry per document."""
    documents = [_metadata(entry) for entry in sorted(_CATALOG, key=lambda item: item.document_id)]
    return {
        "server_version": _server_version,
        "slave_assets_version": _slave_assets_version,
        "documents": documents,
    }


def read_document(document_id: str) -> dict[str, Any]:
    """The full document plus its provenance, or ``DocumentationError``.

    The catalog is closed: an empty identifier, an unknown identifier, a path,
    a URL or an encoded variant is never resolved against the filesystem, only
    looked up as a catalog key.
    """
    if not isinstance(document_id, str) or not document_id:
        raise DocumentationError(
            DOC_NOT_FOUND,
            "document_id must be a catalog id such as "
            + ", ".join(_CATALOG_IDS),
        )
    entry = _BY_ID.get(document_id)
    if entry is None:
        raise DocumentationError(
            DOC_NOT_FOUND,
            f"unknown document id {document_id!r}: identifiers are catalog keys, "
            "not paths or URLs",
        )
    content = _read_text_resource(_resolve(entry), entry.document_id)
    metadata = _metadata(entry)
    metadata["mime_type"] = MIME_TYPE
    metadata["content"] = content
    return metadata


def _error_envelope(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "error",
        "error": {"code": code, "message": message, "retryable": False},
    }


def docs_index() -> dict[str, Any]:
    """Tool payload: the catalog. It reads metadata only, never the rig."""
    return list_documents()


def docs_read(document_id: str) -> dict[str, Any]:
    """Tool payload: one document, or the shared error envelope."""
    try:
        return read_document(document_id)
    except DocumentationError as exc:
        return _error_envelope(exc.code, exc.message)


def _resource_handler(document_id: str) -> Any:
    """A no-argument handler for one concrete resource URI.

    FastMCP treats a handler with parameters as a URI template, so the id is
    bound by this factory instead of a default argument; the returned function
    has an empty signature and cannot accept a client-supplied path.
    """

    def _resource() -> str:
        try:
            return read_document(document_id)["content"]
        except DocumentationError as exc:
            # FastMCP turns a handler exception into an MCP error; the code
            # stays in the message so the client can branch on it.
            raise ValueError(f"{exc.code}: {exc.message}") from None

    return _resource


def register_documentation(server: "FastMCP") -> None:
    """Registers the two tools and the seven fixed resources exactly once.

    The handlers read packaged assets only: no lease, gate, firmware, HID or
    daemon-state call is made here, so documentation is readable with the rig
    powered off. Resources are concrete URIs (never a template), so a client
    cannot request an arbitrary path through them.
    """
    server.tool(
        description=(
            "Lists the Titella documentation catalog: id, title, description, "
            "uri, source and version for every packaged document. Use it to "
            "choose a document without reading them all."
        ),
        structured_output=False,
    )(docs_index)

    server.tool(
        description=(
            "Reads one canonical Titella document by catalog id and returns its "
            "full Markdown content with source and version. Unknown ids, paths "
            "and URLs answer doc_not_found; a missing or oversized packaged "
            "asset answers docs_unavailable."
        ),
        structured_output=False,
    )(docs_read)

    for entry in _CATALOG:
        server.resource(
            entry.uri,
            name=entry.title,
            description=entry.description,
            mime_type=MIME_TYPE,
        )(_resource_handler(entry.document_id))


__all__ = [
    "DOCS_UNAVAILABLE",
    "DOC_NOT_FOUND",
    "DocumentationError",
    "MAX_DOCUMENT_BYTES",
    "MIME_TYPE",
    "SERVER_INSTRUCTIONS",
    "SOURCE_SLAVE_ASSETS",
    "SOURCE_TITELLA",
    "docs_index",
    "docs_read",
    "list_documents",
    "read_document",
    "register_documentation",
]
