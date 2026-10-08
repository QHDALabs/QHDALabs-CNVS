import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from cnvs.cli import main
from cnvs.models import Event, EventSourceMatch
from cnvs.normalization import normalize_collection
from cnvs.storage import Database, StorageError
from cnvs.validation import parse_record


ROOT = Path(__file__).resolve().parents[1]


def make_event(event_id: str = "CNVS-EVT-2026-10-08-0001") -> Event:
    payload = json.loads(
        (ROOT / "examples" / "event.json").read_text(encoding="utf-8")
    )
    payload["event_id"] = event_id
    return parse_record("event", payload)


class EventWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cnvs.sqlite3"
        self.database = Database(self.database_path)
        self.event = make_event()
        self.database.create_event(self.event)

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_document(
        self,
        source_id: str,
        guid: str,
        published_at: str,
        collected_at: str,
    ) -> str:
        url = f"https://{source_id.lower()}.example/feed"
        content = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0"><channel><language>en</language><item>'
            f"<guid>{guid}</guid><title>{guid} synthetic incident report</title>"
            f"<link>https://publisher.example/{guid}</link>"
            f"<pubDate>{published_at}</pubDate>"
            "<description>Synthetic report about an incident timeline.</description>"
            "</item></channel></rss>"
        ).encode()
        result = self.database.record_collection_result(
            source_id=source_id,
            source_metadata={"publisher": source_id, "language": "en"},
            source_url=url,
            status="SUCCEEDED",
            started_at="2026-10-08T10:00:00Z",
            completed_at=collected_at,
            final_url=url,
            http_status=200,
            content_type="application/rss+xml",
            content=content,
            archive_content=True,
        )
        documents, _ = normalize_collection(self.database, result.collection_id)
        self.assertEqual(len(documents), 1)
        return documents[0].document_id

    def propose_and_link(self, document_id: str) -> EventSourceMatch:
        match = self.database.propose_event_source_match(
            event_id=self.event.event_id,
            document_id=document_id,
            proposed_by="analyst-a",
            proposed_at="2026-10-08T11:00:00Z",
            rationale="The source describes the same location and incident window.",
        )
        self.assertEqual(match.status, "PENDING")
        return self.database.review_event_source_match(
            match_id=match.match_id,
            decision="LINKED",
            reviewed_by="reviewer-b",
            reviewed_at="2026-10-08T11:05:00Z",
            rationale="Manual comparison supports this event link.",
        )

    def test_event_creation_update_and_cli_json_workflow(self):
        second = make_event("CNVS-EVT-2026-10-08-0002")
        input_path = Path(self.temp_dir.name) / "event.json"
        input_path.write_text(
            json.dumps(
                {
                    "schema_version": second.schema_version,
                    "event_id": second.event_id,
                    "event_type": second.event_type,
                    "title": second.title,
                    "description": second.description,
                    "status": second.status,
                    "created_at": second.created_at,
                    "updated_at": second.updated_at,
                    "start_time": second.start_time,
                    "end_time": second.end_time,
                    "location": second.location,
                    "country": second.country,
                    "coordinates": second.coordinates,
                    "entities": second.entities,
                }
            ),
            encoding="utf-8",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "event",
                    "create",
                    "--file",
                    str(input_path),
                    "--database",
                    str(self.database_path),
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn("revision 1", output.getvalue())

        updated = replace(second, title="Corrected synthetic event title")
        input_path.write_text(
            json.dumps(
                {
                    **json.loads(input_path.read_text(encoding="utf-8")),
                    "title": updated.title,
                }
            ),
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "event",
                        "update",
                        "--file",
                        str(input_path),
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertEqual(self.database.revisions("event", second.event_id), [1, 2])
        self.assertEqual(
            self.database.get("event", second.event_id).title,
            "Corrected synthetic event title",
        )
        self.assertEqual(len(self.database.list_events()), 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "event",
                        "show",
                        second.event_id,
                        "--revision",
                        "1",
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn(second.title, output.getvalue())
        with self.assertRaisesRegex(StorageError, "already exists"):
            self.database.create_event(second)
        invalid_range = replace(
            second,
            start_time="2026-10-08T12:00:00Z",
            end_time="2026-10-08T11:00:00Z",
        )
        with self.assertRaisesRegex(StorageError, "must not precede"):
            self.database.create_event(invalid_range)

    def test_source_match_proposals_require_explicit_review_and_keep_candidates(self):
        document_id = self.add_document(
            "SYNTHETIC-MATCH", "incident-1", "Tue, 07 Oct 2025 09:00:00 +0000",
            "2026-10-08T10:10:00Z",
        )
        second_event = make_event("CNVS-EVT-2026-10-08-0002")
        self.database.create_event(second_event)
        first = self.database.propose_event_source_match(
            event_id=self.event.event_id,
            document_id=document_id,
            proposed_by="analyst-a",
            proposed_at="2026-10-08T11:00:00Z",
            rationale="Possible match based on location.",
        )
        other_candidate = self.database.propose_event_source_match(
            event_id=second_event.event_id,
            document_id=document_id,
            proposed_by="analyst-a",
            proposed_at="2026-10-08T11:01:00Z",
            rationale="Alternative event under consideration.",
        )
        self.assertEqual(first.status, "PENDING")
        self.assertEqual(other_candidate.status, "PENDING")
        self.assertEqual(
            len(
                self.database.event_source_matches(
                    document_id=document_id, status="PENDING"
                )
            ),
            2,
        )
        self.assertEqual(self.database.timeline_entries(self.event.event_id), [])

        reviewed = self.database.review_event_source_match(
            match_id=first.match_id,
            decision="UNRESOLVED",
            reviewed_by="reviewer-b",
            reviewed_at="2026-10-08T11:10:00Z",
            rationale="Available details do not distinguish the candidate events.",
        )
        self.assertEqual(reviewed.status, "UNRESOLVED")
        self.assertEqual(self.database.timeline_entries(self.event.event_id), [])
        with self.assertRaisesRegex(StorageError, "already exists"):
            self.database.propose_event_source_match(
                event_id=self.event.event_id,
                document_id=document_id,
                proposed_by="analyst-a",
                proposed_at="2026-10-08T11:15:00Z",
                rationale="Duplicate proposal.",
            )

        reviewed_again = self.database.review_event_source_match(
            match_id=first.match_id,
            decision="LINKED",
            reviewed_by="reviewer-c",
            reviewed_at="2026-10-08T12:00:00Z",
            rationale="A new primary source resolves the earlier ambiguity.",
        )
        self.assertEqual(reviewed_again.status, "LINKED")
        self.assertEqual(reviewed_again.source_id, "SYNTHETIC-MATCH")
        self.assertEqual(
            [
                item.status
                for item in self.database.event_source_match_history(first.match_id)
            ],
            ["PENDING", "UNRESOLVED", "LINKED"],
        )
        timeline = self.database.timeline_entries(self.event.event_id)
        self.assertEqual(len(timeline), 1)
        self.assertIsNone(timeline[0].event_time)
        self.assertEqual(
            self.database.get_event_source_match(first.match_id).review_rationale,
            "A new primary source resolves the earlier ambiguity.",
        )

    def test_timeline_keeps_times_distinct_sorts_and_audits_corrections(self):
        earlier_doc = self.add_document(
            "SYNTHETIC-EARLIER", "earlier-report",
            "Tue, 07 Oct 2025 09:00:00 +0200", "2026-10-08T10:30:00Z",
        )
        later_doc = self.add_document(
            "SYNTHETIC-LATER", "later-report",
            "Wed, 08 Oct 2025 12:00:00 +0000", "2026-10-08T10:20:00Z",
        )
        self.propose_and_link(later_doc)
        self.propose_and_link(earlier_doc)

        entries = self.database.timeline_entries(self.event.event_id)
        self.assertEqual(
            [entry.document_id for entry in entries], [earlier_doc, later_doc]
        )
        first = entries[0]
        self.assertEqual(
            first.original_publication_time, "Tue, 07 Oct 2025 09:00:00 +0200"
        )
        self.assertEqual(first.normalized_publication_time, "2025-10-07T07:00:00Z")
        self.assertTrue(first.publication_timezone_known)
        self.assertEqual(first.collection_time, "2026-10-08T10:30:00Z")
        self.assertEqual(first.source_id, "SYNTHETIC-EARLIER")
        self.assertEqual(first.publisher, "SYNTHETIC-EARLIER")
        self.assertIsNone(first.event_time)

        revised = self.database.revise_timeline_entry(
            entry_id=first.entry_id,
            event_time="2025-10-07T06:30:00Z",
            updated_by="analyst-c",
            updated_at="2026-10-08T12:00:00Z",
            rationale="Primary notice confirms the occurrence time.",
        )
        self.assertEqual(revised.revision, 2)
        self.assertEqual(revised.event_time, "2025-10-07T06:30:00Z")
        history = self.database.timeline_entry_history(first.entry_id)
        self.assertEqual([entry.revision for entry in history], [1, 2])
        self.assertIsNone(history[0].event_time)
        self.assertEqual(history[1].updated_by, "analyst-c")
        self.assertEqual(
            self.database.timeline_entries(self.event.event_id)[0].entry_id,
            first.entry_id,
        )

        cleared = self.database.revise_timeline_entry(
            entry_id=first.entry_id,
            event_time=None,
            updated_by="analyst-d",
            updated_at="2026-10-08T12:30:00Z",
            rationale="The notice time refers to publication, not occurrence.",
        )
        self.assertEqual(cleared.revision, 3)
        self.assertIsNone(cleared.event_time)
        self.assertEqual(len(self.database.timeline_entry_history(first.entry_id)), 3)

    def test_only_confirmed_matches_can_be_corrected_and_revisions_are_immutable(self):
        document_id = self.add_document(
            "SYNTHETIC-PENDING", "pending-report",
            "Tue, 07 Oct 2025 09:00:00 +0000", "2026-10-08T10:10:00Z",
        )
        match = self.database.propose_event_source_match(
            event_id=self.event.event_id,
            document_id=document_id,
            proposed_by="analyst-a",
            proposed_at="2026-10-08T11:00:00Z",
            rationale="Candidate needs review.",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "confirmed event/source match"
            ):
                connection.execute(
                    """
                    INSERT INTO event_timeline_entries (
                        entry_id, event_id, document_id, revision, event_time,
                        event_time_rationale, updated_by, updated_at
                    ) VALUES (?, ?, ?, 1, NULL, ?, ?, ?)
                    """,
                    (
                        "pending-link-entry",
                        self.event.event_id,
                        document_id,
                        "Not reviewed",
                        "analyst",
                        "2026-10-08T11:05:00Z",
                    ),
                )
        finally:
            connection.close()
        self.database.review_event_source_match(
            match_id=match.match_id,
            decision="LINKED",
            reviewed_by="reviewer-b",
            reviewed_at="2026-10-08T11:10:00Z",
            rationale="Confirmed.",
        )
        entry = self.database.timeline_entries(self.event.event_id)[0]
        self.database.review_event_source_match(
            match_id=match.match_id,
            decision="REJECTED",
            reviewed_by="reviewer-c",
            reviewed_at="2026-10-08T11:15:00Z",
            rationale="New evidence invalidates the match.",
        )
        self.assertEqual(self.database.timeline_entries(self.event.event_id), [])
        with self.assertRaisesRegex(StorageError, "only be corrected"):
            self.database.revise_timeline_entry(
                entry_id=entry.entry_id,
                event_time="2025-10-07T08:00:00Z",
                updated_by="analyst-a",
                updated_at="2026-10-08T11:20:00Z",
                rationale="Attempted correction.",
            )
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE event_timeline_entries SET event_time = ? "
                    "WHERE entry_id = ? AND revision = 1",
                    ("2025-10-07T08:00:00Z", entry.entry_id),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM event_source_match_reviews WHERE match_id = ?",
                    (match.match_id,),
                )
        finally:
            connection.close()

    def test_cli_match_review_and_timeline_expose_audit_context(self):
        document_id = self.add_document(
            "SYNTHETIC-CLI", "cli-report",
            "Tue, 07 Oct 2025 09:00:00 +0000", "2026-10-08T10:10:00Z",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "event", "match", self.event.event_id, document_id,
                        "--proposed-by", "analyst-a", "--rationale", "Candidate",
                        "--proposed-at", "2026-10-08T11:00:00Z",
                        "--database", str(self.database_path),
                    ]
                ),
                0,
            )
        match_id = output.getvalue().split()[0]
        self.assertIn("status=PENDING", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "event", "review-match", match_id,
                        "--decision", "LINKED", "--reviewer", "reviewer-b",
                        "--rationale", "Manually corroborated.",
                        "--reviewed-at", "2026-10-08T11:10:00Z",
                        "--database", str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn("status=LINKED", output.getvalue())
        self.assertIn("source=SYNTHETIC-CLI", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "event", "match-history", match_id,
                        "--database", str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn("status=PENDING", output.getvalue())
        self.assertIn("status=LINKED", output.getvalue())
        entry_id = self.database.timeline_entries(self.event.event_id)[0].entry_id
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "event", "timeline-edit", entry_id,
                        "--event-time", "2025-10-07T08:00:00Z",
                        "--analyst", "analyst-c", "--rationale", "Primary notice",
                        "--database", str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn("event_time=2025-10-07T08:00:00Z", output.getvalue())
        self.assertEqual(
            len(self.database.timeline_entries(self.event.event_id)), 1
        )


if __name__ == "__main__":
    unittest.main()
