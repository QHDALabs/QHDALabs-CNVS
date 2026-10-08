import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from cnvs.cli import main
from cnvs.models import (
    Assessment,
    Claim,
    Event,
    Evidence,
    RawSnapshot,
    Source,
)
from cnvs.storage import Database, StorageError
from cnvs.validation import RecordValidationError, parse_record, validate_payload


ROOT = Path(__file__).resolve().parents[1]


def load_example(name: str) -> dict:
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


def make_records() -> tuple[Event, Source, Evidence, Claim, Assessment]:
    event = parse_record("event", load_example("event.json"))
    source = parse_record("source", load_example("source.json"))
    evidence = parse_record("evidence", load_example("evidence.json"))
    claim = parse_record("claim", load_example("claim.json"))
    assessment = parse_record("assessment", load_example("assessment.json"))
    assert isinstance(event, Event)
    assert isinstance(source, Source)
    assert isinstance(evidence, Evidence)
    assert isinstance(claim, Claim)
    assert isinstance(assessment, Assessment)
    return event, source, evidence, claim, assessment


class RecordValidationTests(unittest.TestCase):
    def test_supported_records_parse_into_typed_models(self):
        event, source, evidence, claim, assessment = make_records()

        self.assertIsInstance(event, Event)
        self.assertIsInstance(source, Source)
        self.assertIsInstance(evidence, Evidence)
        self.assertIsInstance(claim, Claim)
        self.assertIsInstance(assessment, Assessment)
        self.assertEqual(event.coordinates, (18.6, 54.4))
        self.assertEqual(claim.supporting_evidence_ids, ("EVD-EX-001",))
        self.assertEqual(assessment.confidence["attribution"], "UNKNOWN")

    def test_invalid_schema_version_and_missing_fields_are_rejected(self):
        payload = load_example("event.json")
        payload["schema_version"] = "2.0"
        with self.assertRaisesRegex(RecordValidationError, "schema_version"):
            validate_payload("event", payload)

        payload = load_example("event.json")
        payload.pop("created_at")
        with self.assertRaisesRegex(RecordValidationError, "created_at"):
            validate_payload("event", payload)

    def test_invalid_datetime_format_is_rejected(self):
        for invalid_time in ("not-a-time", "2026-10-07 09:00:00Z", "2026-10-07T09:00:00"):
            with self.subTest(timestamp=invalid_time):
                payload = load_example("event.json")
                payload["created_at"] = invalid_time
                with self.assertRaisesRegex(RecordValidationError, "date-time"):
                    validate_payload("event", payload)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cnvs.sqlite3"
        self.database = Database(self.database_path)
        self.event, self.source, self.evidence, self.claim, self.assessment = make_records()

    def tearDown(self):
        self.temp_dir.cleanup()

    def save_linked_records(self):
        self.database.save(self.event)
        self.database.save(self.source)
        self.database.save(self.evidence)
        self.database.save(self.claim)

    def test_migrations_apply_once_and_persist(self):
        self.assertEqual(
            [version for version, _ in self.database.migration_status()],
            [
                "001_initial",
                "002_collection_attempts",
                "003_normalized_documents",
                "004_event_timelines",
                "005_claim_extractions",
                "006_provenance_graph",
            ],
        )
        self.assertEqual(self.database.migrate(), 6)

        reopened = Database(self.database_path)
        self.assertEqual(reopened.migration_status(), self.database.migration_status())

    def test_concurrent_initialization_and_saves_keep_sequential_revisions(self):
        concurrent_path = Path(self.temp_dir.name) / "concurrent.sqlite3"

        def initialize_and_save() -> int:
            return Database(concurrent_path).save(self.event)

        with ThreadPoolExecutor(max_workers=4) as executor:
            revisions = list(executor.map(lambda _: initialize_and_save(), range(4)))

        self.assertEqual(sorted(revisions), [1, 2, 3, 4])
        self.assertEqual(
            Database(concurrent_path).revisions("event", self.event.event_id),
            [1, 2, 3, 4],
        )

    def test_applied_migration_checksum_cannot_be_changed(self):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                ("0" * 64, "001_initial"),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(StorageError, "has changed"):
            Database(self.database_path)

    def test_records_persist_and_reads_are_typed(self):
        self.save_linked_records()

        self.assertEqual(self.database.get("event", self.event.event_id), self.event)
        self.assertEqual(self.database.get("source", self.source.source_id), self.source)
        self.assertEqual(
            self.database.get("evidence", self.evidence.evidence_id), self.evidence
        )
        self.assertEqual(self.database.get("claim", self.claim.claim_id), self.claim)

    def test_revision_history_is_append_only_and_supports_exact_reads(self):
        self.database.save(self.event)
        revised = replace(self.event, title="Updated synthetic title")
        self.assertEqual(self.database.save(revised), 2)

        self.assertEqual(self.database.revisions("event", self.event.event_id), [1, 2])
        self.assertEqual(self.database.get("event", self.event.event_id), revised)
        self.assertEqual(
            self.database.get("event", self.event.event_id, revision=1), self.event
        )

        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    """
                    UPDATE record_revisions SET payload_json = '{}'
                    WHERE record_type = 'event' AND record_id = ? AND revision = 1
                    """,
                    (self.event.event_id,),
                )
        finally:
            connection.close()

    def test_current_record_projection_cannot_diverge_from_revisions(self):
        self.database.save(self.event)
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "must match"):
                connection.execute(
                    """
                    UPDATE canonical_records SET payload_json = '{}'
                    WHERE record_type = 'event' AND record_id = ?
                    """,
                    (self.event.event_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "must be revised"):
                connection.execute(
                    """
                    DELETE FROM canonical_records
                    WHERE record_type = 'event' AND record_id = ?
                    """,
                    (self.event.event_id,),
                )
        finally:
            connection.close()

    def test_raw_snapshots_are_content_addressed_and_immutable(self):
        content = b"Synthetic immutable source payload"
        snapshot = RawSnapshot.from_content(
            content,
            media_type="text/plain",
            created_at="2026-10-07T09:00:00Z",
        )
        reference = self.database.add_raw_snapshot(snapshot)
        self.assertEqual(reference, f"sha256:{snapshot.content_sha256}")
        self.assertEqual(self.database.add_raw_snapshot(snapshot), reference)
        self.assertEqual(self.database.get_raw_snapshot(reference), snapshot)

        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE raw_snapshots SET content = ? WHERE content_sha256 = ?",
                    (b"changed", snapshot.content_sha256),
                )
        finally:
            connection.close()
        invalid_snapshot = replace(snapshot, content_sha256="0" * 64)
        with self.assertRaisesRegex(StorageError, "does not match"):
            self.database.add_raw_snapshot(invalid_snapshot)
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM raw_snapshots WHERE content_sha256 = ?",
                    (snapshot.content_sha256,),
                )
        finally:
            connection.close()

    def test_source_must_reference_an_existing_raw_snapshot_by_digest(self):
        source = replace(self.source, raw_content_ref="sha256:" + "a" * 64)
        with self.assertRaisesRegex(StorageError, "snapshot that is not stored"):
            self.database.save(source)

    def test_source_can_reference_a_verified_immutable_snapshot(self):
        snapshot = RawSnapshot.from_content(
            b"Synthetic source body",
            media_type="text/plain",
            created_at="2026-10-07T09:00:00Z",
        )
        self.database.add_raw_snapshot(snapshot)
        source = replace(self.source, raw_content_ref=snapshot.content_ref)

        self.assertEqual(self.database.save(source), 1)
        self.assertEqual(self.database.get("source", source.source_id), source)

    def test_referenced_records_must_exist_and_belong_to_same_event(self):
        with self.assertRaisesRegex(StorageError, "event record .* does not exist"):
            self.database.save(self.claim)
        self.database.save(self.event)
        self.database.save(self.source)
        wrong_event_claim = replace(self.claim, event_id="CNVS-EVT-2026-10-08-0002")
        with self.assertRaisesRegex(StorageError, "event record .* does not exist"):
            self.database.save(wrong_event_claim)

        self.database.save(self.evidence)
        wrong_event_evidence = replace(
            self.evidence,
            evidence_id="EVD-OTHER-001",
            event_id="CNVS-EVT-2026-10-08-0002",
        )
        self.database.save(
            Event(
                schema_version="0.1",
                event_id="CNVS-EVT-2026-10-08-0002",
                event_type="MARITIME",
                title="Other synthetic event",
                description="Synthetic fixture.",
                status="ACTIVE",
                created_at="2026-10-07T08:00:00Z",
                updated_at="2026-10-07T09:00:00Z",
                start_time=None,
                end_time=None,
                location=None,
                country=None,
                coordinates=None,
                entities=(),
            )
        )
        self.database.save(wrong_event_evidence)
        mismatched_claim = replace(
            self.claim,
            supporting_evidence_ids=(wrong_event_evidence.evidence_id,),
        )
        with self.assertRaisesRegex(StorageError, "from a different event"):
            self.database.save(mismatched_claim)

    def test_assessment_reconstructs_its_exact_record_revisions(self):
        self.save_linked_records()
        self.assertEqual(self.database.save(self.assessment), 1)

        revised_event = replace(self.event, title="Revised after assessment")
        self.assertEqual(self.database.save(revised_event), 2)
        revised_source = replace(self.source, title="Revised source title")
        self.assertEqual(self.database.save(revised_source), 2)
        second_assessment = replace(
            self.assessment,
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            assessment_text="Revised synthetic assessment.",
        )
        self.assertEqual(self.database.save(second_assessment), 2)

        first = self.database.reconstruct_assessment(
            self.assessment.assessment_id, revision=1
        )
        second = self.database.reconstruct_assessment(self.assessment.assessment_id)
        self.assertEqual(first.assessment.assessment_text, self.assessment.assessment_text)
        self.assertEqual(first.records[("event", self.event.event_id)], self.event)
        self.assertEqual(first.records[("source", self.source.source_id)], self.source)
        self.assertEqual(second.records[("event", self.event.event_id)], revised_event)
        self.assertEqual(
            second.records[("source", self.source.source_id)], revised_source
        )
        self.assertEqual(second.assessment.assessment_text, "Revised synthetic assessment.")

        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "assessment revisions"):
                connection.execute(
                    """
                    UPDATE assessment_revisions SET record_set_json = '{}'
                    WHERE assessment_id = ? AND revision = 1
                    """,
                    (self.assessment.assessment_id,),
                )
        finally:
            connection.close()

    def test_database_cli_applies_migration_and_reports_status(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["db", "migrate", "--database", str(self.database_path)])
        self.assertEqual(result, 0)
        self.assertIn("6 migration(s) applied", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["db", "status", "--database", str(self.database_path)])
        self.assertEqual(result, 0)
        self.assertIn("001_initial applied at", output.getvalue())
        self.assertIn("002_collection_attempts applied at", output.getvalue())
        self.assertIn("003_normalized_documents applied at", output.getvalue())
        self.assertIn("004_event_timelines applied at", output.getvalue())
        self.assertIn("005_claim_extractions applied at", output.getvalue())
        self.assertIn("006_provenance_graph applied at", output.getvalue())

    def test_database_cli_reports_database_open_failures_without_traceback(self):
        output = io.StringIO()
        error_output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error_output):
            result = main(["db", "status", "--database", str(self.temp_dir.name)])

        self.assertEqual(result, 2)
        self.assertIn("cnvs: error:", error_output.getvalue())
        self.assertNotIn("Traceback", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
