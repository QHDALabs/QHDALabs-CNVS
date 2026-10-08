import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cnvs.cli import main
from cnvs.models import Event, Evidence, Source
from cnvs.normalization import normalize_collection
from cnvs.storage import Database, StorageError
from cnvs.validation import parse_record


ROOT = Path(__file__).resolve().parents[1]
TIMES = (
    "2026-10-08T10:00:00Z",
    "2026-10-08T10:01:00Z",
    "2026-10-08T10:02:00Z",
    "2026-10-08T10:03:00Z",
    "2026-10-08T10:04:00Z",
    "2026-10-08T10:05:00Z",
    "2026-10-08T10:06:00Z",
    "2026-10-08T10:07:00Z",
    "2026-10-08T10:08:00Z",
    "2026-10-08T10:09:00Z",
)


def fixture_record(record_type: str, filename: str, **updates):
    payload = json.loads((ROOT / "examples" / filename).read_text(encoding="utf-8"))
    payload.update(updates)
    return parse_record(record_type, payload)


class ProvenanceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cnvs.sqlite3"
        self.database = Database(self.database_path)
        event = fixture_record(
            "event",
            "event.json",
            event_id="CNVS-EVT-2026-10-08-0001",
        )
        if not isinstance(event, Event):
            raise AssertionError("The event fixture did not parse as Event.")
        self.event = event
        self.database.create_event(self.event)
        self.documents = [
            self.add_document(f"PROVENANCE-SOURCE-{index}", f"doc-{index}")
            for index in range(1, 4)
        ]

    def tearDown(self):
        self.temp_dir.cleanup()

    def add_document(self, source_id: str, guid: str) -> str:
        feed_url = f"https://{source_id.lower()}.example/feed"
        feed = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0"><channel><language>en</language><item>'
            f"<guid>{guid}</guid><title>{guid} synthetic report</title>"
            f"<link>https://{source_id.lower()}.example/{guid}</link>"
            "<pubDate>Tue, 07 Oct 2025 09:00:00 +0000</pubDate>"
            f"<description>Report {guid} about the fictional incident.</description>"
            "</item></channel></rss>"
        ).encode("utf-8")
        collection = self.database.record_collection_result(
            source_id=source_id,
            source_metadata={"publisher": source_id, "language": "en"},
            source_url=feed_url,
            status="SUCCEEDED",
            started_at=TIMES[0],
            completed_at=TIMES[1],
            final_url=feed_url,
            http_status=200,
            content_type="application/rss+xml",
            content=feed,
            archive_content=True,
        )
        documents, _ = normalize_collection(self.database, collection.collection_id)
        self.assertEqual(len(documents), 1)
        return documents[0].document_id

    def propose_link(
        self, document_id: str, upstream_document_id: str, relationship_type="CITES"
    ):
        return self.database.propose_provenance_link(
            document_id=document_id,
            upstream_document_id=upstream_document_id,
            relationship_type=relationship_type,
            proposed_by="analyst-a",
            proposed_at=TIMES[2],
            rationale="The synthetic document explicitly identifies its source.",
        )

    def confirm_link(self, link):
        return self.database.review_provenance_link(
            link_id=link.link_id,
            decision="CONFIRMED",
            reviewed_by="reviewer-b",
            reviewed_at=TIMES[3],
            rationale="The source relationship is directly documented.",
        )

    def test_reviewed_links_trace_upstream_and_reject_cycles(self):
        origin, middle, latest = self.documents
        latest_link = self.propose_link(latest, middle, "SYNDICATED")
        self.assertEqual(latest_link.review_status, "PENDING")
        self.assertEqual(self.database.provenance_graph(latest), [])
        latest_link = self.confirm_link(latest_link)
        middle_link = self.confirm_link(
            self.propose_link(middle, origin, "DERIVED_FROM")
        )

        graph = self.database.provenance_graph(latest)
        self.assertEqual(
            {link.link_id for link in graph},
            {latest_link.link_id, middle_link.link_id},
        )
        self.assertEqual(
            {link.source_id for link in graph},
            {"PROVENANCE-SOURCE-2", "PROVENANCE-SOURCE-3"},
        )
        self.assertEqual(
            self.database.provenance_links(
                document_id=latest, review_status="CONFIRMED"
            ),
            [latest_link],
        )
        history = self.database.provenance_link_history(latest_link.link_id)
        self.assertEqual(
            [link.review_status for link in history], ["PENDING", "CONFIRMED"]
        )

        cycle = self.propose_link(origin, latest)
        with self.assertRaisesRegex(StorageError, "would create a cycle"):
            self.confirm_link(cycle)
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "must not create a cycle"):
                connection.execute(
                    """
                    INSERT INTO provenance_link_reviews (
                        review_id, link_id, decision, reviewed_by, reviewed_at, rationale
                    ) VALUES ('manual-cycle', ?, 'CONFIRMED', 'reviewer',
                        ?, 'Direct database cycle guard test.')
                    """,
                    (cycle.link_id, TIMES[4]),
                )
        finally:
            connection.close()
        self.assertEqual(self.database.get_provenance_link(cycle.link_id).review_status, "PENDING")

    def test_origin_history_records_identified_uncertain_and_unknown(self):
        origin, middle, latest = self.documents
        middle_link = self.confirm_link(self.propose_link(middle, origin))
        latest_link = self.confirm_link(self.propose_link(latest, middle))
        identified = self.database.record_provenance_origin(
            document_id=latest,
            origin_status="IDENTIFIED",
            earliest_origin_document_id=origin,
            assessed_by="analyst",
            assessed_at=TIMES[4],
            rationale="The confirmed citation chain leads to the earliest archived item.",
            supporting_link_ids=[latest_link.link_id, middle_link.link_id],
        )
        self.assertEqual(identified.origin_status, "IDENTIFIED")
        self.assertEqual(identified.earliest_origin_document_id, origin)
        self.assertEqual(
            set(identified.supporting_link_ids),
            {latest_link.link_id, middle_link.link_id},
        )
        with self.assertRaisesRegex(StorageError, "do not connect"):
            self.database.record_provenance_origin(
                document_id=latest,
                origin_status="IDENTIFIED",
                earliest_origin_document_id=origin,
                assessed_by="analyst",
                assessed_at=TIMES[5],
                rationale="Intentionally incomplete support.",
                supporting_link_ids=[latest_link.link_id],
            )
        uncertain = self.database.record_provenance_origin(
            document_id=latest,
            origin_status="UNCERTAIN",
            earliest_origin_document_id=middle,
            assessed_by="analyst-two",
            assessed_at=TIMES[5],
            rationale="An earlier report may exist outside the collected material.",
            supporting_link_ids=[latest_link.link_id],
        )
        unknown = self.database.record_provenance_origin(
            document_id=latest,
            origin_status="UNKNOWN",
            earliest_origin_document_id=None,
            assessed_by="analyst-three",
            assessed_at=TIMES[6],
            rationale="No traceable upstream publication was found.",
        )
        self.assertEqual(uncertain.origin_status, "UNCERTAIN")
        self.assertEqual(unknown.origin_status, "UNKNOWN")
        self.assertIsNone(unknown.earliest_origin_document_id)
        self.assertEqual(
            [
                row.origin_status
                for row in self.database.provenance_origin_history(latest)
            ],
            ["IDENTIFIED", "UNCERTAIN", "UNKNOWN"],
        )
        self.assertEqual(
            self.database.latest_provenance_origin(latest), unknown
        )

    def test_independence_membership_requires_review_and_keeps_audit_trail(self):
        origin, middle, latest = self.documents
        link = self.confirm_link(self.propose_link(latest, middle, "SYNDICATED"))
        first = self.database.propose_independence_assignment(
            group_id="DEPENDENT-CHAIN-1",
            member_type="DOCUMENT",
            member_id=latest,
            proposed_by="analyst",
            proposed_at=TIMES[4],
            rationale="This report republishes the upstream source.",
        )
        second = self.database.propose_independence_assignment(
            group_id="DEPENDENT-CHAIN-1",
            member_type="DOCUMENT",
            member_id=middle,
            proposed_by="analyst",
            proposed_at=TIMES[4],
            rationale="This is the identified upstream publication.",
        )
        self.assertEqual(first.review_status, "PENDING")
        self.assertEqual(
            self.database.independence_assignments(
                group_id="DEPENDENT-CHAIN-1", review_status="ACCEPTED"
            ),
            [],
        )
        with self.assertRaisesRegex(StorageError, "pending or accepted"):
            self.database.propose_independence_assignment(
                group_id="OTHER-GROUP",
                member_type="DOCUMENT",
                member_id=latest,
                proposed_by="analyst",
                proposed_at=TIMES[8],
                rationale="A conflicting proposal must not be active.",
            )
        unrelated_link = self.confirm_link(
            self.propose_link(origin, middle, "CITES")
        )
        with self.assertRaisesRegex(StorageError, "do not involve"):
            self.database.review_independence_assignment(
                assignment_id=first.assignment_id,
                decision="ACCEPTED",
                reviewed_by="reviewer",
                reviewed_at=TIMES[5],
                rationale="The link does not concern this member.",
                supporting_link_ids=[unrelated_link.link_id],
            )
        accepted_first = self.database.review_independence_assignment(
            assignment_id=first.assignment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer",
            reviewed_at=TIMES[5],
            rationale="The confirmed syndication link supports dependence.",
            supporting_link_ids=[link.link_id],
        )
        accepted_second = self.database.review_independence_assignment(
            assignment_id=second.assignment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer",
            reviewed_at=TIMES[5],
            rationale="Both documents share the same reporting chain.",
            supporting_link_ids=[link.link_id],
        )
        self.assertEqual(
            {item.member_id for item in self.database.independence_assignments(
                group_id="DEPENDENT-CHAIN-1", review_status="ACCEPTED"
            )},
            {middle, latest},
        )
        self.assertEqual(accepted_first.supporting_link_ids, (link.link_id,))
        self.assertEqual(accepted_second.review_status, "ACCEPTED")
        self.assertEqual(
            [
                item.review_status
                for item in self.database.independence_assignment_history(
                    first.assignment_id
                )
            ],
            ["PENDING", "ACCEPTED"],
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                """
                INSERT INTO independence_assignments (
                    assignment_id, group_id, member_type, member_id,
                    proposed_by, proposed_at, rationale
                ) VALUES ('direct-conflict', 'OTHER-GROUP', 'DOCUMENT', ?,
                    'analyst', ?, 'Direct database invariant test.')
                """,
                (latest, TIMES[6]),
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "already has an accepted group|must share an independence group",
            ):
                connection.execute(
                    """
                    INSERT INTO independence_assignment_reviews (
                        review_id, assignment_id, decision, reviewed_by,
                        reviewed_at, rationale
                    ) VALUES ('direct-conflict-review', 'direct-conflict',
                        'ACCEPTED', 'reviewer', ?, 'Conflict test.')
                    """,
                    (TIMES[7],),
                )
        finally:
            connection.close()

        separate_assignment = self.database.propose_independence_assignment(
            group_id="INDEPENDENT-CHAIN",
            member_type="DOCUMENT",
            member_id=origin,
            proposed_by="analyst",
            proposed_at=TIMES[8],
            rationale="Separate grouping before a syndication link is known.",
        )
        self.database.review_independence_assignment(
            assignment_id=separate_assignment.assignment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer",
            reviewed_at=TIMES[8],
            rationale="No dependency link has been confirmed yet.",
        )
        proposed_syndication = self.propose_link(origin, latest, "SYNDICATED")
        with self.assertRaisesRegex(StorageError, "different independence groups"):
            self.confirm_link(proposed_syndication)
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "different independence groups"
            ):
                connection.execute(
                    """
                    INSERT INTO provenance_link_reviews (
                        review_id, link_id, decision, reviewed_by,
                        reviewed_at, rationale
                    ) VALUES ('direct-syndication-review', ?, 'CONFIRMED',
                        'reviewer', ?, 'Direct database guard test.')
                    """,
                    (proposed_syndication.link_id, TIMES[9]),
                )
        finally:
            connection.close()
        self.database.review_independence_assignment(
            assignment_id=separate_assignment.assignment_id,
            decision="UNRESOLVED",
            reviewed_by="reviewer",
            reviewed_at=TIMES[9],
            rationale="The newly reviewed syndication link changes the grouping.",
        )
        aligned_assignment = self.database.propose_independence_assignment(
            group_id="DEPENDENT-CHAIN-1",
            member_type="DOCUMENT",
            member_id=origin,
            proposed_by="analyst",
            proposed_at=TIMES[8],
            rationale="Align the origin with its confirmed syndication chain.",
        )
        self.database.review_independence_assignment(
            assignment_id=aligned_assignment.assignment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer",
            reviewed_at=TIMES[9],
            rationale="The chain shares a single analyst-reviewed group.",
        )
        self.confirm_link(proposed_syndication)
        self.database.review_independence_assignment(
            assignment_id=aligned_assignment.assignment_id,
            decision="UNRESOLVED",
            reviewed_by="reviewer",
            reviewed_at=TIMES[9],
            rationale="Prepare a conflicting proposal for the guard test.",
        )
        conflicting_assignment = self.database.propose_independence_assignment(
            group_id="INDEPENDENT-CHAIN",
            member_type="DOCUMENT",
            member_id=origin,
            proposed_by="analyst",
            proposed_at=TIMES[8],
            rationale="Intentionally conflicting group for the test.",
        )
        with self.assertRaisesRegex(StorageError, "must use the same group"):
            self.database.review_independence_assignment(
                assignment_id=conflicting_assignment.assignment_id,
                decision="ACCEPTED",
                reviewed_by="reviewer",
                reviewed_at=TIMES[9],
                rationale="A confirmed syndication chain cannot split groups.",
            )
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "must share an independence group"
            ):
                connection.execute(
                    """
                    INSERT INTO independence_assignment_reviews (
                        review_id, assignment_id, decision, reviewed_by,
                        reviewed_at, rationale
                    ) VALUES ('direct-syndication-conflict', ?, 'ACCEPTED',
                        'reviewer', ?, 'Direct database guard test.')
                    """,
                    (conflicting_assignment.assignment_id, TIMES[9]),
                )
        finally:
            connection.close()

        with self.assertRaisesRegex(StorageError, "not confirmed"):
            self.database.review_independence_assignment(
                assignment_id=first.assignment_id,
                decision="UNRESOLVED",
                reviewed_by="reviewer",
                reviewed_at=TIMES[6],
                rationale="Unconfirmed support is invalid.",
                supporting_link_ids=[
                    self.propose_link(origin, middle).link_id
                ],
            )

    def test_source_and_evidence_can_be_assigned_and_reassignment_is_audited(self):
        source = fixture_record(
            "source",
            "source.json",
            source_id="SYNTHETIC-EVIDENCE-SOURCE",
        )
        evidence = fixture_record(
            "evidence",
            "evidence.json",
            evidence_id="SYNTHETIC-EVIDENCE-001",
            event_id=self.event.event_id,
            source_id="SYNTHETIC-EVIDENCE-SOURCE",
        )
        if not isinstance(source, Source) or not isinstance(evidence, Evidence):
            raise AssertionError("Source/evidence fixtures did not parse as expected.")
        self.database.save(source)
        self.database.save(evidence)
        source_assignment = self.database.propose_independence_assignment(
            group_id="PRIMARY-OBSERVATION-1",
            member_type="SOURCE",
            member_id=source.source_id,
            proposed_by="analyst",
            proposed_at=TIMES[4],
            rationale="The source purports to provide a direct observation.",
        )
        evidence_assignment = self.database.propose_independence_assignment(
            group_id="PRIMARY-OBSERVATION-1",
            member_type="EVIDENCE",
            member_id=evidence.evidence_id,
            proposed_by="analyst",
            proposed_at=TIMES[4],
            rationale="The evidence record derives from the same observation.",
        )
        self.database.review_independence_assignment(
            assignment_id=source_assignment.assignment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer",
            reviewed_at=TIMES[5],
            rationale="Source classification reviewed.",
        )
        self.database.review_independence_assignment(
            assignment_id=evidence_assignment.assignment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer",
            reviewed_at=TIMES[5],
            rationale="Evidence classification reviewed.",
        )
        with self.assertRaisesRegex(StorageError, "pending or accepted"):
            self.database.propose_independence_assignment(
                group_id="SECOND-EVIDENCE-GROUP",
                member_type="EVIDENCE",
                member_id=evidence.evidence_id,
                proposed_by="analyst-two",
                proposed_at=TIMES[6],
                rationale="A member with a current group cannot be reassigned.",
            )
        self.database.review_independence_assignment(
            assignment_id=evidence_assignment.assignment_id,
            decision="UNRESOLVED",
            reviewed_by="reviewer-two",
            reviewed_at=TIMES[7],
            rationale="The independence relationship requires further review.",
        )
        reassignment = self.database.propose_independence_assignment(
            group_id="UNRESOLVED-CHAIN",
            member_type="EVIDENCE",
            member_id=evidence.evidence_id,
            proposed_by="analyst-two",
            proposed_at=TIMES[8],
            rationale="New grouping proposal after the prior one was unresolved.",
        )
        self.assertEqual(reassignment.review_status, "PENDING")
        reassigned = self.database.review_independence_assignment(
            assignment_id=reassignment.assignment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer-three",
            reviewed_at=TIMES[9],
            rationale="The revised group assignment is approved.",
        )
        self.assertEqual(reassigned.review_status, "ACCEPTED")

    def test_cli_can_create_review_trace_origin_and_list_group_members(self):
        _, middle, latest = self.documents
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "provenance",
                        "link",
                        latest,
                        middle,
                        "--type",
                        "SYNDICATED",
                        "--proposed-by",
                        "analyst",
                        "--rationale",
                        "Synthetic republication.",
                        "--proposed-at",
                        TIMES[2],
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn("SYNDICATED", output.getvalue())
        link_id = output.getvalue().split()[0]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "provenance",
                        "review-link",
                        link_id,
                        "--decision",
                        "CONFIRMED",
                        "--reviewer",
                        "reviewer",
                        "--rationale",
                        "The publisher credits the upstream item.",
                        "--reviewed-at",
                        TIMES[3],
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn("status=CONFIRMED", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "provenance",
                        "origin",
                        latest,
                        "--status",
                        "IDENTIFIED",
                        "--earliest-origin-document-id",
                        middle,
                        "--supporting-link",
                        link_id,
                        "--analyst",
                        "analyst",
                        "--rationale",
                        "The confirmed link identifies its source.",
                        "--assessed-at",
                        TIMES[4],
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn(f"earliest_origin={middle}", output.getvalue())

        assignment = self.database.propose_independence_assignment(
            group_id="CLI-DEPENDENT-CHAIN",
            member_type="DOCUMENT",
            member_id=latest,
            proposed_by="analyst",
            proposed_at=TIMES[5],
            rationale="The item is a republished copy.",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "provenance",
                        "review-assignment",
                        assignment.assignment_id,
                        "--decision",
                        "ACCEPTED",
                        "--reviewer",
                        "reviewer",
                        "--rationale",
                        "The same reporting chain is documented.",
                        "--supporting-link",
                        link_id,
                        "--reviewed-at",
                        TIMES[9],
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn("status=ACCEPTED", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "provenance",
                        "graph",
                        latest,
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn(link_id, output.getvalue())
        self.assertIn("origin=IDENTIFIED", output.getvalue())

        match = self.database.propose_event_source_match(
            event_id=self.event.event_id,
            document_id=latest,
            proposed_by="analyst",
            proposed_at=TIMES[6],
            rationale="The synthetic document is relevant to the event.",
        )
        self.database.review_event_source_match(
            match_id=match.match_id,
            decision="LINKED",
            reviewed_by="reviewer",
            reviewed_at=TIMES[7],
            rationale="Confirmed for the synthetic test workflow.",
        )
        document = next(
            item
            for item in self.database.normalized_documents()
            if item.document_id == latest
        )
        candidate = self.database.create_claim_extractions(
            event_id=self.event.event_id,
            document_id=latest,
            candidates=[
                {
                    "span_start": 0,
                    "span_end": len(document.original_text),
                    "subject": "synthetic incident",
                    "predicate": "described",
                    "object": "in report",
                    "claim_type": "INCIDENT",
                    "attribution": None,
                    "modality": "REPORTED",
                }
            ],
            extraction_method="synthetic test input",
            extractor="analyst",
            created_at=TIMES[8],
        )[0]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "provenance",
                        "claim",
                        candidate.candidate_id,
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn(candidate.candidate_id, output.getvalue())
        self.assertIn(link_id, output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "provenance",
                        "assignments",
                        "--group-id",
                        "CLI-DEPENDENT-CHAIN",
                        "--status",
                        "ACCEPTED",
                        "--database",
                        str(self.database_path),
                    ]
                ),
                0,
            )
        self.assertIn(assignment.assignment_id, output.getvalue())

    def test_database_enforces_immutability_for_provenance_and_group_records(self):
        _, middle, latest = self.documents
        link = self.confirm_link(self.propose_link(latest, middle))
        assignment = self.database.propose_independence_assignment(
            group_id="IMMUTABLE-CHAIN",
            member_type="DOCUMENT",
            member_id=latest,
            proposed_by="analyst",
            proposed_at=TIMES[4],
            rationale="Synthetic group membership.",
        )
        self.database.review_independence_assignment(
            assignment_id=assignment.assignment_id,
            decision="ACCEPTED",
            reviewed_by="reviewer",
            reviewed_at=TIMES[5],
            rationale="Reviewed membership.",
            supporting_link_ids=[link.link_id],
        )
        self.database.record_provenance_origin(
            document_id=latest,
            origin_status="IDENTIFIED",
            earliest_origin_document_id=middle,
            assessed_by="analyst",
            assessed_at=TIMES[6],
            rationale="Link supports the origin.",
            supporting_link_ids=[link.link_id],
        )
        connection = sqlite3.connect(self.database_path)
        try:
            for statement, parameters in (
                (
                    "UPDATE provenance_links SET rationale = 'changed' WHERE link_id = ?",
                    (link.link_id,),
                ),
                (
                    "DELETE FROM provenance_origin_assessments WHERE document_id = ?",
                    (latest,),
                ),
                (
                    "UPDATE independence_assignments SET group_id = 'changed' "
                    "WHERE assignment_id = ?",
                    (assignment.assignment_id,),
                ),
                (
                    "DELETE FROM independence_assignment_reviews WHERE assignment_id = ?",
                    (assignment.assignment_id,),
                ),
            ):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    connection.execute(statement, parameters)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
