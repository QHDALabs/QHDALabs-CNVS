import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cnvs.cli import main
from cnvs.models import Event, JsonValue
from cnvs.normalization import normalize_collection
from cnvs.storage import Database, StorageError
from cnvs.validation import parse_record


ROOT = Path(__file__).resolve().parents[1]
SOURCE_TEXT = "Équipe reports: Officials said the port remained closed."


def synthetic_event() -> Event:
    payload = json.loads(
        (ROOT / "examples" / "event.json").read_text(encoding="utf-8")
    )
    payload["event_id"] = "CNVS-EVT-2026-10-08-0001"
    event = parse_record("event", payload)
    if not isinstance(event, Event):
        raise AssertionError("The event fixture did not parse as an Event.")
    return event


def claim_fields(text: str = SOURCE_TEXT) -> dict[str, JsonValue]:
    start = text.index("the port remained closed")
    end = start + len("the port remained closed")
    return {
        "span_start": start,
        "span_end": end,
        "subject": "the port",
        "predicate": "remained",
        "object": "closed",
        "claim_type": "INCIDENT",
        "attribution": "Officials",
        "modality": "REPORTED",
    }


class ClaimExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cnvs.sqlite3"
        self.database = Database(self.database_path)
        self.event = synthetic_event()
        self.database.create_event(self.event)

        feed_url = "https://synthetic.example/feed"
        feed = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0"><channel><language>en</language><item>'
            "<guid>claim-fixture</guid><title>Synthetic report</title>"
            "<link>https://synthetic.example/report</link>"
            "<pubDate>Tue, 07 Oct 2025 09:00:00 +0000</pubDate>"
            f"<description>{SOURCE_TEXT}</description>"
            "</item></channel></rss>"
        ).encode("utf-8")
        collection = self.database.record_collection_result(
            source_id="SYNTHETIC-CLAIM-SOURCE",
            source_metadata={"publisher": "Synthetic publisher", "language": "en"},
            source_url=feed_url,
            status="SUCCEEDED",
            started_at="2026-10-08T10:00:00Z",
            completed_at="2026-10-08T10:01:00Z",
            final_url=feed_url,
            http_status=200,
            content_type="application/rss+xml",
            content=feed,
            archive_content=True,
        )
        documents, _ = normalize_collection(self.database, collection.collection_id)
        self.document = documents[0]
        match = self.database.propose_event_source_match(
            event_id=self.event.event_id,
            document_id=self.document.document_id,
            proposed_by="analyst",
            proposed_at="2026-10-08T10:02:00Z",
            rationale="Synthetic fixture explicitly linked to its event.",
        )
        self.match_id = match.match_id

    def tearDown(self):
        self.temp_dir.cleanup()

    def link_source(self):
        return self.database.review_event_source_match(
            match_id=self.match_id,
            decision="LINKED",
            reviewed_by="reviewer",
            reviewed_at="2026-10-08T10:03:00Z",
            rationale="The synthetic fixture is explicitly in scope.",
        )

    def extract(self, fields: dict[str, JsonValue] | None = None):
        return self.database.create_claim_extractions(
            event_id=self.event.event_id,
            document_id=self.document.document_id,
            candidates=[fields or claim_fields()],
            extraction_method="analyst-assisted JSON import",
            extractor="synthetic analyst",
            created_at="2026-10-08T10:04:00Z",
        )

    def test_candidates_require_confirmed_match_and_exact_source_span(self):
        with self.assertRaisesRegex(StorageError, "explicitly confirmed"):
            self.extract()
        self.link_source()

        candidate = self.extract()[0]
        quote = "the port remained closed"
        span_start = SOURCE_TEXT.index(quote)
        self.assertEqual(
            candidate.span_text,
            SOURCE_TEXT[span_start : span_start + len(quote)],
        )
        self.assertEqual(
            candidate.source_text_sha256,
            self.document.text_sha256,
        )
        self.assertEqual(candidate.source_id, self.document.source_id)
        self.assertEqual(candidate.review_status, "PENDING")
        self.assertIsNone(candidate.review_decision)
        self.assertEqual(candidate.claim_type, "INCIDENT")
        self.assertEqual(candidate.modality, "REPORTED")
        self.assertEqual(
            self.database.claim_extractions(review_status="PENDING"), [candidate]
        )

    def test_invalid_spans_fields_and_batch_are_rejected_atomically(self):
        self.link_source()
        invalid = claim_fields()
        invalid["span_end"] = len(SOURCE_TEXT) + 1
        with self.assertRaisesRegex(StorageError, "outside the source text"):
            self.extract(invalid)
        missing = claim_fields()
        del missing["attribution"]
        with self.assertRaisesRegex(StorageError, "fields are invalid"):
            self.extract(missing)
        with self.assertRaisesRegex(StorageError, "outside the source text"):
            self.database.create_claim_extractions(
                event_id=self.event.event_id,
                document_id=self.document.document_id,
                candidates=[claim_fields(), invalid],
                extraction_method="synthetic",
                extractor="synthetic analyst",
                created_at="2026-10-08T10:04:00Z",
            )
        self.assertEqual(self.database.claim_extractions(), [])

    def test_correction_acceptance_rejection_and_history_are_append_only(self):
        self.link_source()
        candidate = self.extract()[0]
        accepted = self.database.review_claim_extraction(
            candidate_id=candidate.candidate_id,
            decision="ACCEPTED",
            reviewed_by="reviewer",
            reviewed_at="2026-10-08T10:05:00Z",
            rationale="Fields faithfully represent the attributed statement.",
        )
        self.assertEqual(accepted.review_status, "ACCEPTED")
        self.assertEqual(
            self.database.claim_extractions(review_status="ACCEPTED"), [accepted]
        )
        connection = sqlite3.connect(self.database_path)
        try:
            canonical_claims = connection.execute(
                """
                SELECT count(*) FROM canonical_records
                WHERE record_type = 'claim'
                """
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(canonical_claims, 0)

        corrected = {
            **claim_fields(),
            "subject": "port operations",
            "span_start": 0,
            "span_end": len(SOURCE_TEXT),
        }
        result = self.database.review_claim_extraction(
            candidate_id=candidate.candidate_id,
            decision="CORRECTED",
            reviewed_by="reviewer-two",
            reviewed_at="2026-10-08T10:06:00Z",
            rationale="The subject should refer to port operations.",
            correction=corrected,
        )
        self.assertEqual(result.review_status, "CORRECTED")
        self.assertEqual(result.correction_payload, corrected)
        accepted_again = self.database.review_claim_extraction(
            candidate_id=candidate.candidate_id,
            decision="ACCEPTED",
            reviewed_by="reviewer-three",
            reviewed_at="2026-10-08T10:07:00Z",
            rationale="Corrected structured extraction reviewed.",
        )
        self.assertEqual(accepted_again.review_status, "ACCEPTED")
        history = self.database.claim_extraction_history(candidate.candidate_id)
        self.assertEqual(
            [item.review_status for item in history],
            ["PENDING", "ACCEPTED", "CORRECTED", "ACCEPTED"],
        )
        self.assertEqual(history[2].correction_payload, corrected)

        rejected = self.database.create_claim_extractions(
            event_id=self.event.event_id,
            document_id=self.document.document_id,
            candidates=[claim_fields()],
            extraction_method="analyst-assisted JSON import",
            extractor="synthetic analyst",
            created_at="2026-10-08T10:08:00Z",
        )[0]
        rejected = self.database.review_claim_extraction(
            candidate_id=rejected.candidate_id,
            decision="REJECTED",
            reviewed_by="reviewer",
            reviewed_at="2026-10-08T10:09:00Z",
            rationale="The quoted text does not support the structured claim.",
        )
        self.assertEqual(rejected.review_status, "REJECTED")

    def test_corrections_must_match_archived_text_and_have_rationale(self):
        self.link_source()
        candidate = self.extract()[0]
        incorrect = {
            **claim_fields(),
            "span_start": 0,
            "span_end": len(SOURCE_TEXT) + 1,
        }
        with self.assertRaisesRegex(StorageError, "outside the source text"):
            self.database.review_claim_extraction(
                candidate_id=candidate.candidate_id,
                decision="CORRECTED",
                reviewed_by="reviewer",
                reviewed_at="2026-10-08T10:05:00Z",
                rationale="Correction.",
                correction=incorrect,
            )
        with self.assertRaisesRegex(StorageError, "must include corrected"):
            self.database.review_claim_extraction(
                candidate_id=candidate.candidate_id,
                decision="CORRECTED",
                reviewed_by="reviewer",
                reviewed_at="2026-10-08T10:05:00Z",
                rationale="Correction.",
            )
        with self.assertRaisesRegex(StorageError, "reviewer and rationale"):
            self.database.review_claim_extraction(
                candidate_id=candidate.candidate_id,
                decision="REJECTED",
                reviewed_by=" ",
                reviewed_at="2026-10-08T10:05:00Z",
                rationale="",
            )

    def test_sqlite_prevents_mutating_candidates_and_review_history(self):
        self.link_source()
        candidate = self.extract()[0]
        self.database.review_claim_extraction(
            candidate_id=candidate.candidate_id,
            decision="UNRESOLVED",
            reviewed_by="reviewer",
            reviewed_at="2026-10-08T10:05:00Z",
            rationale="More source context is needed.",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE claim_extractions SET subject = 'changed' "
                    "WHERE candidate_id = ?",
                    (candidate.candidate_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM claim_extraction_reviews WHERE candidate_id = ?",
                    (candidate.candidate_id,),
                )
        finally:
            connection.close()

    def test_sqlite_rejects_forged_source_or_span_provenance(self):
        self.link_source()
        candidate = self.extract()[0]
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT * FROM claim_extractions WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            columns = [
                description[0]
                for description in connection.execute(
                    "SELECT * FROM claim_extractions LIMIT 0"
                ).description
            ]
            for identifier, changes in (
                ("forged-source", {"source_id": "UNRELATED-SOURCE"}),
                ("forged-span", {"span_text": "fabricated quote"}),
            ):
                forged = dict(zip(columns, row))
                forged["candidate_id"] = identifier
                forged.update(changes)
                placeholders = ", ".join("?" for _ in columns)
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "source provenance is invalid"
                ):
                    connection.execute(
                        f"INSERT INTO claim_extractions ({', '.join(columns)}) "
                        f"VALUES ({placeholders})",
                        [forged[column] for column in columns],
                    )
        finally:
            connection.close()

    def test_cli_extract_review_and_history_work_with_untrusted_text_as_data(self):
        self.link_source()
        input_path = Path(self.temp_dir.name) / "candidates.json"
        input_path.write_text(json.dumps([claim_fields()]), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "claim", "extract", self.event.event_id,
                        self.document.document_id, "--file", str(input_path),
                        "--method", "external structured extractor",
                        "--extractor", "synthetic model", "--created-at",
                        "2026-10-08T10:04:00Z", "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn("Stored 1 derived claim candidate", output.getvalue())
        candidate_id = output.getvalue().splitlines()[1].split()[0]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "claim", "list", "--event-id", self.event.event_id,
                        "--status", "PENDING", "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn(candidate_id, output.getvalue())
        correction_path = Path(self.temp_dir.name) / "correction.json"
        correction = {**claim_fields(), "subject": "port operations"}
        correction_path.write_text(json.dumps(correction), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "claim", "review", candidate_id, "--decision", "CORRECTED",
                        "--reviewer", "reviewer", "--rationale",
                        "Refined subject.", "--correction-file",
                        str(correction_path), "--reviewed-at",
                        "2026-10-08T10:05:00Z", "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn("review=CORRECTED", output.getvalue())
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "claim", "history", candidate_id, "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn("source_text_sha256=", output.getvalue())
        self.assertIn("review=CORRECTED", output.getvalue())


if __name__ == "__main__":
    unittest.main()
