import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cnvs.cli import _matrix_coverage_payload, main
from cnvs.configuration import load_country_coverage_catalog
from cnvs.models import Claim, Event, Evidence, Source
from cnvs.normalization import normalize_collection
from cnvs.storage import Database, StorageError
from cnvs.validation import parse_record


ROOT = Path(__file__).resolve().parents[1]
TIMES = (
    "2026-10-08T10:00:00Z",
    "2026-10-08T10:01:00Z",
    "2026-10-08T10:02:00Z",
    "2026-10-08T10:03:00Z",
)


def fixture_record(record_type: str, filename: str, **updates):
    payload = json.loads((ROOT / "examples" / filename).read_text(encoding="utf-8"))
    payload.update(updates)
    return parse_record(record_type, payload)


class NationalInformationMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "cnvs.sqlite3"
        self.database = Database(self.database_path)
        event = fixture_record(
            "event", "event.json", event_id="CNVS-EVT-2026-10-08-0003"
        )
        if not isinstance(event, Event):
            raise AssertionError("Event fixture did not parse.")
        self.event = event
        self.database.save(self.event)
        self.catalog = load_country_coverage_catalog(ROOT / "config")
        self.plan = self.database.create_country_coverage_plan(
            event_id=self.event.event_id,
            config_sha256=self.catalog.config_sha256,
            countries=[
                {"code": "PL", "name": "Poland", "languages": ["pl"]},
                {"code": "DE", "name": "Germany", "languages": ["de"]},
            ],
            outside_catalog=[
                {"country": "Atlantis", "reason": "Outside the reviewed MVP region."}
            ],
            proposed_by="analyst-a",
            proposed_at=TIMES[0],
            rationale="Select countries directly connected to the synthetic event.",
        )
        self.database.review_country_coverage_plan(
            plan_id=self.plan.plan_id,
            decision="ACCEPTED",
            reviewed_by="reviewer-a",
            reviewed_at=TIMES[1],
            rationale="The event-specific scope is justified.",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_document(
        self,
        source_id: str,
        country: str,
        language: str,
        source_class: str,
        *,
        link_decision: str = "LINKED",
    ) -> str:
        feed_url = f"https://{source_id.lower()}.example/feed"
        item_id = source_id.lower()
        content = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0"><channel><language>'
            f"{language}</language><item><guid>{item_id}</guid>"
            f"<title>{source_id} synthetic incident report</title>"
            f"<link>https://{source_id.lower()}.example/{item_id}</link>"
            "<description>Synthetic report about the matrix test incident.</description>"
            "</item></channel></rss>"
        ).encode()
        result = self.database.record_collection_result(
            source_id=source_id,
            source_metadata={
                "publisher": source_id,
                "country": country,
                "language": language,
                "source_class": source_class,
            },
            source_url=feed_url,
            status="SUCCEEDED",
            started_at=TIMES[0],
            completed_at=TIMES[1],
            final_url=feed_url,
            http_status=200,
            content_type="application/rss+xml",
            content=content,
            archive_content=True,
        )
        documents, _ = normalize_collection(self.database, result.collection_id)
        self.assertEqual(len(documents), 1)
        document = documents[0]
        match = self.database.propose_event_source_match(
            event_id=self.event.event_id,
            document_id=document.document_id,
            proposed_by="analyst-a",
            proposed_at=TIMES[2],
            rationale="The report describes the test event.",
        )
        self.database.review_event_source_match(
            match_id=match.match_id,
            decision=link_decision,
            reviewed_by="reviewer-a",
            reviewed_at=TIMES[3],
            rationale=f"Manual event-link decision: {link_decision}.",
        )
        return document.document_id

    def add_canonical_source(self, source_id: str, country: str, language: str):
        source = fixture_record(
            "source",
            "source.json",
            source_id=source_id,
            country=country,
            language=language,
        )
        if not isinstance(source, Source):
            raise AssertionError("Source fixture did not parse.")
        self.database.save(source)

    def test_matrix_aggregates_only_confirmed_links_and_verified_evidence(self):
        self.add_document(
            "SRC-MATRIX-PL", "PL", "pl", "INSTITUTIONAL_STATEMENT"
        )
        self.add_document("SRC-MATRIX-DE", "DE", "de", "PRIMARY_OBSERVATION")
        self.add_document(
            "SRC-MATRIX-UNLINKED", "PL", "pl", "NEWS_REPORT", link_decision="UNRESOLVED"
        )
        self.add_canonical_source("SRC-MATRIX-PL", "PL", "pl")
        claim = fixture_record(
            "claim",
            "claim.json",
            claim_id="CLM-MATRIX-PL",
            event_id=self.event.event_id,
            source_id="SRC-MATRIX-PL",
            supporting_evidence_ids=[],
            contradicting_evidence_ids=[],
        )
        evidence = fixture_record(
            "evidence",
            "evidence.json",
            evidence_id="EVD-MATRIX-PL",
            event_id=self.event.event_id,
            source_id="SRC-MATRIX-PL",
            verification_status="UNVERIFIED",
        )
        if not isinstance(claim, Claim) or not isinstance(evidence, Evidence):
            raise AssertionError("Claim/evidence fixtures did not parse.")
        self.database.save(claim)
        self.database.record_evidence(evidence)
        self.database.review_evidence(
            evidence_id=evidence.evidence_id,
            verification_status="VERIFIED",
            reviewed_by="reviewer-a",
            reviewed_at=TIMES[3],
            rationale="Synthetic evidence fixture was checked.",
        )
        relation = self.database.propose_claim_evidence_link(
            claim_id=claim.claim_id,
            evidence_id=evidence.evidence_id,
            relationship="SUPPORTS",
            proposed_by="analyst-a",
            proposed_at=TIMES[2],
            rationale="The direct observation supports the synthetic claim.",
        )
        self.database.review_claim_evidence_link(
            link_id=relation.link_id,
            decision="LINKED",
            reviewed_by="reviewer-a",
            reviewed_at=TIMES[3],
            rationale="The relationship was reviewed.",
        )
        assignment = self.database.propose_independence_assignment(
            member_type="SOURCE",
            member_id="SRC-MATRIX-PL",
            group_id="IG-MATRIX-PL-1",
            proposed_by="analyst-a",
            proposed_at=TIMES[2],
            rationale="The reviewed source is one reporting chain.",
        )
        self.database.review_independence_assignment(
            assignment_id=assignment.assignment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer-a",
            reviewed_at=TIMES[3],
            rationale="The source membership is supported.",
        )

        matrix = self.database.national_information_matrix(
            event_id=self.event.event_id,
            config_sha256=self.catalog.config_sha256,
        )
        poland, germany = matrix.cells
        self.assertTrue(matrix.config_matches)
        self.assertEqual(poland.reporting_documents, 1)
        self.assertEqual(poland.reporting_sources, 1)
        self.assertEqual(poland.institutional_sources, 1)
        self.assertEqual(poland.primary_evidence, 1)
        self.assertEqual(poland.claims, 1)
        self.assertEqual(poland.supporting_relations, 1)
        self.assertEqual(poland.accepted_independence_groups, ("IG-MATRIX-PL-1",))
        self.assertEqual(germany.reporting_documents, 1)
        self.assertEqual(germany.reporting_sources, 1)
        self.assertEqual(germany.primary_observation_sources, 1)
        self.assertEqual(germany.primary_evidence, 0)
        self.assertEqual(germany.uncovered_languages, ())
        self.assertTrue(any("No verified primary-directness" in gap for gap in germany.coverage_gaps))

    def test_language_review_conflicts_do_not_count_as_coverage(self):
        self.add_document(
            "SRC-MATRIX-LANGUAGE-CONFLICT", "PL", "de", "NEWS_REPORT"
        )
        matrix = self.database.national_information_matrix(
            event_id=self.event.event_id,
            config_sha256=self.catalog.config_sha256,
        )
        poland = matrix.cells[0]
        self.assertEqual(poland.reporting_documents, 1)
        self.assertEqual(poland.reporting_sources, 1)
        self.assertEqual(poland.uncovered_languages, ("pl",))
        self.assertTrue(
            any("No confirmed original-language document for pl." in gap
                for gap in poland.coverage_gaps)
        )

    def test_assessments_require_review_and_latest_accepted_record_is_displayed(self):
        self.add_document("SRC-MATRIX-PL", "PL", "pl", "NEWS_REPORT")
        self.add_canonical_source("SRC-MATRIX-PL", "PL", "pl")
        assessment = self.database.record_country_matrix_assessment(
            plan_id=self.plan.plan_id,
            country_code="PL",
            config_sha256=self.catalog.config_sha256,
            dominant_frame="Infrastructure disruption",
            attribution_summary="Attribution remains unconfirmed.",
            occurrence_confidence="MEDIUM",
            method_confidence="LOW",
            attribution_confidence="UNKNOWN",
            omissions="No independent technical inspection.",
            contradictions="No reviewed direct contradiction.",
            supporting_source_ids=["SRC-MATRIX-PL"],
            supporting_claim_ids=[],
            supporting_evidence_ids=[],
            analyst="analyst-a",
            created_at=TIMES[2],
            rationale="Confidence dimensions are analyst judgments, not volume scores.",
        )
        with self.assertRaisesRegex(StorageError, "already has a pending"):
            self.database.record_country_matrix_assessment(
                plan_id=self.plan.plan_id,
                country_code="PL",
                config_sha256=self.catalog.config_sha256,
                dominant_frame="Duplicate pending assessment",
                attribution_summary="Unknown.",
                occurrence_confidence="UNKNOWN",
                method_confidence="UNKNOWN",
                attribution_confidence="UNKNOWN",
                omissions="None.",
                contradictions="None.",
                supporting_source_ids=["SRC-MATRIX-PL"],
                supporting_claim_ids=[],
                supporting_evidence_ids=[],
                analyst="analyst-a",
                created_at=TIMES[3],
                rationale="This second pending proposal must be rejected.",
            )
        accepted = self.database.review_country_matrix_assessment(
            assessment_id=assessment.assessment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer-a",
            reviewed_at=TIMES[3],
            rationale="The assessment is appropriately qualified.",
        )
        self.assertEqual(accepted.status, "ACCEPTED")
        matrix = self.database.national_information_matrix(
            event_id=self.event.event_id,
            config_sha256=self.catalog.config_sha256,
        )
        self.assertEqual(matrix.cells[0].assessment, accepted)
        pending = self.database.record_country_matrix_assessment(
            plan_id=self.plan.plan_id,
            country_code="PL",
            config_sha256=self.catalog.config_sha256,
            dominant_frame="Pending revision",
            attribution_summary="Still unknown.",
            occurrence_confidence="UNKNOWN",
            method_confidence="UNKNOWN",
            attribution_confidence="UNKNOWN",
            omissions="No additional material.",
            contradictions="No additional contradictions.",
            supporting_source_ids=["SRC-MATRIX-PL"],
            supporting_claim_ids=[],
            supporting_evidence_ids=[],
            analyst="analyst-a",
            created_at=TIMES[3],
            rationale="Propose a revised assessment without hiding the accepted one.",
        )
        matrix = self.database.national_information_matrix(
            event_id=self.event.event_id,
            config_sha256=self.catalog.config_sha256,
        )
        self.assertEqual(matrix.cells[0].assessment, accepted)
        revised = self.database.review_country_matrix_assessment(
            assessment_id=pending.assessment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer-a",
            reviewed_at=TIMES[3],
            rationale="The newer assessment is now reviewed.",
        )
        self.assertEqual(revised.status, "ACCEPTED")
        self.assertEqual(
            self.database.country_matrix_assessments(
                assessment_id=accepted.assessment_id
            )[0].status,
            "UNRESOLVED",
        )
        matrix = self.database.national_information_matrix(
            event_id=self.event.event_id,
            config_sha256=self.catalog.config_sha256,
        )
        self.assertEqual(matrix.cells[0].assessment, revised)

    def test_plan_and_assessment_rows_are_append_only(self):
        replacement = self.database.create_country_coverage_plan(
            event_id=self.event.event_id,
            config_sha256=self.catalog.config_sha256,
            countries=[{"code": "PL", "name": "Poland", "languages": ["pl"]}],
            outside_catalog=[],
            proposed_by="analyst-a",
            proposed_at=TIMES[2],
            rationale="Narrowed event-specific coverage.",
        )
        self.database.review_country_coverage_plan(
            plan_id=replacement.plan_id,
            decision="ACCEPTED",
            reviewed_by="reviewer-a",
            reviewed_at=TIMES[3],
            rationale="The narrower scope is justified.",
        )
        plans = self.database.country_coverage_plans(event_id=self.event.event_id)
        self.assertEqual([plan.status for plan in plans], ["SUPERSEDED", "ACCEPTED"])
        self.assertEqual(plans[0].countries[0]["code"], "PL")
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE event_country_coverage_plans SET rationale = 'changed' "
                    "WHERE plan_id = ?",
                    (plans[0].plan_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM event_country_coverage_reviews WHERE plan_id = ?",
                    (plans[0].plan_id,),
                )
        finally:
            connection.close()

    def test_plan_and_assessment_cli_workflow(self):
        plan_path = self.root / "plan.json"
        plan_path.write_text(
            json.dumps({"countries": [{"code": "PL", "languages": ["pl"]}]}),
            encoding="utf-8",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "matrix",
                    "plan",
                    self.event.event_id,
                    "--file",
                    str(plan_path),
                    "--proposed-by",
                    "analyst-a",
                    "--rationale",
                    "The event is in Poland.",
                    "--database",
                    str(self.database_path),
                    "--config-dir",
                    str(ROOT / "config"),
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("revision=2 status=PENDING", output.getvalue())
        newest = self.database.country_coverage_plans(event_id=self.event.event_id)[-1]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "matrix",
                        "review",
                        newest.plan_id,
                        "--decision",
                        "ACCEPTED",
                        "--reviewer",
                        "reviewer-a",
                        "--rationale",
                        "The event-specific country scope is justified.",
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        assessment_path = self.root / "assessment.json"
        assessment_path.write_text(
            json.dumps(
                {
                    "dominant_frame": "No established frame",
                    "attribution_summary": "Attribution remains unknown.",
                    "occurrence_confidence": "UNKNOWN",
                    "method_confidence": "UNKNOWN",
                    "attribution_confidence": "UNKNOWN",
                    "omissions": "No linked reporting is available.",
                    "contradictions": "No reviewed contradictions are available.",
                }
            ),
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "matrix",
                        "assess",
                        newest.plan_id,
                        "PL",
                        "--file",
                        str(assessment_path),
                        "--analyst",
                        "analyst-a",
                        "--rationale",
                        "The absence of sources prevents narrative characterization.",
                        "--database",
                        str(self.database_path),
                        "--config-dir",
                        str(ROOT / "config"),
                    ]
                ),
                0,
            )
        assessment = self.database.country_matrix_assessments(
            plan_id=newest.plan_id
        )[0]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "matrix",
                        "review-assessment",
                        assessment.assessment_id,
                        "--decision",
                        "ACCEPTED",
                        "--reviewer",
                        "reviewer-a",
                        "--rationale",
                        "The explicit unknown values are appropriately cautious.",
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "matrix",
                        "show",
                        self.event.event_id,
                        "--database",
                        str(self.database_path),
                        "--config-dir",
                        str(ROOT / "config"),
                    ]
                ),
                0,
            )
        self.assertIn("No confirmed event-linked reporting documents.", output.getvalue())
        self.assertIn("PL Poland", output.getvalue())
        self.assertIn("Attribution remains unknown.", output.getvalue())

    def test_plan_rejects_languages_not_allowed_for_country(self):
        path = self.root / "invalid-plan.json"
        path.write_text(
            json.dumps({"countries": [{"code": "PL", "languages": ["de"]}]}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "subset of its approved languages"):
            _matrix_coverage_payload(str(path), self.catalog)

    def test_configuration_digest_mismatch_is_visible_and_blocks_new_assessment(self):
        changed_digest = "0" * 64
        matrix = self.database.national_information_matrix(
            event_id=self.event.event_id, config_sha256=changed_digest
        )
        self.assertFalse(matrix.config_matches)
        with self.assertRaisesRegex(StorageError, "configuration changed"):
            self.database.record_country_matrix_assessment(
                plan_id=self.plan.plan_id,
                country_code="PL",
                config_sha256=changed_digest,
                dominant_frame="Unknown",
                attribution_summary="Unknown",
                occurrence_confidence="UNKNOWN",
                method_confidence="UNKNOWN",
                attribution_confidence="UNKNOWN",
                omissions="Unknown",
                contradictions="Unknown",
                supporting_source_ids=[],
                supporting_claim_ids=[],
                supporting_evidence_ids=[],
                analyst="analyst-a",
                created_at=TIMES[2],
                rationale="No sources are currently available for this country.",
            )


if __name__ == "__main__":
    unittest.main()
