import contextlib
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from typing import TypeVar
from unittest.mock import patch

from cnvs import collection
from cnvs.cli import main
from cnvs.models import Assessment, Claim, Event, Evidence, Source
from cnvs.normalization import normalize_collection
from cnvs.registry import CollectionConstraints, SourceRegistration
from cnvs.storage import Database
from cnvs.validation import parse_record


ROOT = Path(__file__).resolve().parents[1]
RecordT = TypeVar("RecordT", Event, Source, Claim, Evidence, Assessment)
TIMESTAMP = "2026-10-08T10:00:00Z"
RSS_URL = "https://rss.example.test/feed.xml"
ARTICLE_URL = "https://article.example.test/story"
FAILED_URL = "https://unavailable.example.test/feed.xml"


def _example(
    record_type: str,
    filename: str,
    expected_type: type[RecordT],
    **updates,
) -> RecordT:
    payload = json.loads((ROOT / "examples" / filename).read_text(encoding="utf-8"))
    payload.update(updates)
    record = parse_record(record_type, payload)
    if not isinstance(record, expected_type):
        raise TypeError(f"{filename} did not produce a {expected_type.__name__}.")
    return record


def _registration(
    source_id: str,
    publisher: str,
    url: str,
    access_method: str,
    allowed_content_type: str,
) -> SourceRegistration:
    return SourceRegistration(
        source_id=source_id,
        publisher=publisher,
        source_class="secondary_reporting",
        country="PL",
        language="en",
        access_method=access_method,
        url=url,
        event_relevance="Synthetic Stage 13 rehearsal.",
        review_status="approved",
        reviewer="synthetic-reviewer",
        reviewed_at=TIMESTAMP,
        enabled=True,
        constraints=CollectionConstraints(
            max_bytes=1024 * 1024,
            allowed_content_types=(allowed_content_type,),
            archive_content=True,
            retention_basis="Synthetic fixture; temporary test database.",
            respect_robots_txt=True,
            timeout_seconds=5,
        ),
    )


def _response(url: str, body: bytes, media_type: str) -> collection._Response:
    headers = Message()
    headers["Content-Type"] = media_type
    return collection._Response(200, url, headers, body)


class Stage13EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "e2e.sqlite3"
        self.database = Database(self.database_path)

        self.event = _example("event", "event.json", Event)
        rss_source = _example(
            "source",
            "source.json",
            Source,
            source_id="SRC-RSS-E2E",
            publisher="Synthetic RSS publisher",
            title="Synthetic RSS report",
            url=RSS_URL,
            independence_group="IG-RSS-E2E",
            raw_content_ref="synthetic://stage13/rss",
            notes="Synthetic Stage 13 fixture.",
        )
        article_source = _example(
            "source",
            "source.json",
            Source,
            source_id="SRC-URL-E2E",
            publisher="Synthetic URL publisher",
            title="Synthetic HTML report",
            url=ARTICLE_URL,
            independence_group="IG-URL-E2E",
            raw_content_ref="synthetic://stage13/article",
            notes="Synthetic Stage 13 fixture.",
        )
        self.claim = _example(
            "claim",
            "claim.json",
            Claim,
            claim_id="CLM-E2E",
            source_id="SRC-RSS-E2E",
            supporting_evidence_ids=["EVD-E2E"],
        )
        self.evidence = _example(
            "evidence",
            "evidence.json",
            Evidence,
            evidence_id="EVD-E2E",
            source_id="SRC-URL-E2E",
            analyst_notes="Synthetic Stage 13 evidence; no real-world assertion.",
        )
        for record in (
            self.event,
            rss_source,
            article_source,
            self.evidence,
            self.claim,
        ):
            self.database.save(record)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_synthetic_rss_url_failure_to_reviewed_report_export(self):
        rss_registration = _registration(
            "SRC-RSS-E2E",
            "Synthetic RSS publisher",
            RSS_URL,
            "RSS",
            "application/rss+xml",
        )
        article_registration = _registration(
            "SRC-URL-E2E",
            "Synthetic URL publisher",
            ARTICLE_URL,
            "URL",
            "text/html",
        )
        unavailable_registration = _registration(
            "SRC-FAILED-E2E",
            "Synthetic unavailable publisher",
            FAILED_URL,
            "URL",
            "text/html",
        )
        rss_content = (
            b'<?xml version="1.0"?><rss><channel><language>en</language>'
            b"<item><guid>e2e-wire-1</guid><title>Synthetic RSS report</title>"
            b"<link>https://rss.example.test/story</link>"
            b"<description>Synthetic report states the fictional vessel had damage."
            b"</description><pubDate>Wed, 08 Oct 2026 08:00:00 +0000</pubDate>"
            b"</item></channel></rss>"
        )
        article_content = (
            b'<html lang="en"><head><title>Synthetic HTML report</title>'
            b'<meta property="article:published_time" '
            b'content="2026-10-08T08:30:00Z"></head><body><article>'
            b"A separate synthetic observer describes a mark."
            b"</article></body></html>"
        )
        responses = {
            RSS_URL: _response(RSS_URL, rss_content, "application/rss+xml"),
            ARTICLE_URL: _response(ARTICLE_URL, article_content, "text/html"),
        }

        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection._check_robots"
        ), patch(
            "cnvs.collection._fetch",
            side_effect=lambda url, **kwargs: responses[url],
        ):
            rss_result = collection.collect_source(self.database, rss_registration)
            article_result = collection.collect_source(
                self.database, article_registration
            )
        self.assertEqual(
            (rss_result.status, article_result.status),
            ("SUCCEEDED", "SUCCEEDED"),
        )

        rss_documents, _ = normalize_collection(
            self.database, rss_result.collection_id
        )
        article_documents, _ = normalize_collection(
            self.database, article_result.collection_id
        )
        self.assertEqual(len(rss_documents), 1)
        self.assertEqual(len(article_documents), 1)
        self.assertEqual(
            rss_documents[0].original_text,
            "Synthetic report states the fictional vessel had damage.",
        )
        self.assertIn("separate synthetic observer", article_documents[0].original_text)

        for document in (rss_documents[0], article_documents[0]):
            match = self.database.propose_event_source_match(
                event_id=self.event.event_id,
                document_id=document.document_id,
                proposed_by="synthetic-analyst",
                proposed_at=TIMESTAMP,
                rationale="Synthetic rehearsal document concerns the example event.",
            )
            self.database.review_event_source_match(
                match_id=match.match_id,
                decision="LINKED",
                reviewed_by="synthetic-reviewer",
                reviewed_at=TIMESTAMP,
                rationale="Synthetic event/source link reviewed for the exercise.",
            )
        evidence_link = self.database.propose_claim_evidence_link(
            claim_id=self.claim.claim_id,
            evidence_id=self.evidence.evidence_id,
            relationship="SUPPORTS",
            proposed_by="synthetic-analyst",
            proposed_at=TIMESTAMP,
            rationale="Synthetic observation is relevant to the synthetic claim.",
        )
        self.database.review_claim_evidence_link(
            link_id=evidence_link.link_id,
            decision="LINKED",
            reviewed_by="synthetic-reviewer",
            reviewed_at=TIMESTAMP,
            rationale="The synthetic relationship was checked.",
        )

        failure = collection._CollectionFailure(
            "network_error", "Synthetic source unavailable."
        )
        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection._check_robots"
        ), patch("cnvs.collection._fetch", side_effect=failure):
            failed_attempt = collection.collect_source(
                self.database, unavailable_registration
            )
            self.assertEqual(failed_attempt.status, "FAILED")
            for _ in range(2):
                failed_attempt = collection.retry_collection(
                    self.database,
                    unavailable_registration,
                    failed_attempt.collection_id,
                )
            self.assertEqual(failed_attempt.attempt_number, 3)
            with self.assertRaisesRegex(ValueError, "attempt limit"):
                collection.retry_collection(
                    self.database, unavailable_registration, failed_attempt.collection_id
                )

        result_output = io.StringIO()
        with contextlib.redirect_stdout(result_output):
            result_status = main(
                [
                    "source",
                    "results",
                    "--source-id",
                    "SRC-FAILED-E2E",
                    "--database",
                    str(self.database_path),
                ]
            )
        self.assertEqual(result_status, 0)
        self.assertIn("FAILED", result_output.getvalue())
        self.assertIn("network_error", result_output.getvalue())

        assessment = _example(
            "assessment",
            "assessment.json",
            Assessment,
            assessment_id="ASM-STAGE13-E2E",
            claim_ids=[self.claim.claim_id],
            evidence_ids=[self.evidence.evidence_id],
            information_gaps=[
                "The synthetic unavailable source produced no document; its absence "
                "is not evidence against the event."
            ],
            assessment_text=(
                "Synthetic rehearsal only. No real-world conclusion is supported."
            ),
            confidence={
                "occurrence": "UNKNOWN",
                "method": "UNKNOWN",
                "attribution": "UNKNOWN",
            },
            human_reviewed=False,
        )
        self.assertIsInstance(assessment, Assessment)
        assessment_path = self.root / "assessment.json"
        assessment_path.write_text(
            json.dumps(
                {
                    "schema_version": assessment.schema_version,
                    "assessment_id": assessment.assessment_id,
                    "event_id": assessment.event_id,
                    "created_at": assessment.created_at,
                    "pipeline_version": assessment.pipeline_version,
                    "facts": list(assessment.facts),
                    "claim_ids": list(assessment.claim_ids),
                    "evidence_ids": list(assessment.evidence_ids),
                    "confidence": assessment.confidence,
                    "contradictions": list(assessment.contradictions),
                    "information_gaps": list(assessment.information_gaps),
                    "assessment_text": assessment.assessment_text,
                    "human_reviewed": assessment.human_reviewed,
                }
            ),
            encoding="utf-8",
        )
        cli = ["--database", str(self.database_path)]
        self.assertEqual(
            main(["assessment", "record", "--file", str(assessment_path), *cli]),
            0,
        )

        report_output = io.StringIO()
        with contextlib.redirect_stdout(report_output):
            self.assertEqual(
                main(
                    [
                        "report",
                        "create",
                        assessment.assessment_id,
                        "--high-impact",
                        "--config-dir",
                        str(ROOT / "config"),
                        *cli,
                    ]
                ),
                0,
            )
        report_id = report_output.getvalue().split()[0]
        report = self.database.get_report(report_id)
        for expected in (
            "Synthetic RSS report",
            "Synthetic HTML report",
            "synthetic unavailable source produced no document",
            "occurrence: UNKNOWN",
            "method: UNKNOWN",
            "attribution: UNKNOWN",
            "## FACT",
            "## CLAIM",
            "## ASSESSMENT",
            "## CONFIDENCE",
            "## GAP",
            "## TIMELINE",
        ):
            self.assertIn(expected, report.markdown)
        self.assertIn("https://rss.example.test", report.markdown)
        self.assertIn("https://article.example.test", report.markdown)

        export_dir = self.root / "approved-export"
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.assertEqual(
                main(
                    [
                        "report",
                        "export",
                        report_id,
                        "--directory",
                        str(export_dir),
                        *cli,
                    ]
                ),
                2,
            )
        self.assertIn("cannot be exported before approval", errors.getvalue())

        review_output = io.StringIO()
        with contextlib.redirect_stdout(review_output):
            self.assertEqual(
                main(
                    [
                        "report",
                        "review",
                        report_id,
                        "--decision",
                        "APPROVED",
                        "--reviewer",
                        "synthetic-reviewer",
                        "--rationale",
                        "Reviewed the exact synthetic snapshot, source links and failure gap.",
                        *cli,
                    ]
                ),
                0,
            )
        approved = self.database.get_report(report_id)
        self.assertEqual(approved.status, "APPROVED")
        self.assertEqual(approved.reviewed_by, "synthetic-reviewer")

        self.assertEqual(
            main(
                [
                    "report",
                    "export",
                    report_id,
                    "--directory",
                    str(export_dir),
                    *cli,
                ]
            ),
            0,
        )
        self.assertEqual(
            (export_dir / f"{report_id}.md").read_text(encoding="utf-8"),
            approved.markdown,
        )
        self.assertEqual(
            (export_dir / f"{report_id}.html").read_text(encoding="utf-8"),
            approved.html,
        )


if __name__ == "__main__":
    unittest.main()
