"""Behavioral tests of the packaged documentation catalog and its errors.

The catalog is a closed set of seven identifiers with exactly one canonical
source each. These tests pin the observable contract — ordering, provenance,
versions, byte equality with the packaged source, and the two error codes —
without touching the rig: documentation never gates on the lease, the agent
or the firmware.

The master documents are compared against their canonical checkout copy
(``src/titella/_docs/``), which is what the wheel packages. The slave
documents come from the installed ``titella-slave-assets`` wheel; their
source-to-wheel byte equality is enforced by the slave repository's build
tests, and here they are compared against the packaged resource itself.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from titella import __version__ as server_version
from titella import documentation
from titella_slave_assets import __version__ as slave_assets_version

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "src" / "titella" / "_docs"

EXPECTED_IDS = [
    "bootstrap-python",
    "bootstrap-windows",
    "getting-started",
    "mcp-transport",
    "operation",
    "serial-protocol",
    "troubleshooting",
]
SERVER_IDS = {"getting-started", "mcp-transport", "operation", "troubleshooting"}
SLAVE_IDS = {"bootstrap-python", "bootstrap-windows", "serial-protocol"}


class CanonicalSourceTests(unittest.TestCase):
    """The checkout carries exactly one editable copy of each server document.

    A duplicated procedure or an out-of-policy document (empty, oversized,
    non-UTF-8) must fail here before it reaches the wheel or the sdist.
    """

    def test_docs_directory_holds_exactly_the_four_server_documents(self) -> None:
        self.assertEqual(
            sorted(path.name for path in DOCS.glob("*.md")),
            [f"{document_id}.md" for document_id in sorted(SERVER_IDS)],
        )

    def test_no_second_editable_copy_of_the_server_documents_exists(self) -> None:
        # Only repository-owned trees are scanned: .venv, build and dist are
        # environments, not sources, and must never be walked here.
        candidates = [ROOT / "README.md", *sorted((ROOT / "src").rglob("*.md")),
                      *sorted((ROOT / "tests").rglob("*.md"))]
        duplicates = sorted(
            path.relative_to(ROOT).as_posix()
            for path in candidates
            if path.parent.name != "_docs" and path.stem in SERVER_IDS
        )
        self.assertEqual(duplicates, [])

    def test_each_canonical_source_is_bounded_utf8_and_nonempty(self) -> None:
        for document_id in sorted(SERVER_IDS):
            payload = (DOCS / f"{document_id}.md").read_bytes()
            self.assertTrue(payload.strip(), document_id)
            self.assertLessEqual(len(payload), documentation.MAX_DOCUMENT_BYTES)
            payload.decode("utf-8")


    def test_packaging_declares_the_canonical_docs(self) -> None:
        """A wheel that omits _docs/*.md would ship docs_unavailable."""
        try:
            import tomllib as toml_reader
        except ModuleNotFoundError:  # Python 3.10
            import tomli as toml_reader  # type: ignore[no-redef]

        with (ROOT / "pyproject.toml").open("rb") as stream:
            configuration = toml_reader.load(stream)
        package_data = configuration["tool"]["setuptools"]["package-data"]
        self.assertIn("_docs/*.md", package_data["titella"])
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertTrue(
            any("src" in line and "*.md" in line for line in manifest.splitlines()),
            "the sdist manifest must include the src markdown documents",
        )


class CatalogTests(unittest.TestCase):
    def test_catalog_is_closed_sorted_and_complete(self) -> None:
        catalog = documentation.list_documents()
        ids = [entry["id"] for entry in catalog["documents"]]
        self.assertEqual(ids, EXPECTED_IDS)
        self.assertEqual(ids, sorted(ids))

    def test_versions_come_from_the_packages_that_ship_the_documents(self) -> None:
        catalog = documentation.list_documents()
        self.assertEqual(catalog["server_version"], server_version)
        self.assertEqual(catalog["slave_assets_version"], slave_assets_version)
        for entry in catalog["documents"]:
            expected = (
                server_version
                if entry["id"] in SERVER_IDS
                else slave_assets_version
            )
            self.assertEqual(entry["version"], expected, entry["id"])
            self.assertEqual(
                entry["source"],
                "titella" if entry["id"] in SERVER_IDS else "titella-slave-assets",
            )

    def test_every_entry_carries_discovery_metadata_and_a_fixed_uri(self) -> None:
        for entry in documentation.list_documents()["documents"]:
            self.assertEqual(entry["uri"], f"titella://docs/{entry['id']}")
            for field in ("title", "description"):
                self.assertTrue(entry[field].strip(), (entry["id"], field))

    def test_read_returns_the_document_with_provenance(self) -> None:
        for entry in documentation.list_documents()["documents"]:
            loaded = documentation.read_document(entry["id"])
            self.assertEqual(loaded["id"], entry["id"])
            self.assertEqual(loaded["title"], entry["title"])
            self.assertEqual(loaded["uri"], entry["uri"])
            self.assertEqual(loaded["source"], entry["source"])
            self.assertEqual(loaded["version"], entry["version"])
            self.assertEqual(loaded["mime_type"], "text/markdown")
            content = loaded["content"]
            self.assertTrue(content.strip(), entry["id"])
            self.assertLessEqual(
                len(content.encode("utf-8")), documentation.MAX_DOCUMENT_BYTES
            )

    def test_server_documents_match_their_canonical_sources_byte_for_byte(self) -> None:
        for document_id in sorted(SERVER_IDS):
            source = (DOCS / f"{document_id}.md").read_bytes()
            packaged = documentation.read_document(document_id)["content"]
            self.assertEqual(
                packaged.encode("utf-8"), source, document_id
            )

    def test_slave_documents_are_served_from_the_assets_package(self) -> None:
        from importlib.resources import files

        for document_id in sorted(SLAVE_IDS):
            resource = (
                files("titella_slave_assets")
                .joinpath("_data")
                .joinpath("docs")
                .joinpath(f"{document_id}.md")
            )
            self.assertEqual(
                documentation.read_document(document_id)["content"].encode("utf-8"),
                resource.read_bytes(),
                document_id,
            )


class NotIdentifiersButKeysTests(unittest.TestCase):
    """Identifiers are catalog keys: paths, URLs and encodings never resolve."""

    def test_empty_unknown_paths_and_urls_are_doc_not_found(self) -> None:
        for value in (
            "",
            "   ",
            "no-such-document",
            "../config.toml",
            "..%2fconfig.toml",
            "docs/getting-started.md",
            "getting-started.md",
            "/etc/passwd",
            "C:\\Windows\\win.ini",
            "titella://docs/getting-started",
            "https://example.invalid/getting-started",
            "getting-started?raw=1",
        ):
            with self.assertRaises(documentation.DocumentationError) as raised:
                documentation.read_document(value)
            self.assertEqual(raised.exception.code, documentation.DOC_NOT_FOUND, value)
            self.assertFalse(raised.exception.message == "")

    def test_tool_wrapper_returns_the_shared_error_envelope(self) -> None:
        for value in ("", "../config.toml", "unknown"):
            result = documentation.docs_read(value)
            self.assertEqual(result["ok"], False)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["error"]["code"], "doc_not_found")
            self.assertFalse(result["error"]["retryable"])
            self.assertTrue(result["error"]["message"])

    def test_tool_wrapper_returns_the_document_on_success(self) -> None:
        result = documentation.docs_read("getting-started")
        self.assertNotIn("ok", result)
        self.assertEqual(result["id"], "getting-started")
        self.assertTrue(result["content"].strip())


class PackagedAssetValidationTests(unittest.TestCase):
    """Missing, empty, oversized or undecodable assets are ``docs_unavailable``.

    The validation core takes an ``importlib.resources`` Traversable, so it is
    exercised against synthetic directories: real package assets are never
    altered and no fallback path is reachable.
    """

    def _assert_unavailable(self, resource, document_id: str = "getting-started") -> None:
        with self.assertRaises(documentation.DocumentationError) as raised:
            documentation._read_text_resource(resource, document_id)
        self.assertEqual(raised.exception.code, documentation.DOCS_UNAVAILABLE)
        self.assertTrue(raised.exception.message)

    def test_missing_resource_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self._assert_unavailable(Path(temp) / "absent.md")

    def test_empty_resource_is_never_returned_as_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "empty.md"
            path.write_bytes(b"")
            self._assert_unavailable(path)

    def test_oversized_resource_is_refused_not_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "big.md"
            path.write_bytes(b"x" * (documentation.MAX_DOCUMENT_BYTES + 1))
            self._assert_unavailable(path)

    def test_non_utf8_resource_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "binary.md"
            path.write_bytes(b"\xff\xfe\x00not markdown")
            self._assert_unavailable(path)

    def test_missing_package_is_unavailable(self) -> None:
        entry = documentation._Document(
            document_id="getting-started",
            title="t",
            description="d",
            source=documentation.SOURCE_TITELLA,
            package="titella_docs_absent_package",
            directory=("_docs",),
            filename="getting-started.md",
        )
        with self.assertRaises(documentation.DocumentationError) as raised:
            documentation._resolve(entry)
        self.assertEqual(raised.exception.code, documentation.DOCS_UNAVAILABLE)

    def test_unavailable_is_reported_as_the_envelope_not_as_content(self) -> None:
        original = documentation._resolve
        try:
            documentation._resolve = lambda entry: Path("/nonexistent/titella.md")
            result = documentation.docs_read("getting-started")
        finally:
            documentation._resolve = original
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "docs_unavailable")
        self.assertNotIn("content", result)


class RegistrationTests(unittest.TestCase):
    """One registration call exposes exactly two tools and seven resources."""

    def _server(self) -> FastMCP:
        server = FastMCP("titella-test", instructions=documentation.SERVER_INSTRUCTIONS)
        documentation.register_documentation(server)
        return server

    def test_tools_and_fixed_resources_are_registered_once(self) -> None:
        server = self._server()
        tools = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertEqual(tools, {"docs_index", "docs_read"})
        resources = {str(resource.uri) for resource in asyncio.run(server.list_resources())}
        self.assertEqual(
            resources, {f"titella://docs/{document_id}" for document_id in EXPECTED_IDS}
        )
        self.assertEqual(
            {str(template.uriTemplate) for template in asyncio.run(server.list_resource_templates())},
            set(),
        )

    def test_resources_are_concrete_and_read_the_catalog_content(self) -> None:
        server = self._server()
        for document_id in EXPECTED_IDS:
            contents = list(asyncio.run(server.read_resource(f"titella://docs/{document_id}")))
            self.assertEqual(len(contents), 1)
            self.assertEqual(contents[0].mime_type, "text/markdown")
            self.assertEqual(
                contents[0].content,
                documentation.read_document(document_id)["content"],
            )

    def test_unavailable_asset_becomes_an_mcp_resource_error_not_empty_text(self) -> None:
        from mcp.server.fastmcp.exceptions import ResourceError

        server = self._server()
        original = documentation._resolve
        try:
            documentation._resolve = lambda entry: Path("/nonexistent/titella.md")
            with self.assertRaises(ResourceError) as raised:
                list(asyncio.run(server.read_resource("titella://docs/getting-started")))
        finally:
            documentation._resolve = original
        self.assertIn("docs_unavailable", str(raised.exception))

    def test_unknown_resource_uri_is_rejected(self) -> None:
        from mcp.server.fastmcp.exceptions import ResourceError

        server = self._server()
        # FastMCP versions differ on the exception type for an unregistered
        # URI (ResourceError or ValueError); both are MCP errors, never text.
        for uri in ("titella://docs/../config.toml", "titella://docs/unknown"):
            with self.assertRaises((ResourceError, ValueError)) as raised:
                list(asyncio.run(server.read_resource(uri)))
            self.assertIsInstance(raised.exception, (ResourceError, ValueError))

    def test_tools_through_the_server_return_json_payloads(self) -> None:
        server = self._server()

        def payload(name: str, arguments: dict) -> dict:
            blocks = asyncio.run(server.call_tool(name, arguments))
            for block in blocks:
                if getattr(block, "type", None) == "text":
                    return json.loads(block.text)
            raise AssertionError(f"{name} returned no text content")

        index = payload("docs_index", {})
        self.assertEqual([entry["id"] for entry in index["documents"]], EXPECTED_IDS)
        missing = payload("docs_read", {"document_id": "../x"})
        self.assertEqual(missing["error"]["code"], "doc_not_found")
        loaded = payload("docs_read", {"document_id": "getting-started"})
        self.assertEqual(loaded["id"], "getting-started")
        self.assertEqual(
            loaded["content"], documentation.read_document("getting-started")["content"]
        )

    def test_instructions_are_exact(self) -> None:
        self.assertEqual(
            documentation.SERVER_INSTRUCTIONS,
            "Use docs_index to discover Titella procedures and docs_read to read "
            "them. Read getting-started before operating a rig and the "
            "platform-specific bootstrap guide before slave_bootstrap. "
            "Documentation does not report live device state; use kvm_status "
            "for that.",
        )
        self.assertIn("docs_index", documentation.SERVER_INSTRUCTIONS)
        self.assertIn("kvm_status", documentation.SERVER_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
