import contextlib
import html
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cnvs.cli import main
from cnvs.configuration import load_country_coverage_catalog
from cnvs.models import Assessment, Claim, Evidence, Event, Source
from cnvs.normalization import normalize_collection
from cnvs.reporting import create_report, render_html, render_markdown
from cnvs.storage import Database, StorageError
from cnvs.validation import parse_record


ROOT = Path(__file__).resolve().parents[1]
TIMESTAMP = "2026-10-08T12:00:00Z"


def _example(record_type: str, filename: str, **updates):
    payload = json.loads((ROOT / "examples" / filename).read_text(encoding="utf-8"))
    payload.update(updates)
    return parse_record(record_type, payload)


class AnalystReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "reports.sqlite3"
        self.database = Database(self.database_path)
        self.event = _example("event", "event.json")
        self.source = _example("source", "source.json")
        self.evidence = _example("evidence", "evidence.json")
        self.claim = _example("claim", "claim.json")
        self.assessment = _example("assessment", "assessment.json")
        assert isinstance(self.event, Event)
        assert isinstance(self.source, Source)
        assert isinstance(self.evidence, Evidence)
        assert isinstance(self.claim, Claim)
        assert isinstance(self.assessment, Assessment)
        for record in (
            self.event,
            self.source,
            self.evidence,
            self.claim,
            self.assessment,
        ):
            self.database.save(record)
        self.catalog = load_country_coverage_catalog(ROOT / "config")

    def tearDown(self):
        self.temp_dir.cleanup()

    def create(self, *, high_impact: bool = False, revision: int | None = None) -> str:
        report = create_report(
            self.database,
            assessment_id=self.assessment.assessment_id,
            assessment_revision=revision,
            high_impact=high_impact,
            config_sha256=self.catalog.config_sha256,
            generated_at=TIMESTAMP,
        )
        return report.report_id

    def add_document(
        self, item_id: str, title: str, *, source_id: str | None = None
    ) -> str:
        source_id = source_id or self.source.source_id
        feed_url = f"https://example.invalid/{item_id}/feed"
        content = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0"><channel><language>en</language><item>'
            f"<guid>{item_id}</guid><title>{title}</title>"
            f"<link>https://example.invalid/{item_id}</link>"
            "<description>Synthetic analyst report fixture.</description>"
            "</item></channel></rss>"
        ).encode()
        collection = self.database.record_collection_result(
            source_id=source_id,
            source_metadata={
                "publisher": source_id,
                "country": "PL",
                "language": "en",
                "source_class": self.source.source_class,
            },
            source_url=feed_url,
            status="SUCCEEDED",
            started_at=TIMESTAMP,
            completed_at=TIMESTAMP,
            final_url=feed_url,
            http_status=200,
            content_type="application/rss+xml",
            content=content,
            archive_content=True,
        )
        documents, _ = normalize_collection(self.database, collection.collection_id)
        self.assertEqual(len(documents), 1)
        return documents[0].document_id

    def test_report_contains_required_sections_uncertainty_and_pinned_revision(self):
        revised_event = replace(self.event, title="A later event title")
        self.database.save(revised_event)
        report_id = self.create(revision=1)
        report = self.database.get_report(report_id)

        for heading in (
            "FACT",
            "CLAIM",
            "ASSESSMENT",
            "CONFIDENCE",
            "GAP",
            "SOURCE PROVENANCE & INDEPENDENCE",
            "CONTRADICTIONS",
            "TIMELINE",
            "RECORD REVISIONS",
        ):
            self.assertIn(f"## {heading}", report.markdown)
            self.assertIn(f"<h2>{html.escape(heading)}</h2>", report.html)
        self.assertIn(self.event.title, report.markdown)
        self.assertNotIn("A later event title", report.markdown)
        self.assertIn("occurrence: UNKNOWN", report.markdown)
        self.assertIn("method: UNKNOWN", report.markdown)
        self.assertIn("attribution: UNKNOWN", report.markdown)
        snapshot = json.loads(report.input_snapshot_json)
        references = snapshot["canonical_assessment_snapshot"]["revisions"]
        event_revision = next(
            item["revision"]
            for item in references
            if item["record_type"] == "event"
        )
        self.assertEqual(event_revision, 1)
        self.assertEqual(report.assessment_revision, 1)
        self.assertIn("claim CLM-EX-001 revision 1", report.html)
        self.assertIn("evidence EVD-EX-001 revision 1", report.html)
        self.assertIn("source SRC-EX-001 revision 1", report.html)

    def test_html_escapes_untrusted_content_and_rejects_unsafe_link_schemes(self):
        hostile_event = replace(
            self.event,
            title='<script>alert("x")</script>',
        )
        hostile_source = replace(
            self.source,
            url="javascript:alert(1)",
            title="<img src=x onerror=alert(1)>",
        )
        self.database.save(hostile_event)
        self.database.save(hostile_source)
        revised_assessment = replace(
            self.assessment,
            created_at="2026-10-08T11:00:00Z",
            facts=("<script>fact</script>",),
        )
        self.database.save(revised_assessment)
        report_id = self.create()
        report = self.database.get_report(report_id)

        self.assertNotIn("<script>", report.html)
        self.assertNotIn("<script>", report.markdown)
        self.assertIn("&lt;script&gt;", report.html)
        self.assertNotIn('href="javascript:', report.html)
        self.assertNotIn("](<javascript:", report.markdown)

    def test_rendering_from_the_stored_snapshot_is_byte_reproducible(self):
        report_id = self.create(high_impact=True)
        report = self.database.get_report(report_id)
        snapshot = json.loads(report.input_snapshot_json)["report"]
        rendered_snapshot = {
            "title": snapshot["title"],
            "generated_at": snapshot["generated_at"],
            "assessment_id": snapshot["assessment_id"],
            "assessment_revision": snapshot["assessment_revision"],
            "sections": snapshot["sections"],
        }
        self.assertEqual(report.markdown, render_markdown(rendered_snapshot))
        self.assertEqual(report.html, render_html(rendered_snapshot))
        self.assertEqual(report.status, "PENDING")
        self.assertTrue(report.high_impact)

    def test_country_outliers_and_reviewed_independence_are_reported(self):
        plan = self.database.create_country_coverage_plan(
            event_id=self.event.event_id,
            config_sha256=self.catalog.config_sha256,
            countries=[
                {"code": "PL", "name": "Poland", "languages": ["pl"]}
            ],
            outside_catalog=[
                {"country": "Exampleland", "reason": "Relevant transit location"}
            ],
            proposed_by="analyst",
            proposed_at=TIMESTAMP,
            rationale="Synthetic country coverage fixture.",
        )
        self.database.review_country_coverage_plan(
            plan_id=plan.plan_id,
            decision="ACCEPTED",
            reviewed_by="reviewer",
            reviewed_at=TIMESTAMP,
            rationale="Coverage scope reviewed.",
        )
        evidence_link = self.database.propose_claim_evidence_link(
            claim_id=self.claim.claim_id,
            evidence_id=self.evidence.evidence_id,
            relationship="SUPPORTS",
            proposed_by="analyst",
            proposed_at=TIMESTAMP,
            rationale="The observation supports the attributed proposition.",
        )
        self.database.review_claim_evidence_link(
            link_id=evidence_link.link_id,
            decision="LINKED",
            reviewed_by="reviewer",
            reviewed_at=TIMESTAMP,
            rationale="The evidence relation was reviewed.",
        )
        upstream_source = replace(
            self.source,
            source_id="SRC-ORIGIN-001",
            publisher="Synthetic upstream publisher",
        )
        self.database.save(upstream_source)
        upstream_document = self.add_document(
            "origin-001", "Origin report", source_id=upstream_source.source_id
        )
        linked_document = self.add_document("copy-001", "Syndicated report")
        match = self.database.propose_event_source_match(
            event_id=self.event.event_id,
            document_id=linked_document,
            proposed_by="analyst",
            proposed_at=TIMESTAMP,
            rationale="The report concerns the synthetic event.",
        )
        self.database.review_event_source_match(
            match_id=match.match_id,
            decision="LINKED",
            reviewed_by="reviewer",
            reviewed_at=TIMESTAMP,
            rationale="Event link confirmed.",
        )
        provenance_link = self.database.propose_provenance_link(
            document_id=linked_document,
            upstream_document_id=upstream_document,
            relationship_type="SYNDICATED",
            proposed_by="analyst",
            proposed_at=TIMESTAMP,
            rationale="The copy credits the originating report.",
        )
        self.database.review_provenance_link(
            link_id=provenance_link.link_id,
            decision="CONFIRMED",
            reviewed_by="reviewer",
            reviewed_at=TIMESTAMP,
            rationale="The byline confirms the source chain.",
        )
        document_assignment = self.database.propose_independence_assignment(
            group_id="IG-DEPENDENT-COPY",
            member_type="DOCUMENT",
            member_id=linked_document,
            proposed_by="analyst",
            proposed_at=TIMESTAMP,
            rationale="The linked document republishes the origin.",
        )
        self.database.review_independence_assignment(
            assignment_id=document_assignment.assignment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer",
            reviewed_at=TIMESTAMP,
            rationale="The confirmed source chain establishes dependence.",
            supporting_link_ids=[provenance_link.link_id],
        )

        report = self.database.get_report(self.create())
        self.assertIn("Exampleland outside catalog", report.markdown)
        self.assertIn("CLAIM/EVIDENCE LINKS", report.markdown)
        self.assertIn("SUPPORTS", report.markdown)
        self.assertIn("Relevant transit location", report.markdown)
        self.assertIn("accepted independence assignment", report.markdown)
        self.assertIn(r"IG\-DEPENDENT\-COPY", report.markdown)
        self.assertIn("confirmed SYNDICATED relationship", report.markdown)
        self.assertIn("timeline entry=", report.markdown)

    def test_pending_event_and_claim_evidence_links_remain_explicit(self):
        pending_source = replace(
            self.source,
            source_id="SRC-PENDING-001",
            publisher="Synthetic unreviewed publisher",
        )
        self.database.save(pending_source)
        pending_document = self.add_document(
            "pending-001",
            "Unreviewed report",
            source_id=pending_source.source_id,
        )
        event_match = self.database.propose_event_source_match(
            event_id=self.event.event_id,
            document_id=pending_document,
            proposed_by="analyst",
            proposed_at=TIMESTAMP,
            rationale="Candidate source match awaiting review.",
        )
        evidence_link = self.database.propose_claim_evidence_link(
            claim_id=self.claim.claim_id,
            evidence_id=self.evidence.evidence_id,
            relationship="SUPPORTS",
            proposed_by="analyst",
            proposed_at=TIMESTAMP,
            rationale="Candidate relation awaiting review.",
        )
        report = self.database.get_report(self.create())
        self.assertIn(f"PENDING event/source match {event_match.match_id}", report.markdown)
        self.assertIn(
            f"PENDING claim/evidence relationship {evidence_link.link_id}",
            report.markdown,
        )

    def test_only_reviewed_report_can_be_exported_and_review_is_attributable(self):
        report_id = self.create(high_impact=True)
        output = io.StringIO()
        errors = io.StringIO()
        export_dir = self.root / "exports"
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = main(
                [
                    "report",
                    "export",
                    report_id,
                    "--directory",
                    str(export_dir),
                    "--database",
                    str(self.database_path),
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("cannot be exported before approval", errors.getvalue())
        self.assertFalse(export_dir.exists())

        report = self.database.review_report(
            report_id=report_id,
            decision="APPROVED",
            reviewed_by="analyst-reviewer",
            reviewed_at=TIMESTAMP,
            rationale="Reviewed the pinned sources, gaps and uncertainty.",
        )
        self.assertEqual(report.status, "APPROVED")
        self.assertEqual(report.reviewed_by, "analyst-reviewer")
        self.assertEqual(len(self.database.report_review_history(report_id)), 1)
        result = main(
            [
                "report",
                "export",
                report_id,
                "--directory",
                str(export_dir),
                "--database",
                str(self.database_path),
            ]
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            (export_dir / f"{report_id}.md").read_text(encoding="utf-8"),
            report.markdown,
        )
        self.assertEqual(
            (export_dir / f"{report_id}.html").read_text(encoding="utf-8"),
            report.html,
        )
        self.database.review_report(
            report_id=report_id,
            decision="REJECTED",
            reviewed_by="reviewer-two",
            reviewed_at="2026-10-08T12:01:00Z",
            rationale="A newly identified gap requires revision.",
        )
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = main(
                [
                    "report",
                    "export",
                    report_id,
                    "--directory",
                    str(export_dir),
                    "--database",
                    str(self.database_path),
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("current status: REJECTED", errors.getvalue())

    def test_review_requires_reviewer_and_rationale_and_is_append_only(self):
        report_id = self.create()
        with self.assertRaisesRegex(StorageError, "reviewer and rationale"):
            self.database.review_report(
                report_id=report_id,
                decision="APPROVED",
                reviewed_by=" ",
                reviewed_at=TIMESTAMP,
                rationale="",
            )
        self.database.review_report(
            report_id=report_id,
            decision="REJECTED",
            reviewed_by="reviewer",
            reviewed_at=TIMESTAMP,
            rationale="Missing context.",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE report_reviews SET decision = 'APPROVED'"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE report_snapshots SET markdown = 'tampered' "
                    "WHERE report_id = ?",
                    (report_id,),
                )
        finally:
            connection.close()
        self.assertEqual(self.database.get_report(report_id).status, "REJECTED")

    def test_report_storage_rejects_snapshot_metadata_mismatch(self):
        report = self.database.get_report(self.create())
        input_snapshot = json.loads(report.input_snapshot_json)
        input_snapshot["report"]["report_event_id"] = "CNVS-EVT-OTHER"
        with self.assertRaisesRegex(StorageError, "does not match"):
            self.database.create_report_snapshot(
                report_id="report-with-mismatched-input",
                event_id=report.event_id,
                assessment_id=report.assessment_id,
                assessment_revision=report.assessment_revision,
                high_impact=report.high_impact,
                generated_at=report.generated_at,
                input_snapshot=input_snapshot,
                markdown=report.markdown,
                html=report.html,
            )

    def test_assessment_cli_records_and_lists_immutable_report_review_history(self):
        assessment_file = self.root / "assessment.json"
        assessment_file.write_text(
            json.dumps(
                {
                    "schema_version": self.assessment.schema_version,
                    "assessment_id": self.assessment.assessment_id,
                    "event_id": self.assessment.event_id,
                    "created_at": self.assessment.created_at,
                    "pipeline_version": self.assessment.pipeline_version,
                    "facts": list(self.assessment.facts),
                    "claim_ids": list(self.assessment.claim_ids),
                    "evidence_ids": list(self.assessment.evidence_ids),
                    "confidence": self.assessment.confidence,
                    "contradictions": list(self.assessment.contradictions),
                    "information_gaps": list(self.assessment.information_gaps),
                    "assessment_text": self.assessment.assessment_text,
                    "human_reviewed": self.assessment.human_reviewed,
                }
            ),
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            result = main(
                [
                    "assessment",
                    "record",
                    "--file",
                    str(assessment_file),
                    "--database",
                    str(self.database_path),
                ]
            )
        self.assertEqual(result, 0)

        report_id = self.create()
        self.database.review_report(
            report_id=report_id,
            decision="REJECTED",
            reviewed_by="reviewer-a",
            reviewed_at=TIMESTAMP,
            rationale="Needs correction.",
        )
        self.database.review_report(
            report_id=report_id,
            decision="APPROVED",
            reviewed_by="reviewer-b",
            reviewed_at="2026-10-08T12:01:00Z",
            rationale="Corrected report reviewed.",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "report",
                    "reviews",
                    report_id,
                    "--database",
                    str(self.database_path),
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("decision=REJECTED", output.getvalue())
        self.assertIn("decision=APPROVED", output.getvalue())
        self.assertEqual(self.database.get_report(report_id).status, "APPROVED")
