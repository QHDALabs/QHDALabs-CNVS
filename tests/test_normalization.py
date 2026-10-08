import contextlib
import hashlib
import io
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cnvs.cli import main
from cnvs.models import CollectionResult
from cnvs.normalization import (
    MAX_NORMALIZATION_BYTES,
    NormalizationError,
    normalize_collection,
    normalize_text,
    normalize_url,
    parse_collection_documents,
)
from cnvs.storage import Database, StorageError


def feed(
    *,
    guid: str,
    text: str,
    title: str = "Synthetic bulletin",
    link: str = "https://publisher.example/story?utm_source=feed",
    published: str = "Tue, 07 Oct 2025 09:00:00 +0200",
    language: str = "en",
) -> bytes:
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return (
        f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><language>{language}</language>
<item><guid>{guid}</guid><title>{title}</title><link>{link}</link>
<description>{escaped}</description><pubDate>{published}</pubDate></item>
</channel></rss>"""
    ).encode("utf-8")


class NormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cnvs.sqlite3"
        self.database = Database(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_collection(
        self,
        *,
        source_id: str,
        body: bytes,
        registry_language: str = "pl",
        archive_content: bool = True,
        content_type: str = "application/rss+xml",
        source_url: str | None = None,
        final_url: str | None = None,
    ) -> CollectionResult:
        url = source_url or f"https://{source_id.lower()}.example/feed"
        return self.database.record_collection_result(
            source_id=source_id,
            source_metadata={
                "publisher": f" {source_id} News ",
                "language": registry_language,
                "source_class": "secondary_reporting",
                "country": "PL",
                "access_method": "RSS",
            },
            source_url=url,
            status="SUCCEEDED",
            started_at="2026-10-07T10:00:00Z",
            completed_at="2026-10-07T10:00:05Z",
            final_url=final_url or url,
            http_status=200,
            content_type=content_type,
            content=body,
            archive_content=archive_content,
        )

    def test_preserves_original_text_and_timestamps_while_normalizing(self):
        original = "  Cafe\u0301\u00a0reports   \n a synthetic event.  "
        collected = self.add_collection(
            source_id="SYNTHETIC-ONE",
            body=feed(
                guid="fixture-1",
                text=original,
                published="Tue, 07 Oct 2025 09:00:00 +0200",
                language="pl",
            ),
            registry_language="pl",
        )
        documents, relationships = normalize_collection(
            self.database, collected.collection_id
        )

        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document.original_text, original)
        self.assertEqual(document.normalized_text, "Café reports a synthetic event.")
        self.assertEqual(document.original_published_at, "Tue, 07 Oct 2025 09:00:00 +0200")
        self.assertEqual(document.normalized_published_at, "2025-10-07T07:00:00Z")
        self.assertTrue(document.publication_timezone_known)
        self.assertEqual(document.language, "pl")
        self.assertEqual(document.language_source, "DOCUMENT_DECLARED")
        self.assertEqual(document.language_review_status, "RECORDED")
        self.assertEqual(document.publisher_original, "SYNTHETIC-ONE News")
        self.assertEqual(document.publisher_normalized, "synthetic-one news")
        self.assertEqual(
            document.normalized_url,
            "https://publisher.example/story",
        )
        self.assertEqual(relationships, [])
        self.assertEqual(
            self.database.normalized_documents(collection_id=collected.collection_id),
            documents,
        )
        self.assertEqual(
            self.database.operational_metrics()["normalization_attempts"],
            {"SUCCEEDED": 1},
        )

    def test_language_conflicts_and_unzoned_dates_are_flagged_not_discarded(self):
        collected = self.add_collection(
            source_id="SYNTHETIC-TWO",
            body=feed(
                guid="fixture-2",
                text="A short synthetic bulletin.",
                published="2025-10-07T09:00:00",
                language="en",
            ),
            registry_language="pl",
        )
        documents, _ = normalize_collection(self.database, collected.collection_id)

        document = documents[0]
        self.assertEqual(document.declared_language, "en")
        self.assertEqual(document.registry_language, "pl")
        self.assertEqual(document.language, "en")
        self.assertEqual(document.language_review_status, "REVIEW_REQUIRED")
        self.assertEqual(document.original_published_at, "2025-10-07T09:00:00")
        self.assertEqual(document.normalized_published_at, "2025-10-07T09:00:00")
        self.assertFalse(document.publication_timezone_known)

    def test_unknown_language_is_explicit_and_unsupported_declaration_is_retained(self):
        collected = self.add_collection(
            source_id="SYNTHETIC-UNKNOWN",
            body=feed(
                guid="fixture-unknown",
                text="Unclassified synthetic text.",
                language="xx",
            ),
            registry_language="xx",
        )
        document = normalize_collection(
            self.database, collected.collection_id
        )[0][0]
        self.assertEqual(document.language, "unknown")
        self.assertEqual(document.language_source, "UNKNOWN")
        self.assertEqual(document.language_review_status, "REVIEW_REQUIRED")
        self.assertEqual(document.declared_language, "xx")

    def test_html_page_extracts_canonical_url_meta_language_and_visible_text(self):
        body = (
            b'<html lang="en"><head><meta charset="windows-1252">'
            b"<title> Synthetic page </title>"
            b'<meta property="article:published_time" '
            b'content="2025-10-07T09:00:00+02:00">'
            b'<link rel="canonical" href="/news/story?utm_medium=mail">'
            b"</head><body><article>R\xe9sum\xe9 &amp; evidence</article>"
            b"<script>ignore this script</script></body></html>"
        )
        collected = self.add_collection(
            source_id="SYNTHETIC-HTML",
            body=body,
            registry_language="pl",
            content_type="text/html",
            source_url="https://publisher.example/redirect",
            final_url="https://publisher.example/page",
        )

        document = normalize_collection(self.database, collected.collection_id)[0][0]
        self.assertEqual(document.original_title, " Synthetic page ")
        self.assertEqual(document.normalized_title, "Synthetic page")
        self.assertIn("Résumé & evidence", document.original_text)
        self.assertNotIn("ignore this script", document.original_text)
        self.assertEqual(
            document.normalized_url, "https://publisher.example/news/story"
        )
        self.assertEqual(
            document.original_published_at, "2025-10-07T09:00:00+02:00"
        )
        self.assertEqual(document.normalized_published_at, "2025-10-07T07:00:00Z")
        self.assertEqual(document.language, "en")
        self.assertEqual(document.language_review_status, "REVIEW_REQUIRED")

    def test_language_falls_back_to_registry_when_document_has_no_declaration(self):
        html_page = (
            b"<html><head><title>Synthetic page</title></head>"
            b"<body><p>Text with language supplied by source registry.</p></body></html>"
        )
        collected = self.add_collection(
            source_id="SYNTHETIC-HTML-LANGUAGE",
            body=html_page,
            registry_language="pl",
            content_type="text/html",
        )
        document = normalize_collection(
            self.database, collected.collection_id
        )[0][0]

        self.assertEqual(document.language, "pl")
        self.assertEqual(document.language_source, "REGISTRY")
        self.assertEqual(document.language_review_status, "RECORDED")
        self.assertIsNone(document.declared_language)

    def test_translation_retains_provenance_and_never_replaces_original(self):
        collected = self.add_collection(
            source_id="SYNTHETIC-TRANSLATION",
            body=feed(guid="translation-source", text="Original source text."),
            registry_language="en",
        )
        document = normalize_collection(
            self.database, collected.collection_id
        )[0][0]

        translation = self.database.add_translation(
            document_id=document.document_id,
            translated_text="Tekst po polsku.",
            target_language="pl",
            translation_method="analyst translation",
            translator="analyst@example.test",
            translated_at="2026-10-07T10:30:00Z",
        )

        self.assertEqual(translation.source_text_sha256, document.text_sha256)
        self.assertEqual(translation.source_language, "en")
        self.assertEqual(translation.target_language, "pl")
        self.assertEqual(self.database.normalized_documents()[0].original_text, "Original source text.")
        self.assertEqual(self.database.translations(document.document_id), [translation])

    def test_unknown_source_language_cannot_claim_translation_provenance(self):
        collected = self.add_collection(
            source_id="SYNTHETIC-LANG-UNKNOWN",
            body=feed(guid="translation-unknown", text="Some text.", language="xx"),
            registry_language="xx",
        )
        document = normalize_collection(
            self.database, collected.collection_id
        )[0][0]
        with self.assertRaisesRegex(StorageError, "language is unknown"):
            self.database.add_translation(
                document_id=document.document_id,
                translated_text="Tekst.",
                target_language="pl",
                translation_method="manual",
                translator="analyst",
                translated_at="2026-10-07T10:30:00Z",
            )

    def test_exact_duplicates_are_detected_and_remain_pending_not_merged(self):
        text = (
            "Independent review is required before attributing the synthetic "
            "infrastructure incident. "
        ) * 10
        first_collection = self.add_collection(
            source_id="SYNTHETIC-EXACT-A",
            body=feed(
                guid="exact-a",
                text=text,
                link="https://first.example/story?utm_campaign=a",
            ),
        )
        second_collection = self.add_collection(
            source_id="SYNTHETIC-EXACT-B",
            body=feed(
                guid="exact-b",
                text=text,
                link="https://second.example/reprint",
            ),
        )
        first = normalize_collection(self.database, first_collection.collection_id)[0][0]
        second_documents, relationships = normalize_collection(
            self.database, second_collection.collection_id
        )

        self.assertEqual(len(second_documents), 1)
        self.assertEqual(len(relationships), 1)
        match = relationships[0]
        self.assertEqual(match.relationship_type, "EXACT_TEXT_MATCH")
        self.assertEqual(match.similarity, 1.0)
        self.assertEqual(match.review_status, "PENDING")
        self.assertEqual(
            {match.document_id, match.related_document_id},
            {first.document_id, second_documents[0].document_id},
        )
        self.assertEqual(len(self.database.normalized_documents()), 2)

    def test_likely_syndication_is_reviewable_and_decisions_are_append_only(self):
        original = (
            "Analysts continue to assess the synthetic transport interruption "
            "while local authorities review the reported damage and timeline. "
        ) * 12
        republished = original.replace(
            "Analysts continue to assess", "Editors report that analysts assess", 1
        )
        first_collection = self.add_collection(
            source_id="SYNTHETIC-WIRE-A",
            body=feed(guid="wire-a", text=original, title="Transport interruption"),
        )
        second_collection = self.add_collection(
            source_id="SYNTHETIC-WIRE-B",
            body=feed(guid="wire-b", text=republished, title="Transport interruption"),
        )
        normalize_collection(self.database, first_collection.collection_id)
        _, relationships = normalize_collection(
            self.database, second_collection.collection_id
        )

        self.assertEqual(len(relationships), 1)
        relationship = relationships[0]
        self.assertEqual(relationship.relationship_type, "POSSIBLE_SYNDICATION")
        self.assertEqual(relationship.review_status, "PENDING")
        first_review = self.database.review_duplicate_relationship(
            relationship_id=relationship.relationship_id,
            decision="CONFIRMED_DEPENDENT",
            reviewed_by="analyst-one",
            reviewed_at="2026-10-07T10:30:00Z",
            rationale="Synthetic fixture shares the same long text.",
        )
        self.assertEqual(first_review.review_status, "REVIEWED")
        self.assertEqual(first_review.review_decision, "CONFIRMED_DEPENDENT")
        latest_review = self.database.review_duplicate_relationship(
            relationship_id=relationship.relationship_id,
            decision="INDEPENDENT_EVIDENCE_DOCUMENTED",
            reviewed_by="analyst-two",
            reviewed_at="2026-10-07T10:31:00Z",
            rationale="Separate evidence chains were documented for this fixture.",
        )
        self.assertEqual(
            latest_review.review_decision, "INDEPENDENT_EVIDENCE_DOCUMENTED"
        )
        self.assertEqual(latest_review.reviewed_by, "analyst-two")

    def test_normalization_requires_retained_content_and_success_status(self):
        unarchived = self.add_collection(
            source_id="SYNTHETIC-NO-ARCHIVE",
            body=feed(guid="no-archive", text="Synthetic text."),
            archive_content=False,
        )
        with self.assertRaisesRegex(NormalizationError, "no retained content"):
            normalize_collection(self.database, unarchived.collection_id)

        failed = self.database.record_collection_result(
            source_id="SYNTHETIC-FAILED",
            source_url="https://failed.example/feed",
            status="FAILED",
            started_at="2026-10-07T10:00:00Z",
            completed_at="2026-10-07T10:00:01Z",
            error_code="network_error",
            error_message="Synthetic failure.",
        )
        with self.assertRaisesRegex(NormalizationError, "only successful"):
            normalize_collection(self.database, failed.collection_id)

    def test_normalization_failures_are_recorded_and_visible_in_metrics(self):
        collected = self.add_collection(
            source_id="SYNTHETIC-UNSUPPORTED",
            body=b"<not-a-supported-source/>",
            content_type="application/json",
        )

        with self.assertLogs("cnvs.normalization", level="ERROR") as captured:
            with self.assertRaisesRegex(NormalizationError, "not supported"):
                normalize_collection(self.database, collected.collection_id)

        self.assertEqual(
            [
                (
                    record.__dict__.get("event"),
                    record.__dict__.get("status"),
                    record.__dict__.get("error_code"),
                )
                for record in captured.records
            ],
            [("normalization_attempt", "FAILED", "normalization_error")],
        )
        self.assertEqual(
            self.database.operational_metrics()["normalization_attempts"],
            {"FAILED": 1},
        )
        self.assertEqual(
            self.database.operational_metrics()["normalization_failures"],
            {"normalization_error": 1},
        )
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE processing_attempts SET status = 'SUCCEEDED' "
                    "WHERE collection_id = ?",
                    (collected.collection_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM processing_attempts WHERE collection_id = ?",
                    (collected.collection_id,),
                )
        finally:
            connection.close()

    def test_normalization_rejects_oversized_input_before_starting_parser(self):
        with self.assertRaisesRegex(NormalizationError, "normalization limit") as raised:
            parse_collection_documents(
                collection_id="synthetic-collection",
                source_id="synthetic-source",
                source_metadata={},
                source_url="https://example.test/feed",
                final_url=None,
                content_type="application/rss+xml",
                content=b"x" * (MAX_NORMALIZATION_BYTES + 1),
                created_at="2026-10-07T10:00:00Z",
            )
        self.assertEqual(raised.exception.code, "input_too_large")

    def test_parser_timeout_is_recorded_as_failure(self):
        collected = self.add_collection(
            source_id="SYNTHETIC-PARSER-TIMEOUT",
            body=feed(guid="timeout", text="A bounded parser test."),
        )
        with patch("cnvs.normalization.PARSER_TIMEOUT_SECONDS", 0):
            with self.assertRaisesRegex(NormalizationError, "exceeded 0 seconds") as raised:
                normalize_collection(self.database, collected.collection_id)

        self.assertEqual(raised.exception.code, "parser_timeout")
        self.assertEqual(
            self.database.operational_metrics()["normalization_failures"],
            {"parser_timeout": 1},
        )

    def test_feed_item_limit_is_a_recorded_processing_failure(self):
        content = (
            b"<rss><channel>"
            + b"<item/>" * 5001
            + b"</channel></rss>"
        )
        collected = self.add_collection(
            source_id="SYNTHETIC-ITEM-LIMIT",
            body=content,
        )
        with self.assertRaisesRegex(NormalizationError, "5,000-item") as raised:
            normalize_collection(self.database, collected.collection_id)

        self.assertEqual(raised.exception.code, "too_many_items")
        self.assertEqual(
            self.database.operational_metrics()["normalization_failures"],
            {"too_many_items": 1},
        )

    def test_rerunning_normalization_is_idempotent(self):
        collected = self.add_collection(
            source_id="SYNTHETIC-IDEMPOTENT",
            body=feed(guid="idempotent", text="A synthetic source item."),
        )
        first = normalize_collection(self.database, collected.collection_id)
        second = normalize_collection(self.database, collected.collection_id)
        self.assertEqual(first, second)

    def test_storage_rejects_normalized_text_not_derived_from_snapshot(self):
        collected = self.add_collection(
            source_id="SYNTHETIC-FORGED",
            body=feed(guid="forged", text="Original source wording."),
        )
        document = normalize_collection(self.database, collected.collection_id)[0][0]
        forged_text = "Fabricated source wording."
        forged = replace(
            document,
            original_text=forged_text,
            normalized_text=forged_text,
            text_sha256=hashlib.sha256(forged_text.encode()).hexdigest(),
            normalized_text_sha256=hashlib.sha256(forged_text.encode()).hexdigest(),
        )
        with self.assertRaisesRegex(StorageError, "do not match archived collection"):
            self.database.save_normalized_documents([forged])

    def test_url_and_unicode_normalization_preserve_original_values(self):
        self.assertEqual(normalize_text("A\u00a0B  Cafe\u0301"), "A B Café")
        self.assertEqual(
            normalize_url(
                "HTTPS://Publisher.Example:443/a/../story/?utm_source=feed&id=4#section"
            ),
            "https://publisher.example/story/?id=4",
        )
        self.assertEqual(
            normalize_url("https://publisher.example/story?token=secret"),
            "https://publisher.example/story",
        )

    def test_translation_and_duplicate_cli_commands_are_auditable(self):
        original = "This synthetic document contains enough original text for the audit."
        collected = self.add_collection(
            source_id="SYNTHETIC-CLI-A",
            body=feed(guid="cli-a", text=original, language="en"),
            registry_language="en",
        )
        second = self.add_collection(
            source_id="SYNTHETIC-CLI-B",
            body=feed(guid="cli-b", text=original, language="en"),
            registry_language="en",
        )
        document = normalize_collection(self.database, collected.collection_id)[0][0]
        _, duplicates = normalize_collection(self.database, second.collection_id)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "source",
                    "duplicates",
                    "--document-id",
                    document.document_id,
                    "--database",
                    str(self.database_path),
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("EXACT_TEXT_MATCH", output.getvalue())
        self.assertIn("PENDING", output.getvalue())
        self.assertEqual(len(duplicates), 1)

        text_path = Path(self.temp_dir.name) / "translated.txt"
        text_path.write_text("Polski tekst.", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "source",
                    "translation-add",
                    document.document_id,
                    "--target-language",
                    "pl",
                    "--method",
                    "manual",
                    "--translator",
                    "analyst",
                    "--text-file",
                    str(text_path),
                    "--translated-at",
                    "2026-10-07T10:30:00Z",
                    "--database",
                    str(self.database_path),
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("en->pl", output.getvalue())
        self.assertIn(document.text_sha256, output.getvalue())

    def test_document_and_translation_records_cannot_be_mutated_or_deleted(self):
        collected = self.add_collection(
            source_id="SYNTHETIC-IMMUTABLE",
            body=feed(guid="immutable", text="Original synthetic text.", language="en"),
            registry_language="en",
        )
        document = normalize_collection(self.database, collected.collection_id)[0][0]
        translation = self.database.add_translation(
            document_id=document.document_id,
            translated_text="Tekst.",
            target_language="pl",
            translation_method="manual",
            translator="analyst",
            translated_at="2026-10-07T10:30:00Z",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE normalized_documents SET normalized_text = 'changed' "
                    "WHERE document_id = ?",
                    (document.document_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM translations WHERE translation_id = ?",
                    (translation.translation_id,),
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
