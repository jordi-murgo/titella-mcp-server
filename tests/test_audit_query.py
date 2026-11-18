"""Offline query tests: search, read, note, export — no rig, no registry."""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from titella.audit import AuditCall, AuditError, AuditManager, MAX_NOTE_BYTES, utc_now_rfc3339
from titella.audit_query import (
    audit_export,
    audit_note,
    audit_read,
    audit_search,
    load_archive,
)


class QueryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "audits"
        self.manager = AuditManager(self.root)
        self.addCleanup(self.manager.close)
        self.manager.start()
        self.activated = self.manager.activate("queries", "lab")
        self.run_id = self.activated["run_id"]
        self._action_counter = 0

    def _call(self, tool: str = "remote_shell", titella: str = "lab") -> AuditCall:
        self._action_counter += 1
        return AuditCall(
            action_id=f"{self._action_counter:032x}",
            audit_id="queries",
            run_id=self.run_id,
            titella_id=titella,
            tool=tool,
            started_monotonic=0.0,
            started_utc=utc_now_rfc3339(),
        )

    def _open_stream(self, call: AuditCall | None = None, role: str = "stdout") -> tuple[str, AuditCall]:
        call = call or self._call()
        evidence_id = self.manager.open_stream(
            call, role=role, channel="shell", operation_id="job-1", titella_id=call.titella_id
        )
        assert evidence_id is not None
        return evidence_id, call

    def _stream(
        self,
        data: bytes,
        role: str = "stdout",
        *,
        close: bool = True,
        call: AuditCall | None = None,
        step: int = 4096,
    ) -> str:
        evidence_id, _call = self._open_stream(call, role=role)
        for position in range(0, len(data), step):
            self.manager.append_stream(evidence_id, data[position : position + step])
        if close:
            self.manager.close_stream(evidence_id, complete=True, reason="job.finished:ok")
        self.manager.flush_barrier()
        return evidence_id

    def _artifact_bytes(self, audit_id: str, evidence_id: str) -> bytes:
        view = load_archive(self.root, audit_id)
        meta = view.evidence_index[evidence_id]
        return (self.root / audit_id / meta.relative_path).read_bytes()

    def _events_text(self, export_dir: Path) -> str:
        return (export_dir / "events.jsonl").read_text(encoding="utf-8")


class SearchTests(QueryTestCase):
    def test_marker_split_across_chunks_is_found_with_offsets(self) -> None:
        marker = b"NEEDLE-ACROSS-CHUNKS"
        filler = b"x" * 4090  # forces the marker across an append boundary
        payload = filler + marker + filler
        stream_id = self._stream(payload)
        result = audit_search(self.root, "queries", "needle-across-chunks")
        hits = [h for h in result["hits"] if h["evidence_id"] == stream_id]
        self.assertTrue(hits)
        offset = hits[0]["byte_offset"]
        self.assertIsNotNone(offset)
        self.assertEqual(payload[offset : offset + len(marker)], marker)

    def test_multibyte_marker_straddles_window_boundary(self) -> None:
        marker = "NEÆDLE".encode("utf-8")  # Æ is two bytes across the boundary
        payload = b"x" * 65533 + marker + b"y" * 200
        stream_id = self._stream(payload)
        result = audit_search(self.root, "queries", "neædle")
        hits = [h for h in result["hits"] if h["evidence_id"] == stream_id]
        self.assertTrue(hits)
        offset = hits[0]["byte_offset"]
        self.assertEqual(offset, 65533)
        self.assertEqual(payload[offset : offset + len(marker)], marker)

    def test_casefold_expansion_maps_original_byte_offsets(self) -> None:
        payload = b"see-" + "straße".encode("utf-8") + b"-end"
        stream_id = self._stream(payload)
        # "ss" only exists after casefolding 'ß' (char expands to two chars).
        hits = [
            h
            for h in audit_search(self.root, "queries", "ss")["hits"]
            if h["evidence_id"] == stream_id
        ]
        self.assertTrue(hits)
        offset = hits[0]["byte_offset"]
        self.assertEqual(payload[offset : offset + 2], "ß".encode("utf-8"))
        # The whole word matches through the expansion from its first byte.
        word_hits = [
            h
            for h in audit_search(self.root, "queries", "STRASSE")["hits"]
            if h["evidence_id"] == stream_id
        ]
        self.assertTrue(word_hits)
        word_offset = word_hits[0]["byte_offset"]
        self.assertEqual(word_offset, 4)
        self.assertEqual(
            payload[word_offset : word_offset + len("straße".encode("utf-8"))],
            "straße".encode("utf-8"),
        )

    def test_invalid_utf8_bytes_do_not_shift_offsets(self) -> None:
        payload = b"\xff\xfeok-marker\x00tail"
        stream_id = self._stream(payload)
        hits = [
            h
            for h in audit_search(self.root, "queries", "ok-marker")["hits"]
            if h["evidence_id"] == stream_id
        ]
        self.assertTrue(hits)
        self.assertEqual(hits[0]["byte_offset"], 2)
        snippet = hits[0]["snippet"]
        self.assertIn("\ufffd", snippet)
        self.assertLessEqual(len(snippet), 512)

    def test_pagination_is_complete_without_duplicates(self) -> None:
        occurrences = 10
        payload = b"zz MARK yy " * occurrences  # marker at 3 + 11*k
        stream_id = self._stream(payload)
        offsets: list[int] = []
        cursor = None
        pages = 0
        while True:
            result = audit_search(self.root, "queries", "mark", cursor=cursor, limit=3)
            pages += 1
            for hit in result["hits"]:
                if hit["evidence_id"] == stream_id:
                    offsets.append(hit["byte_offset"])
            cursor = result["next_cursor"]
            if cursor is None:
                break
            self.assertLess(pages, 20)
        self.assertEqual(len(offsets), occurrences)
        self.assertEqual(offsets, sorted(set(offsets)))
        self.assertEqual(offsets, [3 + 11 * k for k in range(occurrences)])

    def test_snapshot_pagination_ignores_mid_append(self) -> None:
        part1 = b"alpha MARKER one\n" * 3  # 17-byte blocks: markers at 6, 23, 40
        part2 = b"beta MARKER two\n" * 3
        stream_id, _call = self._open_stream()
        self.manager.append_stream(stream_id, part1)
        self.manager.flush_barrier()

        first = audit_search(self.root, "queries", "marker", limit=2)
        self.assertEqual(len(first["hits"]), 2)
        self.assertIsNotNone(first["next_cursor"])

        self.manager.append_stream(stream_id, part2)
        self.manager.flush_barrier()

        second = audit_search(
            self.root, "queries", "marker", cursor=first["next_cursor"], limit=10
        )
        offsets = [hit["byte_offset"] for hit in second["hits"] if hit["evidence_id"] == stream_id]
        # part1 is 17 bytes per block ("alpha MARKER one\n"); markers at
        # 6, 23 and 40, so the second page continues exactly at 40.
        self.assertEqual(offsets, [40])
        for hit in second["hits"]:
            self.assertLess(hit["byte_offset"], len(part1))
            self.assertNotIn("beta", hit["snippet"])
        self.assertIsNone(second["next_cursor"])

        fresh = audit_search(self.root, "queries", "marker", limit=50)
        fresh_offsets = [
            hit["byte_offset"] for hit in fresh["hits"] if hit["evidence_id"] == stream_id
        ]
        self.assertEqual(len(fresh_offsets), 6)

    def test_empty_query_lists_chronology_and_filters(self) -> None:
        events = audit_search(self.root, "queries", "", kind="audit.activated")
        self.assertEqual(len(events["hits"]), 1)
        self.assertEqual(events["hits"][0]["kind"], "audit.activated")
        missing = audit_search(self.root, "queries", "", kind="nope")
        self.assertEqual(missing["hits"], [])

    def test_empty_query_pagination_snapshot_ignores_new_events(self) -> None:
        self._stream(b"x")  # evidence.created, stream.chunk, stream.closed
        first = audit_search(self.root, "queries", "", limit=2)
        self.assertEqual(len(first["hits"]), 2)
        self.assertIsNotNone(first["next_cursor"])

        audit_note(self.root, self.manager, "queries", "later note", [])

        second = audit_search(
            self.root, "queries", "", cursor=first["next_cursor"], limit=10
        )
        self.assertEqual([hit["seq"] for hit in second["hits"]], [3, 4])
        self.assertNotIn("note", [hit["kind"] for hit in second["hits"]])
        self.assertIsNone(second["next_cursor"])

    def test_rfc3339_filters_with_zone_and_utc_normalization(self) -> None:
        base = audit_search(self.root, "queries", "")
        first_ts = base["hits"][0]["ts_utc"]
        zulu = audit_search(self.root, "queries", "", since=first_ts)
        offset = audit_search(
            self.root, "queries", "", since=first_ts.replace("Z", "+00:00")
        )
        self.assertEqual(len(zulu["hits"]), len(offset["hits"]))
        with self.assertRaises(AuditError) as ctx:
            audit_search(self.root, "queries", "", since="2026-01-01T00:00:00")
        self.assertEqual(ctx.exception.code, "invalid_argument")
        with self.assertRaises(AuditError):
            audit_search(
                self.root,
                "queries",
                "",
                since="2030-01-01T00:00:00Z",
                until="2020-01-01T00:00:00Z",
            )

    def test_stream_hits_honor_filters(self) -> None:
        stream_id = self._stream(b"FILTERED-MARKER bytes", call=self._call(titella="lab"))

        def offsets(**kwargs: object) -> list[int]:
            result = audit_search(self.root, "queries", "filtered-marker", **kwargs)  # type: ignore[arg-type]
            return [
                hit["byte_offset"]
                for hit in result["hits"]
                if hit["evidence_id"] == stream_id
            ]

        self.assertTrue(offsets())
        self.assertEqual(offsets(titella="other-rig"), [])
        self.assertEqual(offsets(kind="note"), [])
        self.assertTrue(offsets(kind="stdout"))
        self.assertTrue(offsets(since="1970-01-01T00:00:00Z"))
        self.assertEqual(offsets(until="1970-01-01T00:00:00Z"), [])

    def test_upload_and_download_bins_are_not_scanned(self) -> None:
        call = self._call(tool="remote_transfer")
        for role in ("upload", "download"):
            evidence_id = self.manager.record_artifact(
                call,
                role=role,
                data=b"UPLOAD-SECRET-MARKER",
                mime_type="application/octet-stream",
                extension="bin",
            )
            self.assertIsNotNone(evidence_id)
        self.manager.flush_barrier()
        result = audit_search(self.root, "queries", "upload-secret-marker")
        self.assertEqual(result["hits"], [])

    def test_note_text_is_searchable_via_artifact(self) -> None:
        note = audit_note(self.root, self.manager, "queries", "NEEDLE-NOTE-VERIFIED", [])
        note_id = note["evidence_id"]
        result = audit_search(self.root, "queries", "needle-note-verified")
        hits = [hit for hit in result["hits"] if hit["evidence_id"] == note_id]
        self.assertTrue(hits)
        hit = hits[0]
        self.assertEqual(hit["kind"], "note")
        self.assertIsNotNone(hit["byte_offset"])
        raw = self._artifact_bytes("queries", note_id)
        self.assertIn(b"NEEDLE-NOTE-VERIFIED", raw[hit["byte_offset"] : hit["byte_offset"] + 20])
        self.assertIsNotNone(hit["seq"])

    def test_ocr_text_is_searchable_and_indexed(self) -> None:
        image_id = self.manager.record_ocr(
            "lab",
            source_evidence_id=None,
            frame_sha256=None,
            image_bytes=b"\x89PNG\r\n\x1a\nfake",
            text="OCR NEEDLE-OCR-TEXT here",
            scope="full",
            bbox=None,
            model="fake-model",
        )
        self.assertIsNotNone(image_id)
        self.manager.flush_barrier()
        view = load_archive(self.root, "queries")
        result_id = next(
            event["data"]["result_evidence_id"]
            for event in view.events
            if event["kind"] == "ocr.recorded"
        )
        result = audit_search(self.root, "queries", "needle-ocr-text")
        hits = [hit for hit in result["hits"] if hit["evidence_id"] == result_id]
        self.assertTrue(hits)
        self.assertEqual(hits[0]["kind"], "result")
        payload = audit_read(self.root, "queries", result_id)[0]
        self.assertEqual(payload["type"], "bytes")
        raw = base64.b64decode(payload["data_b64"])
        self.assertIn(b"NEEDLE-OCR-TEXT", raw)

    def test_cursor_from_foreign_audit_or_filters_rejected(self) -> None:
        stream = self._stream(b"cursor marker bytes")
        first = audit_search(self.root, "queries", "", limit=1)
        self.assertIsNotNone(first["next_cursor"])
        with self.assertRaises(AuditError):
            audit_search(
                self.root, "queries", "", cursor=first["next_cursor"], kind="note"
            )
        with self.assertRaises(AuditError) as ctx:
            audit_search(self.root, "queries", "", cursor="!!!not-a-token!!!")
        self.assertEqual(ctx.exception.code, "invalid_argument")
        # A cursor minted for another audit is rejected by audit id.
        self.manager.activate("second", "lab2")
        with self.assertRaises(AuditError) as ctx:
            audit_search(self.root, "second", "", cursor=first["next_cursor"])
        self.assertEqual(ctx.exception.code, "invalid_argument")
        self.assertTrue(stream)

    def test_query_and_limit_validation(self) -> None:
        with self.assertRaises(AuditError):
            audit_search(self.root, "queries", "a" * 513)
        for bad in (0, 201, -1):
            with self.assertRaises(AuditError):
                audit_search(self.root, "queries", "", limit=bad)
        with self.assertRaises(AuditError):
            audit_search(self.root, "queries", "", limit=True)  # type: ignore[arg-type]

    def test_corrupt_tail_warning_preserves_originals(self) -> None:
        stream = self._stream(b"tail-check")
        events_path = self.root / "queries" / "events.jsonl"
        original = events_path.read_bytes()
        with events_path.open("ab") as handle:
            handle.write(b'{"schema_version":1,"seq":999,"broken')
        result = audit_search(self.root, "queries", "tail-check")
        self.assertIn("warning", result)
        self.assertIn("audit_corrupt", result["warning"])
        self.assertTrue(
            any(hit["evidence_id"] == stream for hit in result["hits"])
        )
        self.assertEqual(
            events_path.read_bytes(), original + b'{"schema_version":1,"seq":999,"broken'
        )


class ReadTests(QueryTestCase):
    def test_binary_read_pagination_and_eof(self) -> None:
        payload = bytes(range(256)) * 40  # 10240 bytes
        stream_id = self._stream(payload)
        page1 = audit_read(self.root, "queries", stream_id, offset=0, length=4096)[0]
        self.assertEqual(page1["length"], 4096)
        self.assertFalse(page1["eof"])
        page2 = audit_read(self.root, "queries", stream_id, offset=4096, length=4096)[0]
        page3 = audit_read(self.root, "queries", stream_id, offset=8192, length=4096)[0]
        self.assertTrue(page3["eof"])
        joined = (
            base64.b64decode(page1["data_b64"])
            + base64.b64decode(page2["data_b64"])
            + base64.b64decode(page3["data_b64"])
        )
        self.assertEqual(joined, payload)
        self.assertTrue(page1["sealed"])
        self.assertTrue(page1["complete"])
        self.assertEqual(page1["sha256"], hashlib.sha256(payload).hexdigest())
        past = audit_read(self.root, "queries", stream_id, offset=99999, length=100)[0]
        self.assertEqual(past["length"], 0)
        self.assertEqual(past["next_offset"], past["size"])

    def test_partial_page_rejects_a_tampered_open_chunk(self) -> None:
        evidence_id = self._stream(b"0123456789", close=False)
        page = audit_read(self.root, "queries", evidence_id, offset=4, length=3)[0]
        self.assertEqual(base64.b64decode(page["data_b64"]), b"456")
        artifact = self.root / "queries" / "artifacts" / f"{evidence_id}.bin"
        artifact.write_bytes(b"01234X6789")
        with self.assertRaises(AuditError) as caught:
            audit_read(self.root, "queries", evidence_id, offset=4, length=3)
        self.assertEqual(caught.exception.code, "audit_integrity_failed")

    def test_open_stream_read_is_capped_to_confirmed_prefix(self) -> None:
        evidence_id, _call = self._open_stream()
        prefix = b"0123456789"
        self.manager.append_stream(evidence_id, prefix)
        self.manager.flush_barrier()
        artifact = self.root / "queries" / "artifacts" / f"{evidence_id}.bin"
        # Bytes written ahead of the journal must never be served as evidence.
        with artifact.open("ab") as handle:
            handle.write(b"EXTRA-NOT-CONFIRMED")

        page = audit_read(self.root, "queries", evidence_id, offset=0, length=65536)[0]
        self.assertEqual(page["length"], len(prefix))
        self.assertEqual(base64.b64decode(page["data_b64"]), prefix)
        self.assertFalse(page["sealed"])
        self.assertFalse(page["complete"])
        self.assertEqual(page["size"], len(prefix))
        self.assertEqual(page["sha256"], hashlib.sha256(prefix).hexdigest())
        past = audit_read(self.root, "queries", evidence_id, offset=len(prefix))[0]
        self.assertEqual(past["length"], 0)
        self.assertTrue(past["eof"])

        # Sealing does not bless bytes the journal never confirmed: the on-disk
        # length differs from the sealed record, so reading must refuse.
        self.manager.close_stream(evidence_id, complete=True, reason="job.finished:ok")
        self.manager.flush_barrier()
        with self.assertRaises(AuditError) as ctx:
            audit_read(self.root, "queries", evidence_id, offset=0)
        self.assertEqual(ctx.exception.code, "audit_integrity_failed")

        # Restoring the exact sealed bytes makes the same read succeed.
        artifact.write_bytes(prefix)
        sealed = audit_read(self.root, "queries", evidence_id, offset=0)[0]
        self.assertTrue(sealed["sealed"])
        self.assertTrue(sealed["complete"])
        self.assertEqual(base64.b64decode(sealed["data_b64"]), prefix)

    def test_sealed_integrity_failure_detected(self) -> None:
        stream_id = self._stream(b"integrity-check")
        target = self.root / "queries" / "artifacts" / f"{stream_id}.bin"
        target.write_bytes(b"tampered")
        with self.assertRaises(AuditError) as ctx:
            audit_read(self.root, "queries", stream_id)
        self.assertEqual(ctx.exception.code, "audit_integrity_failed")

    def test_unknown_evidence_and_foreign_id_rejected(self) -> None:
        with self.assertRaises(AuditError) as ctx:
            audit_read(self.root, "queries", "0" * 32)
        self.assertEqual(ctx.exception.code, "evidence_not_found")
        with self.assertRaises(AuditError):
            audit_read(self.root, "queries", "../events.jsonl")
        with self.assertRaises(AuditError):
            audit_read(self.root, "queries", "0" * 32, offset=-1)
        with self.assertRaises(AuditError):
            audit_read(self.root, "queries", "0" * 32, length=0)

    def test_png_read_returns_image_dict_and_rejects_pagination(self) -> None:
        png = b"\x89PNG\r\n\x1a\nfake-png-body"
        call = self._call(tool="screen")
        png_id = self.manager.record_artifact(
            call,
            role="capture",
            data=png,
            mime_type="image/png",
            extension="png",
        )
        self.assertIsNotNone(png_id)
        self.manager.flush_barrier()
        payload = audit_read(self.root, "queries", png_id)[0]
        self.assertEqual(payload["type"], "image")
        self.assertEqual(payload["mime_type"], "image/png")
        self.assertEqual(base64.b64decode(payload["data_b64"]), png)
        with self.assertRaises(AuditError):
            audit_read(self.root, "queries", png_id, offset=1)


class NoteTests(QueryTestCase):
    def test_note_persisted_as_artifact_and_event(self) -> None:
        result = audit_note(self.root, self.manager, "queries", "paso verificado", [])
        self.assertTrue(result["ok"])
        note_path = self.root / "queries" / "artifacts" / f"{result['evidence_id']}.json"
        doc = json.loads(note_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["text"], "paso verificado")
        events = (self.root / "queries" / "events.jsonl").read_text()
        self.assertIn('"kind": "note"', events)
        view = load_archive(self.root, "queries")
        self.assertEqual(view.evidence_index[result["evidence_id"]].role, "note")

    def test_foreign_evidence_reference_rejected(self) -> None:
        with self.assertRaises(AuditError) as ctx:
            audit_note(self.root, self.manager, "queries", "x", ["f" * 32])
        self.assertEqual(ctx.exception.code, "evidence_not_found")

    def test_note_validation(self) -> None:
        with self.assertRaises(AuditError):
            audit_note(self.root, self.manager, "queries", "   ", [])
        with self.assertRaises(AuditError):
            audit_note(self.root, self.manager, "queries", "x" * (MAX_NOTE_BYTES + 1), [])
        stream = self._stream(b"ref-target")
        with self.assertRaises(AuditError):
            audit_note(self.root, self.manager, "queries", "dup refs", [stream, stream])

    def test_note_refused_on_corrupt_journal(self) -> None:
        events_path = self.root / "queries" / "events.jsonl"
        original = events_path.read_bytes()
        with events_path.open("ab") as handle:
            handle.write(b'{"broken')
        with self.assertRaises(AuditError) as ctx:
            audit_note(self.root, self.manager, "queries", "late note", [])
        self.assertEqual(ctx.exception.code, "audit_corrupt")
        self.assertEqual(events_path.read_bytes(), original + b'{"broken')


class ExportTests(QueryTestCase):
    def _selected_stream_and_secret_input(self) -> tuple[str, str]:
        stream = self._stream(b"export stream bytes")
        call = self._call(tool="remote_shell")
        secret = self.manager.record_artifact(
            call,
            role="input",
            data=json.dumps({"secret": "SECRET-INPUT-TOKEN"}).encode("utf-8"),
            mime_type="application/json",
            extension="json",
        )
        self.assertIsNotNone(secret)
        self.manager.flush_barrier()
        return stream, secret

    def test_export_selection_only_with_hashes_and_index(self) -> None:
        stream_id, secret_id = self._selected_stream_and_secret_input()
        result = audit_export(self.root, self.manager, "queries", [stream_id])
        self.assertTrue(result["ok"])
        self.assertTrue(result["recorded"])
        self.assertTrue(result["complete"])
        export_dir = Path(result["directory"])
        manifest = json.loads((export_dir / "manifest.json").read_text())
        self.assertTrue(manifest["contains_raw_evidence"])
        self.assertEqual(manifest["selection"], [stream_id])
        self.assertEqual(len(manifest["evidence"]), 1)
        entry = manifest["evidence"][0]
        self.assertEqual(len(entry["sha256"]), 64)
        copied = (export_dir / "assets" / f"{stream_id}.bin").read_bytes()
        self.assertEqual(hashlib.sha256(copied).hexdigest(), entry["sha256"])

        index = (export_dir / "evidence.md").read_text()
        self.assertIn(f"assets/{stream_id}.bin", index)
        self.assertIn(entry["sha256"], index)  # full digest, not truncated
        self.assertIn(manifest["created_at_utc"], index)

        events = self._events_text(export_dir)
        self.assertIn(stream_id, events)
        self.assertNotIn(secret_id, events)
        self.assertNotIn("SECRET-INPUT-TOKEN", events)
        self.assertNotIn("audit.activated", events)
        assets = sorted(p.name for p in (export_dir / "assets").iterdir())
        self.assertEqual(assets, [f"{stream_id}.bin"])

        second = audit_export(self.root, self.manager, "queries", [stream_id])
        self.assertNotEqual(second["export_id"], result["export_id"])
        self.assertTrue(Path(second["directory"]).is_dir())
        self.assertTrue(export_dir.is_dir())

    def test_export_filters_events_by_selected_mentions_only(self) -> None:
        stream_id, secret_id = self._selected_stream_and_secret_input()
        export = audit_export(self.root, self.manager, "queries", [secret_id])
        events = [json.loads(line) for line in self._events_text(Path(export["directory"])).splitlines()]
        mentioned: set[str] = set()
        for event in events:
            data = event.get("data") or {}
            if isinstance(data, dict):
                for value in data.values():
                    if isinstance(value, str) and len(value) == 32:
                        mentioned.add(value)
                    elif isinstance(value, list):
                        mentioned.update(v for v in value if isinstance(v, str))
        self.assertIn(secret_id, mentioned)
        self.assertNotIn(stream_id, mentioned)

    def test_export_open_stream_frozen_with_warning(self) -> None:
        evidence_id, _call = self._open_stream()
        confirmed = b"frozen-prefix"
        self.manager.append_stream(evidence_id, confirmed)
        self.manager.flush_barrier()
        artifact = self.root / "queries" / "artifacts" / f"{evidence_id}.bin"
        with artifact.open("ab") as handle:
            handle.write(b"UNCONFIRMED")
        result = audit_export(self.root, self.manager, "queries", [evidence_id])
        self.assertFalse(result["complete"])
        self.assertTrue(any("still open" in warning for warning in result["warnings"]))
        copied = (Path(result["directory"]) / "assets" / f"{evidence_id}.bin").read_bytes()
        self.assertEqual(copied, confirmed)
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        entry = manifest["evidence"][0]
        self.assertFalse(entry["sealed"])
        self.assertFalse(entry["complete"])
        self.assertEqual(entry["size"], len(confirmed))
        self.assertEqual(entry["sha256"], hashlib.sha256(confirmed).hexdigest())

    def test_export_integrity_failure_leaves_no_published_package(self) -> None:
        stream_id = self._stream(b"sealed-export-bytes")
        target = self.root / "queries" / "artifacts" / f"{stream_id}.bin"
        target.write_bytes(b"tampered-content")
        with self.assertRaises(AuditError) as ctx:
            audit_export(self.root, self.manager, "queries", [stream_id])
        self.assertEqual(ctx.exception.code, "audit_integrity_failed")
        exports_dir = self.root / "queries" / "exports"
        if exports_dir.exists():
            visible = [p for p in exports_dir.iterdir() if not p.name.startswith(".")]
            self.assertEqual(visible, [])

    def test_export_corrupt_journal_returns_partial_package(self) -> None:
        stream_id = self._stream(b"corrupt-export-bytes")
        events_path = self.root / "queries" / "events.jsonl"
        original = events_path.read_bytes()
        with events_path.open("ab") as handle:
            handle.write(b'{"schema_version":1,"seq":9999,"broken')
        result = audit_export(self.root, self.manager, "queries", [stream_id])
        self.assertFalse(result["complete"])
        self.assertFalse(result["recorded"])
        self.assertTrue(any("audit_corrupt" in warning for warning in result["warnings"]))
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        self.assertFalse(manifest["complete"])
        self.assertTrue(manifest["corrupt_tail"])
        self.assertFalse(manifest.get("recorded", True))
        self.assertEqual(
            events_path.read_bytes(), original + b'{"schema_version":1,"seq":9999,"broken'
        )

    def test_export_enumerates_omitted_references(self) -> None:
        call = self._call(tool="screen")
        frame = self.manager.record_artifact(
            call,
            role="capture",
            data=b"\x89PNG\r\n\x1a\nfull-frame",
            mime_type="image/png",
            extension="png",
        )
        self.assertIsNotNone(frame)
        crop = self.manager.record_artifact(
            call,
            role="diff",
            data=b"\x89PNG\r\n\x1a\ncrop",
            mime_type="image/png",
            extension="png",
            related_evidence_ids=[frame],
        )
        self.assertIsNotNone(crop)
        self.manager.flush_barrier()
        export = audit_export(self.root, self.manager, "queries", [crop])
        manifest = json.loads(Path(export["manifest_path"]).read_text())
        self.assertTrue(
            any(frame in entry for entry in manifest["omitted_references"]),
            manifest["omitted_references"],
        )
        index = Path(export["index_path"]).read_text()
        self.assertIn("Referenced but omitted", index)
        self.assertIn(frame, index)
        self.assertIn(f"assets/{crop}.png", index)
        self.assertIn("![evidence", index)

    def test_duplicate_foreign_and_oversized_selections_rejected(self) -> None:
        stream = self._stream(b"x")
        with self.assertRaises(AuditError):
            audit_export(self.root, self.manager, "queries", [stream, stream])
        with self.assertRaises(AuditError) as ctx:
            audit_export(self.root, self.manager, "queries", ["e" * 32])
        self.assertEqual(ctx.exception.code, "evidence_not_found")
        with self.assertRaises(AuditError):
            audit_export(self.root, self.manager, "queries", [])
        with self.assertRaises(AuditError) as ctx:
            audit_export(
                self.root, self.manager, "queries", [f"{index:032x}" for index in range(501)]
            )
        self.assertEqual(ctx.exception.code, "invalid_argument")
        with self.assertRaises(AuditError):
            audit_export(self.root, self.manager, "queries", [stream, "not-hex"])

    def test_record_export_failure_is_reported_not_faked(self) -> None:
        stream = self._stream(b"record-export-bytes")

        def boom(*_args: object, **_kwargs: object) -> None:
            raise AuditError("audit_recording_failed", "queue exceeded", True)

        original_record_export = self.manager.record_export
        self.manager.record_export = boom  # type: ignore[method-assign]
        self.addCleanup(setattr, self.manager, "record_export", original_record_export)
        result = audit_export(self.root, self.manager, "queries", [stream])
        self.assertTrue(result["ok"])
        self.assertFalse(result["recorded"])
        self.assertTrue(any("export.created" in warning for warning in result["warnings"]))
        manifest = json.loads(Path(result["manifest_path"]).read_text())
        self.assertFalse(manifest["recorded"])

    def test_barrier_failure_is_reported_as_warning(self) -> None:
        stream = self._stream(b"barrier-bytes")

        def boom() -> None:
            raise AuditError("audit_recording_failed", "writer jammed", False)

        original_barrier = self.manager.flush_barrier
        self.manager.flush_barrier = boom  # type: ignore[method-assign]
        self.addCleanup(setattr, self.manager, "flush_barrier", original_barrier)
        result = audit_export(self.root, self.manager, "queries", [stream])
        self.assertTrue(any("write barrier failed" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
