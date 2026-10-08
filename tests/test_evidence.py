import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cnvs.cli import main
from cnvs.models import Claim, Event, Evidence, EvidenceGap, Source
from cnvs.storage import Database, StorageError
from cnvs.validation import parse_record


ROOT = Path(__file__).resolve().parents[1]
TIMES = (
    "2026-10-08T10:00:00Z",
    "2026-10-08T10:01:00Z",
    "2026-10-08T10:02:00Z",
    "2026-10-08T10:03:00Z",
    "2026-10-08T10:04:00Z",
)


def fixture_record(record_type: str, filename: str, **updates):
    payload = json.loads((ROOT / "examples" / filename).read_text(encoding="utf-8"))
    payload.update(updates)
    return parse_record(record_type, payload)


class EvidenceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cnvs.sqlite3"
        self.database = Database(self.database_path)
        self.event = fixture_record(
            "event", "event.json", event_id="CNVS-EVT-2026-10-08-0002"
        )
        self.source = fixture_record(
            "source", "source.json", source_id="SRC-EVIDENCE-001"
        )
        self.evidence = fixture_record(
            "evidence",
            "evidence.json",
            evidence_id="EVD-EVIDENCE-001",
            event_id=self.event.event_id,
            source_id=self.source.source_id,
        )
        self.claim = fixture_record(
            "claim",
            "claim.json",
            claim_id="CLM-EVIDENCE-001",
            event_id=self.event.event_id,
            source_id=self.source.source_id,
            supporting_evidence_ids=[],
            contradicting_evidence_ids=[],
        )
        if not all(
            isinstance(record, expected)
            for record, expected in (
                (self.event, Event),
                (self.source, Source),
                (self.evidence, Evidence),
                (self.claim, Claim),
            )
        ):
            raise AssertionError("Evidence workflow fixtures parsed incorrectly.")
        self.database.save(self.event)
        self.database.save(self.source)
        self.database.record_evidence(self.evidence)
        self.database.save(self.claim)

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_evidence(self, evidence_id: str) -> Evidence:
        evidence = fixture_record(
            "evidence",
            "evidence.json",
            evidence_id=evidence_id,
            event_id=self.event.event_id,
            source_id=self.source.source_id,
        )
        if not isinstance(evidence, Evidence):
            raise AssertionError("Evidence fixture did not parse.")
        self.database.record_evidence(evidence)
        return evidence

    def propose_link(self, evidence_id: str, relationship: str):
        return self.database.propose_claim_evidence_link(
            claim_id=self.claim.claim_id,
            evidence_id=evidence_id,
            relationship=relationship,
            proposed_by="analyst-a",
            proposed_at=TIMES[0],
            rationale=f"Reviewing the evidence as {relationship.lower()}.",
        )

    def review_link(self, link_id: str, decision: str = "LINKED"):
        return self.database.review_claim_evidence_link(
            link_id=link_id,
            decision=decision,
            reviewed_by="reviewer-b",
            reviewed_at=TIMES[1],
            rationale=f"Reviewer decision: {decision.lower()}.",
        )

    def test_evidence_origin_times_and_effective_verification_are_auditable(self):
        self.assertEqual(self.database.get("evidence", self.evidence.evidence_id), self.evidence)
        self.assertEqual(self.evidence.directness, "PRIMARY")
        self.assertEqual(self.evidence.event_time, "2026-10-07T06:30:00Z")
        self.assertEqual(self.evidence.collection_time, "2026-10-07T07:30:00Z")

        note = self.database.add_evidence_note(
            evidence_id=self.evidence.evidence_id,
            analyst="analyst-a",
            noted_at=TIMES[0],
            note="The file depicts an identifiable physical feature.",
            rationale="Describe only what is directly visible.",
        )
        self.assertEqual(self.database.evidence_notes(self.evidence.evidence_id), [note])
        review = self.database.review_evidence(
            evidence_id=self.evidence.evidence_id,
            verification_status="IN_REVIEW",
            reviewed_by="reviewer-a",
            reviewed_at=TIMES[0],
            rationale="Checking the primary material provenance.",
            analyst_note="The image metadata still needs review.",
        )
        self.assertEqual(review.verification_status, "IN_REVIEW")
        self.assertEqual(
            self.database.evidence_verification_status(self.evidence.evidence_id),
            "IN_REVIEW",
        )
        self.database.review_evidence(
            evidence_id=self.evidence.evidence_id,
            verification_status="VERIFIED",
            reviewed_by="reviewer-b",
            reviewed_at=TIMES[1],
            rationale="The source snapshot and collection record match.",
        )
        history = self.database.evidence_review_history(self.evidence.evidence_id)
        self.assertEqual(
            [item.verification_status for item in history], ["IN_REVIEW", "VERIFIED"]
        )
        self.assertEqual(history[0].analyst_note, "The image metadata still needs review.")
        self.assertEqual(self.database.get("evidence", self.evidence.evidence_id), self.evidence)

        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM evidence_verification_reviews WHERE review_id = ?",
                    (review.review_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM evidence_notes WHERE note_id = ?",
                    (note.note_id,),
                )
        finally:
            connection.close()

    def test_claim_evidence_links_require_review_and_keep_complete_history(self):
        link = self.propose_link(self.evidence.evidence_id, "SUPPORTS")
        self.assertEqual(link.review_status, "PENDING")
        self.assertEqual(
            self.database.claim_evidence_links(
                claim_id=self.claim.claim_id, review_status="LINKED"
            ),
            [],
        )
        linked = self.review_link(link.link_id)
        self.assertEqual(linked.review_status, "LINKED")
        self.assertEqual(linked.reviewed_by, "reviewer-b")
        self.assertEqual(
            [item.review_status for item in self.database.claim_evidence_link_history(link.link_id)],
            ["PENDING", "LINKED"],
        )
        with self.assertRaisesRegex(StorageError, "pending or linked"):
            self.propose_link(self.evidence.evidence_id, "CONTRADICTS")

        self.review_link(link.link_id, "UNRESOLVED")
        corrected = self.propose_link(self.evidence.evidence_id, "NOT_DIRECTLY_RELEVANT")
        self.assertEqual(corrected.review_status, "PENDING")

    def test_cross_event_relations_are_rejected(self):
        other_event = fixture_record(
            "event", "event.json", event_id="CNVS-EVT-2026-10-08-0003"
        )
        other_evidence = fixture_record(
            "evidence",
            "evidence.json",
            evidence_id="EVD-EVIDENCE-OTHER",
            event_id=other_event.event_id,
            source_id=self.source.source_id,
        )
        self.database.save(other_event)
        self.database.record_evidence(other_evidence)
        with self.assertRaisesRegex(StorageError, "same event"):
            self.propose_link(other_evidence.evidence_id, "SUPPORTS")

        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "same event"):
                connection.execute(
                    """
                    INSERT INTO claim_evidence_links (
                        link_id, event_id, claim_id, evidence_id, relationship,
                        proposed_by, proposed_at, rationale
                    ) VALUES (
                        'cross-event-link', ?, ?, ?, 'SUPPORTS', 'analyst',
                        ?, 'Direct database invariant test.'
                    )
                    """,
                    (
                        other_event.event_id,
                        self.claim.claim_id,
                        self.evidence.evidence_id,
                        TIMES[0],
                    ),
                )
        finally:
            connection.close()

    def test_missing_insufficient_and_conflicting_gaps_are_explicit_and_reviewed(self):
        missing = self.database.record_evidence_gap(
            event_id=self.event.event_id,
            claim_id=self.claim.claim_id,
            gap_type="MISSING",
            description="No independently collected imagery is available.",
            created_by="analyst-a",
            created_at=TIMES[0],
            rationale="The claim concerns a physical event requiring direct observation.",
        )
        insufficient = self.database.record_evidence_gap(
            event_id=self.event.event_id,
            claim_id=self.claim.claim_id,
            gap_type="INSUFFICIENT",
            description="Current evidence does not establish the time of damage.",
            created_by="analyst-a",
            created_at=TIMES[0],
            rationale="The only timestamp is a publisher time, not event time.",
        )
        self.assertEqual(missing.status, "OPEN")
        self.assertEqual(insufficient.status, "OPEN")
        resolved = self.database.review_evidence_gap(
            gap_id=missing.gap_id,
            decision="RESOLVED",
            reviewed_by="reviewer-b",
            reviewed_at=TIMES[1],
            rationale="A new direct observation is now linked.",
        )
        self.assertEqual(resolved.status, "RESOLVED")
        self.database.review_evidence_gap(
            gap_id=missing.gap_id,
            decision="REOPENED",
            reviewed_by="reviewer-c",
            reviewed_at=TIMES[2],
            rationale="The newly linked observation cannot establish the missing detail.",
        )
        self.assertEqual(
            [item.status for item in self.database.evidence_gap_history(missing.gap_id)],
            ["OPEN", "RESOLVED", "OPEN"],
        )

        contradicting_evidence = self.add_evidence("EVD-EVIDENCE-CONTRADICTS")
        supports = self.review_link(self.propose_link(
            self.evidence.evidence_id, "SUPPORTS"
        ).link_id)
        contradicts = self.review_link(self.propose_link(
            contradicting_evidence.evidence_id, "CONTRADICTS"
        ).link_id)
        conflict = self.database.record_evidence_gap(
            event_id=self.event.event_id,
            claim_id=self.claim.claim_id,
            gap_type="CONFLICTING",
            description="Two linked observations make incompatible statements.",
            created_by="analyst-a",
            created_at=TIMES[2],
            rationale="Both relationships were separately reviewed.",
            supporting_link_id=supports.link_id,
            contradicting_link_id=contradicts.link_id,
        )
        self.assertEqual(conflict.gap_type, "CONFLICTING")
        self.assertEqual(
            self.database.evidence_gaps(
                event_id=self.event.event_id, gap_type="CONFLICTING"
            ),
            [conflict],
        )
        with self.assertRaisesRegex(StorageError, "confirmed SUPPORTS"):
            self.database.record_evidence_gap(
                event_id=self.event.event_id,
                claim_id=self.claim.claim_id,
                gap_type="CONFLICTING",
                description="Unlinked conflict must not be accepted.",
                created_by="analyst-a",
                created_at=TIMES[3],
                rationale="This intentionally lacks a reviewed relation.",
                supporting_link_id=supports.link_id,
                contradicting_link_id="missing-link",
            )
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "conflicts need linked support"):
                connection.execute(
                    """
                    INSERT INTO evidence_gaps (
                        gap_id, event_id, claim_id, gap_type, description,
                        created_by, created_at, rationale, supporting_link_id,
                        contradicting_link_id
                    ) VALUES (
                        'invalid-conflict', ?, ?, 'CONFLICTING',
                        'A database-level conflict invariant test.', 'analyst',
                        ?, 'An identical link cannot contradict itself.', ?, ?
                    )
                    """,
                    (
                        self.event.event_id,
                        self.claim.claim_id,
                        TIMES[3],
                        supports.link_id,
                        supports.link_id,
                    ),
                )
        finally:
            connection.close()

    def test_evidence_cli_creates_lists_reviews_and_records_gaps(self):
        source_payload = json.loads(
            (ROOT / "examples" / "source.json").read_text(encoding="utf-8")
        )
        source_payload["source_id"] = "SRC-CLI-001"
        source_file = Path(self.temp_dir.name) / "source.json"
        source_file.write_text(json.dumps(source_payload), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main([
                "source", "record", "--file", str(source_file),
                "--database", str(self.database_path),
            ])
        self.assertEqual(result, 0)
        self.assertIn("Stored source SRC-CLI-001 at revision 1", output.getvalue())

        claim_payload = json.loads(
            (ROOT / "examples" / "claim.json").read_text(encoding="utf-8")
        )
        claim_payload.update(
            claim_id="CLM-CLI-001",
            event_id=self.event.event_id,
            source_id="SRC-CLI-001",
            supporting_evidence_ids=[],
            contradicting_evidence_ids=[],
        )
        claim_file = Path(self.temp_dir.name) / "claim.json"
        claim_file.write_text(json.dumps(claim_payload), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main([
                "claim", "record", "--file", str(claim_file),
                "--database", str(self.database_path),
            ])
        self.assertEqual(result, 0)
        self.assertIn("Stored claim CLM-CLI-001 at revision 1", output.getvalue())

        payload = json.loads((ROOT / "examples" / "evidence.json").read_text(encoding="utf-8"))
        payload.update(
            evidence_id="EVD-CLI-001",
            event_id=self.event.event_id,
            source_id="SRC-CLI-001",
        )
        evidence_file = Path(self.temp_dir.name) / "evidence.json"
        evidence_file.write_text(json.dumps(payload), encoding="utf-8")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main([
                "evidence", "create", "--file", str(evidence_file),
                "--database", str(self.database_path),
            ])
        self.assertEqual(result, 0)
        self.assertIn("EVD-CLI-001", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main([
                "evidence", "review", "EVD-CLI-001", "--status", "VERIFIED",
                "--reviewer", "reviewer", "--rationale", "Provenance inspected.",
                "--database", str(self.database_path),
            ])
        self.assertEqual(result, 0)
        self.assertIn("verification=VERIFIED", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main([
                "evidence", "list", "--event-id", self.event.event_id,
                "--database", str(self.database_path),
            ])
        self.assertEqual(result, 0)
        self.assertIn("EVD-CLI-001", output.getvalue())
        self.assertIn("verification=VERIFIED", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main([
                "evidence", "note", "EVD-CLI-001", "--analyst", "analyst",
                "--note", "The material was inspected.", "--rationale",
                "Record the observation separately from verification.",
                "--database", str(self.database_path),
            ])
        self.assertEqual(result, 0)
        self.assertIn("analyst=analyst", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main([
                "evidence", "link", "CLM-CLI-001", "EVD-CLI-001",
                "--relationship", "CONTRADICTS", "--proposed-by", "analyst",
                "--rationale", "The observation is inconsistent with the claim.",
                "--database", str(self.database_path),
            ])
        self.assertEqual(result, 0)
        link = self.database.claim_evidence_links(
            claim_id="CLM-CLI-001", evidence_id="EVD-CLI-001"
        )[0]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main([
                "evidence", "review-link", link.link_id, "--decision", "LINKED",
                "--reviewer", "reviewer", "--rationale",
                "The contradiction is supported by the cited material.",
                "--database", str(self.database_path),
            ])
        self.assertEqual(result, 0)
        self.assertIn("relationship=CONTRADICTS status=LINKED", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main([
                "evidence", "gap", self.event.event_id, "--type", "MISSING",
                "--description", "No direct observation was collected.",
                "--analyst", "analyst", "--rationale", "Expected a direct observation.",
                "--claim-id", "CLM-CLI-001", "--database", str(self.database_path),
            ])
        self.assertEqual(result, 0)
        self.assertIn("type=MISSING status=OPEN", output.getvalue())


if __name__ == "__main__":
    unittest.main()
