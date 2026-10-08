import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from cnvs import __version__
from cnvs.collection import collect_source, find_registration, retry_collection
from cnvs.configuration import (
    CountryCoverageCatalog,
    ConfigurationError,
    load_country_coverage_catalog,
    validate_configuration,
)
from cnvs.models import (
    CollectionResult,
    Claim,
    ClaimEvidenceLink,
    ClaimExtraction,
    CountryCoveragePlan,
    CountryMatrixAssessment,
    DuplicateRelationship,
    Event,
    EventSourceMatch,
    Evidence,
    EvidenceGap,
    EvidenceNote,
    EvidenceReview,
    IndependenceAssignment,
    JsonValue,
    NationalInformationMatrix,
    NormalizedDocument,
    ProvenanceLink,
    ProvenanceOriginAssessment,
    Source,
    TimelineEntry,
    TranslationRecord,
)
from cnvs.normalization import (
    add_translation,
    normalize_collection,
)
from cnvs.registry import load_source_registry
from cnvs.settings import Settings
from cnvs.storage import Database, StorageError
from cnvs.validation import parse_record


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cnvs",
        description="CNVS project and configuration utilities.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    config_parser = commands.add_parser("config", help="Inspect runtime configuration.")
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    validate_parser = config_commands.add_parser(
        "validate", help="Validate the MVP YAML configuration set."
    )
    validate_parser.add_argument(
        "--config-dir",
        help="Configuration directory (overrides CNVS_CONFIG_DIR; defaults to ./config).",
    )
    database_parser = commands.add_parser("db", help="Manage the local database.")
    database_commands = database_parser.add_subparsers(
        dest="database_command", required=True
    )
    for name, help_text in (
        ("migrate", "Apply pending database migrations."),
        ("status", "Show applied database migrations."),
    ):
        command_parser = database_commands.add_parser(name, help=help_text)
        command_parser.add_argument(
            "--database",
            help=(
                "SQLite database file "
                "(overrides CNVS_DATABASE_PATH; defaults to .data/cnvs.sqlite3)."
            ),
        )
    source_parser = commands.add_parser(
        "source", help="Review and collect registered sources."
    )
    source_commands = source_parser.add_subparsers(
        dest="source_command", required=True
    )
    source_record = source_commands.add_parser(
        "record", help="Store or revise a canonical source record from JSON."
    )
    source_record.add_argument("--file", required=True, help="Source JSON file.")
    source_record.add_argument("--database", help="SQLite database file.")
    source_list = source_commands.add_parser(
        "list", help="List registered sources and review state."
    )
    source_list.add_argument("--config-dir", help="Configuration directory.")
    source_collect = source_commands.add_parser(
        "collect", help="Collect one approved RSS feed or public URL."
    )
    source_collect.add_argument("source_id")
    source_retry = source_commands.add_parser(
        "retry", help="Retry the latest failed attempt for a source."
    )
    source_retry.add_argument("collection_id")
    for command_parser in (source_collect, source_retry):
        command_parser.add_argument("--config-dir", help="Configuration directory.")
        command_parser.add_argument("--database", help="SQLite database file.")
    source_results = source_commands.add_parser(
        "results", help="Inspect collection results and failures."
    )
    source_results.add_argument("--source-id", help="Limit results to one registry source.")
    source_results.add_argument("--database", help="SQLite database file.")
    source_normalize = source_commands.add_parser(
        "normalize", help="Normalize a successful archived collection."
    )
    source_normalize.add_argument("collection_id")
    source_normalize.add_argument("--database", help="SQLite database file.")
    source_documents = source_commands.add_parser(
        "documents", help="Inspect extracted original and normalized documents."
    )
    source_documents.add_argument("--collection-id")
    source_documents.add_argument("--source-id")
    source_documents.add_argument("--database", help="SQLite database file.")
    source_duplicates = source_commands.add_parser(
        "duplicates", help="Inspect exact duplicate and possible syndication matches."
    )
    source_duplicates.add_argument("--document-id")
    source_duplicates.add_argument(
        "--status", choices=("PENDING", "REVIEWED")
    )
    source_duplicates.add_argument("--database", help="SQLite database file.")
    source_review = source_commands.add_parser(
        "review-duplicate", help="Append an analyst decision to a duplicate match."
    )
    source_review.add_argument("relationship_id")
    source_review.add_argument(
        "--decision",
        required=True,
        choices=(
            "CONFIRMED_DEPENDENT",
            "INDEPENDENT_EVIDENCE_DOCUMENTED",
            "REJECTED",
            "UNRESOLVED",
        ),
    )
    source_review.add_argument("--reviewer", required=True)
    source_review.add_argument("--rationale", required=True)
    source_review.add_argument("--reviewed-at")
    source_review.add_argument("--database", help="SQLite database file.")
    source_translation = source_commands.add_parser(
        "translation-add", help="Record an analyst- or tool-produced translation."
    )
    source_translation.add_argument("document_id")
    source_translation.add_argument("--target-language", required=True)
    source_translation.add_argument("--method", required=True)
    source_translation.add_argument("--translator", required=True)
    source_translation.add_argument("--text-file", required=True)
    source_translation.add_argument("--translated-at")
    source_translation.add_argument("--database", help="SQLite database file.")
    source_translation_list = source_commands.add_parser(
        "translations", help="Inspect translation provenance for one document."
    )
    source_translation_list.add_argument("document_id")
    source_translation_list.add_argument("--database", help="SQLite database file.")

    event_parser = commands.add_parser(
        "event", help="Create events, review source matches and maintain timelines."
    )
    event_commands = event_parser.add_subparsers(
        dest="event_command", required=True
    )
    for name, help_text in (
        ("create", "Create an event from a validated JSON record."),
        ("update", "Save an event correction as a new record revision."),
    ):
        command_parser = event_commands.add_parser(name, help=help_text)
        command_parser.add_argument("--file", required=True, help="Event JSON file.")
        command_parser.add_argument("--database", help="SQLite database file.")
    event_list = event_commands.add_parser("list", help="List current events.")
    event_list.add_argument("--database", help="SQLite database file.")
    event_show = event_commands.add_parser("show", help="Show one current event.")
    event_show.add_argument("event_id")
    event_show.add_argument("--revision", type=int)
    event_show.add_argument("--database", help="SQLite database file.")
    event_match = event_commands.add_parser(
        "match", help="Propose a source-document match; proposal remains pending."
    )
    event_match.add_argument("event_id")
    event_match.add_argument("document_id")
    event_match.add_argument("--proposed-by", required=True)
    event_match.add_argument("--rationale", required=True)
    event_match.add_argument("--proposed-at")
    event_match.add_argument("--database", help="SQLite database file.")
    event_matches = event_commands.add_parser(
        "matches", help="List candidate event/source matches."
    )
    event_matches.add_argument("--event-id")
    event_matches.add_argument("--document-id")
    event_matches.add_argument(
        "--status",
        choices=("PENDING", "LINKED", "REJECTED", "UNRESOLVED"),
    )
    event_matches.add_argument("--database", help="SQLite database file.")
    event_review = event_commands.add_parser(
        "review-match", help="Review a candidate event/source match."
    )
    event_review.add_argument("match_id")
    event_review.add_argument(
        "--decision", required=True, choices=("LINKED", "REJECTED", "UNRESOLVED")
    )
    event_review.add_argument("--reviewer", required=True)
    event_review.add_argument("--rationale", required=True)
    event_review.add_argument("--reviewed-at")
    event_review.add_argument("--database", help="SQLite database file.")
    event_match_history = event_commands.add_parser(
        "match-history", help="Show the proposal and every review for a match."
    )
    event_match_history.add_argument("match_id")
    event_match_history.add_argument("--database", help="SQLite database file.")
    event_timeline = event_commands.add_parser(
        "timeline", help="Show one event's chronological, source-linked timeline."
    )
    event_timeline.add_argument("event_id")
    event_timeline.add_argument("--database", help="SQLite database file.")
    timeline_edit = event_commands.add_parser(
        "timeline-edit", help="Append an auditable event-time correction."
    )
    timeline_edit.add_argument("entry_id")
    event_time = timeline_edit.add_mutually_exclusive_group(required=True)
    event_time.add_argument("--event-time")
    event_time.add_argument(
        "--clear-event-time", action="store_true",
        help="Mark the event occurrence time as unknown.",
    )
    timeline_edit.add_argument("--analyst", required=True)
    timeline_edit.add_argument("--rationale", required=True)
    timeline_edit.add_argument("--updated-at")
    timeline_edit.add_argument("--database", help="SQLite database file.")
    timeline_history = event_commands.add_parser(
        "timeline-history", help="Show all revisions of a timeline entry."
    )
    timeline_history.add_argument("entry_id")
    timeline_history.add_argument("--database", help="SQLite database file.")

    claim_parser = commands.add_parser(
        "claim", help="Review source-grounded derived claim extractions."
    )
    claim_commands = claim_parser.add_subparsers(
        dest="claim_command", required=True
    )
    claim_record = claim_commands.add_parser(
        "record", help="Store or revise a canonical claim record from JSON."
    )
    claim_record.add_argument("--file", required=True, help="Claim JSON file.")
    claim_record.add_argument("--database", help="SQLite database file.")
    claim_extract = claim_commands.add_parser(
        "extract",
        help="Validate and store analyst/tool-supplied candidate claims from JSON.",
    )
    claim_extract.add_argument("event_id")
    claim_extract.add_argument("document_id")
    claim_extract.add_argument("--file", required=True)
    claim_extract.add_argument("--method", required=True)
    claim_extract.add_argument("--extractor", required=True)
    claim_extract.add_argument("--created-at")
    claim_extract.add_argument("--database", help="SQLite database file.")
    claim_list = claim_commands.add_parser(
        "list", help="List derived claim candidates and review states."
    )
    claim_list.add_argument("--event-id")
    claim_list.add_argument("--document-id")
    claim_list.add_argument(
        "--status",
        choices=("PENDING", "ACCEPTED", "REJECTED", "CORRECTED", "UNRESOLVED"),
    )
    claim_list.add_argument("--database", help="SQLite database file.")
    claim_review = claim_commands.add_parser(
        "review", help="Accept, correct, reject or defer a derived claim candidate."
    )
    claim_review.add_argument("candidate_id")
    claim_review.add_argument(
        "--decision",
        required=True,
        choices=("ACCEPTED", "REJECTED", "CORRECTED", "UNRESOLVED"),
    )
    claim_review.add_argument("--reviewer", required=True)
    claim_review.add_argument("--rationale", required=True)
    claim_review.add_argument("--correction-file")
    claim_review.add_argument("--reviewed-at")
    claim_review.add_argument("--database", help="SQLite database file.")
    claim_history = claim_commands.add_parser(
        "history", help="Show a claim candidate and its append-only review history."
    )
    claim_history.add_argument("candidate_id")
    claim_history.add_argument("--database", help="SQLite database file.")

    matrix_parser = commands.add_parser(
        "matrix", help="Plan, review and inspect event-specific country coverage."
    )
    matrix_commands = matrix_parser.add_subparsers(
        dest="matrix_command", required=True
    )
    matrix_plan = matrix_commands.add_parser(
        "plan", help="Propose event coverage from the approved country/language catalog."
    )
    matrix_plan.add_argument("event_id")
    matrix_plan.add_argument("--file", required=True, help="Coverage plan JSON file.")
    matrix_plan.add_argument("--proposed-by", required=True)
    matrix_plan.add_argument("--rationale", required=True)
    matrix_plan.add_argument("--proposed-at")
    matrix_plan.add_argument("--config-dir", help="Configuration directory.")
    matrix_plan.add_argument("--database", help="SQLite database file.")
    matrix_review = matrix_commands.add_parser(
        "review", help="Review a proposed event country coverage plan."
    )
    matrix_review.add_argument("plan_id")
    matrix_review.add_argument(
        "--decision",
        required=True,
        choices=("ACCEPTED", "REJECTED", "UNRESOLVED", "SUPERSEDED"),
    )
    matrix_review.add_argument("--reviewer", required=True)
    matrix_review.add_argument("--rationale", required=True)
    matrix_review.add_argument("--reviewed-at")
    matrix_review.add_argument("--database", help="SQLite database file.")
    matrix_plans = matrix_commands.add_parser(
        "plans", help="List proposed coverage plans and their review states."
    )
    matrix_plans.add_argument("--event-id", required=True)
    matrix_plans.add_argument("--database", help="SQLite database file.")
    matrix_assess = matrix_commands.add_parser(
        "assess", help="Record a country-level frame, attribution and confidence assessment."
    )
    matrix_assess.add_argument("plan_id")
    matrix_assess.add_argument("country_code")
    matrix_assess.add_argument("--file", required=True, help="Assessment JSON file.")
    matrix_assess.add_argument("--analyst", required=True)
    matrix_assess.add_argument("--rationale", required=True)
    matrix_assess.add_argument("--created-at")
    matrix_assess.add_argument("--config-dir", help="Configuration directory.")
    matrix_assess.add_argument("--database", help="SQLite database file.")
    matrix_review_assessment = matrix_commands.add_parser(
        "review-assessment", help="Review or supersede a country matrix assessment."
    )
    matrix_review_assessment.add_argument("assessment_id")
    matrix_review_assessment.add_argument(
        "--decision", required=True, choices=("ACCEPTED", "REJECTED", "UNRESOLVED")
    )
    matrix_review_assessment.add_argument("--reviewer", required=True)
    matrix_review_assessment.add_argument("--rationale", required=True)
    matrix_review_assessment.add_argument("--reviewed-at")
    matrix_review_assessment.add_argument("--database", help="SQLite database file.")
    matrix_assessments = matrix_commands.add_parser(
        "assessments", help="List country assessments and their review states."
    )
    matrix_assessments.add_argument("--plan-id", required=True)
    matrix_assessments.add_argument("--database", help="SQLite database file.")
    matrix_show = matrix_commands.add_parser(
        "show", help="Show coverage counts, gaps and reviewed assessments for an event."
    )
    matrix_show.add_argument("event_id")
    matrix_show.add_argument("--config-dir", help="Configuration directory.")
    matrix_show.add_argument("--database", help="SQLite database file.")

    evidence_parser = commands.add_parser(
        "evidence", help="Record evidence, review verification and inspect claim relations."
    )
    evidence_commands = evidence_parser.add_subparsers(
        dest="evidence_command", required=True
    )
    evidence_create = evidence_commands.add_parser(
        "create", help="Record a validated evidence JSON file."
    )
    evidence_create.add_argument("--file", required=True)
    evidence_create.add_argument("--database", help="SQLite database file.")
    evidence_list = evidence_commands.add_parser(
        "list", help="List evidence records and their source/event-time context."
    )
    evidence_list.add_argument("--event-id")
    evidence_list.add_argument("--source-id")
    evidence_list.add_argument("--database", help="SQLite database file.")
    evidence_review = evidence_commands.add_parser(
        "review", help="Append a verification decision and analyst rationale."
    )
    evidence_review.add_argument("evidence_id")
    evidence_review.add_argument(
        "--status", required=True, choices=("IN_REVIEW", "VERIFIED", "REJECTED")
    )
    evidence_review.add_argument("--reviewer", required=True)
    evidence_review.add_argument("--rationale", required=True)
    evidence_review.add_argument("--note")
    evidence_review.add_argument("--reviewed-at")
    evidence_review.add_argument("--database", help="SQLite database file.")
    evidence_note = evidence_commands.add_parser(
        "note", help="Add an attributed analyst note with time and rationale."
    )
    evidence_note.add_argument("evidence_id")
    evidence_note.add_argument("--analyst", required=True)
    evidence_note.add_argument("--note", required=True)
    evidence_note.add_argument("--rationale", required=True)
    evidence_note.add_argument("--noted-at")
    evidence_note.add_argument("--database", help="SQLite database file.")
    evidence_notes = evidence_commands.add_parser(
        "notes", help="List attributed analyst notes for evidence."
    )
    evidence_notes.add_argument("evidence_id")
    evidence_notes.add_argument("--database", help="SQLite database file.")
    evidence_history = evidence_commands.add_parser(
        "history", help="Show append-only verification decisions for evidence."
    )
    evidence_history.add_argument("evidence_id")
    evidence_history.add_argument("--database", help="SQLite database file.")
    evidence_link = evidence_commands.add_parser(
        "link", help="Propose a rationale-bearing claim/evidence relationship."
    )
    evidence_link.add_argument("claim_id")
    evidence_link.add_argument("evidence_id")
    evidence_link.add_argument(
        "--relationship",
        required=True,
        choices=("SUPPORTS", "CONTRADICTS", "NOT_DIRECTLY_RELEVANT"),
    )
    evidence_link.add_argument("--proposed-by", required=True)
    evidence_link.add_argument("--rationale", required=True)
    evidence_link.add_argument("--proposed-at")
    evidence_link.add_argument("--database", help="SQLite database file.")
    evidence_review_link = evidence_commands.add_parser(
        "review-link", help="Review a proposed claim/evidence relationship."
    )
    evidence_review_link.add_argument("link_id")
    evidence_review_link.add_argument(
        "--decision", required=True, choices=("LINKED", "REJECTED", "UNRESOLVED")
    )
    evidence_review_link.add_argument("--reviewer", required=True)
    evidence_review_link.add_argument("--rationale", required=True)
    evidence_review_link.add_argument("--reviewed-at")
    evidence_review_link.add_argument("--database", help="SQLite database file.")
    evidence_links = evidence_commands.add_parser(
        "links", help="List proposed and reviewed claim/evidence relationships."
    )
    evidence_links.add_argument("--event-id")
    evidence_links.add_argument("--claim-id")
    evidence_links.add_argument("--evidence-id")
    evidence_links.add_argument(
        "--status", choices=("PENDING", "LINKED", "REJECTED", "UNRESOLVED")
    )
    evidence_links.add_argument("--database", help="SQLite database file.")
    evidence_link_history = evidence_commands.add_parser(
        "link-history", help="Show proposal and append-only reviews for a relationship."
    )
    evidence_link_history.add_argument("link_id")
    evidence_link_history.add_argument("--database", help="SQLite database file.")
    evidence_gap = evidence_commands.add_parser(
        "gap", help="Record a missing, insufficient or conflicting evidence gap."
    )
    evidence_gap.add_argument("event_id")
    evidence_gap.add_argument(
        "--type", dest="gap_type", required=True,
        choices=("MISSING", "INSUFFICIENT", "CONFLICTING"),
    )
    evidence_gap.add_argument("--description", required=True)
    evidence_gap.add_argument("--analyst", required=True)
    evidence_gap.add_argument("--rationale", required=True)
    evidence_gap.add_argument("--claim-id")
    evidence_gap.add_argument("--supporting-link")
    evidence_gap.add_argument("--contradicting-link")
    evidence_gap.add_argument("--created-at")
    evidence_gap.add_argument("--database", help="SQLite database file.")
    evidence_gaps = evidence_commands.add_parser(
        "gaps", help="List explicit evidence gaps and their current status."
    )
    evidence_gaps.add_argument("--event-id")
    evidence_gaps.add_argument("--claim-id")
    evidence_gaps.add_argument(
        "--type", dest="gap_type",
        choices=("MISSING", "INSUFFICIENT", "CONFLICTING"),
    )
    evidence_gaps.add_argument("--status", choices=("OPEN", "RESOLVED", "DISMISSED"))
    evidence_gaps.add_argument("--database", help="SQLite database file.")
    evidence_review_gap = evidence_commands.add_parser(
        "review-gap", help="Resolve, dismiss or reopen an evidence gap."
    )
    evidence_review_gap.add_argument("gap_id")
    evidence_review_gap.add_argument(
        "--decision", required=True, choices=("RESOLVED", "DISMISSED", "REOPENED")
    )
    evidence_review_gap.add_argument("--reviewer", required=True)
    evidence_review_gap.add_argument("--rationale", required=True)
    evidence_review_gap.add_argument("--reviewed-at")
    evidence_review_gap.add_argument("--database", help="SQLite database file.")
    evidence_gap_history = evidence_commands.add_parser(
        "gap-history", help="Show an evidence gap and its append-only review history."
    )
    evidence_gap_history.add_argument("gap_id")
    evidence_gap_history.add_argument("--database", help="SQLite database file.")

    provenance_parser = commands.add_parser(
        "provenance", help="Trace source dependencies and review evidence independence."
    )
    provenance_commands = provenance_parser.add_subparsers(
        dest="provenance_command", required=True
    )
    provenance_link = provenance_commands.add_parser(
        "link", help="Propose a directed document-to-origin provenance link."
    )
    provenance_link.add_argument("document_id")
    provenance_link.add_argument("upstream_document_id")
    provenance_link.add_argument(
        "--type",
        required=True,
        choices=("CITES", "QUOTES", "SYNDICATED", "DERIVED_FROM"),
    )
    provenance_link.add_argument("--proposed-by", required=True)
    provenance_link.add_argument("--rationale", required=True)
    provenance_link.add_argument("--proposed-at")
    provenance_link.add_argument("--database", help="SQLite database file.")
    provenance_review_link = provenance_commands.add_parser(
        "review-link", help="Confirm, reject or defer a provenance link."
    )
    provenance_review_link.add_argument("link_id")
    provenance_review_link.add_argument(
        "--decision", required=True, choices=("CONFIRMED", "REJECTED", "UNRESOLVED")
    )
    provenance_review_link.add_argument("--reviewer", required=True)
    provenance_review_link.add_argument("--rationale", required=True)
    provenance_review_link.add_argument("--reviewed-at")
    provenance_review_link.add_argument("--database", help="SQLite database file.")
    provenance_links = provenance_commands.add_parser(
        "links", help="List provenance links and their review states."
    )
    provenance_links.add_argument("--document-id")
    provenance_links.add_argument(
        "--type", choices=("CITES", "QUOTES", "SYNDICATED", "DERIVED_FROM")
    )
    provenance_links.add_argument(
        "--status", choices=("PENDING", "CONFIRMED", "REJECTED", "UNRESOLVED")
    )
    provenance_links.add_argument("--database", help="SQLite database file.")
    provenance_graph = provenance_commands.add_parser(
        "graph", help="Trace a document through confirmed upstream source links."
    )
    provenance_graph.add_argument("document_id")
    provenance_graph.add_argument("--database", help="SQLite database file.")
    provenance_claim = provenance_commands.add_parser(
        "claim", help="Trace an extraction candidate through its source document's graph."
    )
    provenance_claim.add_argument("candidate_id")
    provenance_claim.add_argument("--database", help="SQLite database file.")
    provenance_origin = provenance_commands.add_parser(
        "origin", help="Record the earliest identifiable origin or its uncertainty."
    )
    provenance_origin.add_argument("document_id")
    provenance_origin.add_argument(
        "--status", required=True, choices=("IDENTIFIED", "UNCERTAIN", "UNKNOWN")
    )
    provenance_origin.add_argument("--earliest-origin-document-id")
    provenance_origin.add_argument(
        "--supporting-link", action="append", default=[], metavar="LINK-ID"
    )
    provenance_origin.add_argument("--analyst", required=True)
    provenance_origin.add_argument("--rationale", required=True)
    provenance_origin.add_argument("--assessed-at")
    provenance_origin.add_argument("--database", help="SQLite database file.")
    provenance_origin_history = provenance_commands.add_parser(
        "origin-history", help="Show all origin assessments for a document."
    )
    provenance_origin_history.add_argument("document_id")
    provenance_origin_history.add_argument("--database", help="SQLite database file.")
    provenance_assign = provenance_commands.add_parser(
        "assign",
        help="Propose a source, document or evidence independence-group membership.",
    )
    provenance_assign.add_argument(
        "--member-type", required=True, choices=("DOCUMENT", "SOURCE", "EVIDENCE")
    )
    provenance_assign.add_argument("--member-id", required=True)
    provenance_assign.add_argument("--group-id", required=True)
    provenance_assign.add_argument("--proposed-by", required=True)
    provenance_assign.add_argument("--rationale", required=True)
    provenance_assign.add_argument("--proposed-at")
    provenance_assign.add_argument("--database", help="SQLite database file.")
    provenance_review_assignment = provenance_commands.add_parser(
        "review-assignment", help="Review an independence-group assignment."
    )
    provenance_review_assignment.add_argument("assignment_id")
    provenance_review_assignment.add_argument(
        "--decision", required=True, choices=("ACCEPTED", "REJECTED", "UNRESOLVED")
    )
    provenance_review_assignment.add_argument("--reviewer", required=True)
    provenance_review_assignment.add_argument("--rationale", required=True)
    provenance_review_assignment.add_argument(
        "--supporting-link", action="append", default=[], metavar="LINK-ID"
    )
    provenance_review_assignment.add_argument("--reviewed-at")
    provenance_review_assignment.add_argument("--database", help="SQLite database file.")
    provenance_assignments = provenance_commands.add_parser(
        "assignments", help="List reviewed or pending independence-group assignments."
    )
    provenance_assignments.add_argument("--group-id")
    provenance_assignments.add_argument(
        "--member-type", choices=("DOCUMENT", "SOURCE", "EVIDENCE")
    )
    provenance_assignments.add_argument("--member-id")
    provenance_assignments.add_argument(
        "--status", choices=("PENDING", "ACCEPTED", "REJECTED", "UNRESOLVED")
    )
    provenance_assignments.add_argument("--database", help="SQLite database file.")
    provenance_assignment_history = provenance_commands.add_parser(
        "assignment-history", help="Show an assignment and its append-only reviews."
    )
    provenance_assignment_history.add_argument("assignment_id")
    provenance_assignment_history.add_argument("--database", help="SQLite database file.")
    return parser


def _read_json_object(path: str, description: str) -> dict[str, JsonValue]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{description} must be a JSON object.")
    return payload


def _matrix_coverage_payload(
    path: str, catalog: CountryCoverageCatalog
) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    payload = _read_json_object(path, "Coverage plan input")
    raw_countries = payload.get("countries")
    raw_outside = payload.get("outside_catalog", [])
    if not isinstance(raw_countries, list) or not raw_countries:
        raise ValueError("Coverage plan countries must be a non-empty JSON array.")
    if not isinstance(raw_outside, list):
        raise ValueError("Coverage plan outside_catalog must be a JSON array.")
    catalog_entries = {entry.code: entry for entry in catalog.entries}
    selected: dict[str, list[str]] = {}
    for item in raw_countries:
        if not isinstance(item, dict):
            raise ValueError("Each selected country must be a JSON object.")
        code = item.get("code")
        languages = item.get("languages")
        if not isinstance(code, str) or code not in catalog_entries:
            raise ValueError(f"Unknown country code in coverage plan: {code!r}.")
        if code in selected:
            raise ValueError(f"Country {code} is selected more than once.")
        if languages is None:
            languages = list(catalog_entries[code].languages)
        if not isinstance(languages, list) or not languages:
            raise ValueError(f"Country {code} needs a non-empty unique language list.")
        language_codes = [
            language for language in languages if isinstance(language, str)
        ]
        if (
            len(language_codes) != len(languages)
            or len(language_codes) != len(set(language_codes))
        ):
            raise ValueError(f"Country {code} needs a non-empty unique language list.")
        allowed = set(catalog_entries[code].languages)
        if any(language not in allowed for language in language_codes):
            raise ValueError(
                f"Selected languages for {code} must be a subset of its approved languages."
            )
        selected[code] = language_codes
    countries: list[dict[str, JsonValue]] = []
    for entry in catalog.entries:
        if entry.code not in selected:
            continue
        languages_payload: list[JsonValue] = []
        country_payload: dict[str, JsonValue] = {
            "code": entry.code,
            "name": entry.name,
        }
        for language in selected[entry.code]:
            languages_payload.append(language)
        country_payload["languages"] = languages_payload
        countries.append(country_payload)
    outside: list[dict[str, JsonValue]] = []
    outside_names: set[str] = set()
    for item in raw_outside:
        if not isinstance(item, dict):
            raise ValueError("Each out-of-catalog country must be a JSON object.")
        country, reason = item.get("country"), item.get("reason")
        if not isinstance(country, str) or not isinstance(reason, str):
            raise ValueError(
                "Each out-of-catalog entry needs non-empty country and reason strings."
            )
        country_name, country_reason = country.strip(), reason.strip()
        if not country_name or not country_reason:
            raise ValueError(
                "Each out-of-catalog entry needs non-empty country and reason strings."
            )
        if country_name in catalog_entries:
            raise ValueError(
                f"{country_name} is in the approved catalog; select it under countries."
            )
        if country_name in outside_names:
            raise ValueError(f"Out-of-catalog country {country_name} is duplicated.")
        outside_names.add(country_name)
        outside.append({"country": country_name, "reason": country_reason})
    return countries, outside


def _matrix_assessment_payload(path: str) -> dict[str, JsonValue]:
    payload = _read_json_object(path, "Matrix assessment input")
    required_text = (
        "dominant_frame",
        "attribution_summary",
        "occurrence_confidence",
        "method_confidence",
        "attribution_confidence",
        "omissions",
        "contradictions",
    )
    for key in required_text:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Matrix assessment field {key!r} must be a non-empty string.")
    for key in (
        "supporting_source_ids",
        "supporting_claim_ids",
        "supporting_evidence_ids",
    ):
        values = payload.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(f"Matrix assessment field {key!r} must be a string array.")
        payload[key] = [
            value for value in values if isinstance(value, str)
        ]
    return payload


def _matrix_string_list(payload: dict[str, JsonValue], key: str) -> list[str]:
    values = payload.get(key, [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) for value in values
    ):
        raise ValueError(f"Matrix assessment field {key!r} must be a string array.")
    return [value for value in values if isinstance(value, str)]


def _print_country_coverage_plan(plan: CountryCoveragePlan) -> None:
    print(
        f"{plan.plan_id} event={plan.event_id} revision={plan.revision} "
        f"status={plan.status} config_sha256={plan.config_sha256}"
    )
    print(f"  countries={json.dumps(plan.countries, ensure_ascii=False)}")
    print(f"  outside_catalog={json.dumps(plan.outside_catalog, ensure_ascii=False)}")
    print(f"  proposed_by={plan.proposed_by} proposed_at={plan.proposed_at}")
    print(f"  rationale={plan.rationale}")
    if plan.reviewed_by is not None:
        print(
            f"  reviewed_by={plan.reviewed_by} reviewed_at={plan.reviewed_at} "
            f"review_rationale={plan.review_rationale}"
        )


def _print_matrix_assessment(assessment: CountryMatrixAssessment) -> None:
    print(
        f"{assessment.assessment_id} event={assessment.event_id} "
        f"country={assessment.country_code} status={assessment.status}"
    )
    print(
        f"  frame={assessment.dominant_frame} "
        f"attribution={assessment.attribution_summary}"
    )
    print(
        f"  confidence=occurrence:{assessment.occurrence_confidence},"
        f"method:{assessment.method_confidence},"
        f"attribution:{assessment.attribution_confidence}"
    )
    print(
        f"  omissions={assessment.omissions} contradictions={assessment.contradictions}"
    )
    print(
        "  supporting_sources="
        + ",".join(assessment.supporting_source_ids)
        + " claims="
        + ",".join(assessment.supporting_claim_ids)
        + " evidence="
        + ",".join(assessment.supporting_evidence_ids)
    )
    print(
        f"  analyst={assessment.analyst} created_at={assessment.created_at} "
        f"rationale={assessment.rationale}"
    )
    if assessment.reviewed_by is not None:
        print(
            f"  reviewed_by={assessment.reviewed_by} reviewed_at={assessment.reviewed_at} "
            f"review_rationale={assessment.review_rationale}"
        )


def _print_national_information_matrix(matrix: NationalInformationMatrix) -> None:
    _print_country_coverage_plan(matrix.plan)
    if not matrix.config_matches:
        print(
            "WARNING: country/language configuration differs from the accepted "
            "plan snapshot; re-propose and review coverage."
        )
    if not matrix.cells:
        print("No country cells: the latest coverage plan is not accepted.")
        return
    for cell in matrix.cells:
        print(
            f"{cell.country_code} {cell.country_name} languages={','.join(cell.languages)} "
            f"documents={cell.reporting_documents} sources={cell.reporting_sources} "
            f"institutional_sources={cell.institutional_sources} "
            f"primary_observation_sources={cell.primary_observation_sources} "
            f"primary_evidence={cell.primary_evidence} claims={cell.claims} "
            f"supports={cell.supporting_relations} "
            f"contradictions={cell.contradicting_relations}"
        )
        print(
            f"  uncovered_languages={','.join(cell.uncovered_languages) or 'none'} "
            f"independence_groups={','.join(cell.accepted_independence_groups) or 'none'}"
        )
        for gap in cell.coverage_gaps:
            print(f"  coverage_gap={gap}")
        if cell.assessment is None:
            print("  assessment=none")
        else:
            _print_matrix_assessment(cell.assessment)


def _print_collection_result(result: CollectionResult) -> None:
    print(
        f"{result.collection_id} {result.source_id} attempt {result.attempt_number}: "
        f"{result.status}"
    )
    print(
        f"  started_at={result.started_at} completed_at={result.completed_at} "
        f"retry_of={result.retry_of}"
    )
    print(
        f"  requested_url={result.source_url} final_url={result.final_url} "
        f"HTTP={result.http_status} type={result.content_type}"
    )
    print(
        f"  sha256={result.content_sha256} archived={result.archived_content_ref}"
    )
    if result.source_metadata:
        review = result.source_metadata.get("review")
        constraints = result.source_metadata.get("constraints")
        reviewer = review.get("reviewer") if isinstance(review, dict) else None
        archive = (
            constraints.get("archive_content")
            if isinstance(constraints, dict)
            else None
        )
        print(
            f"  publisher={result.source_metadata.get('publisher')} "
            f"class={result.source_metadata.get('source_class')} "
            f"country={result.source_metadata.get('country')} "
            f"language={result.source_metadata.get('language')} "
            f"method={result.source_metadata.get('access_method')} "
            f"reviewer={reviewer} archive_permitted={archive}"
        )
    for origin_id in result.origin_identifiers[:10]:
        print(f"  origin={origin_id}")
    if len(result.origin_identifiers) > 10:
        print(f"  origin=... and {len(result.origin_identifiers) - 10} more")
    if result.error_code:
        print(f"  error={result.error_code}: {result.error_message}")


def _print_document(document: NormalizedDocument) -> None:
    print(
        f"{document.document_id} source={document.source_id} "
        f"collection={document.collection_id} item={document.item_index}"
    )
    print(
        f"  origin={document.origin_identifier} "
        f"language={document.language} via={document.language_source} "
        f"review={document.language_review_status}"
    )
    if document.declared_language and document.registry_language:
        print(
            f"  declared_language={document.declared_language} "
            f"registry_language={document.registry_language}"
        )
    print(
        f"  title_original={document.original_title!r} "
        f"title_normalized={document.normalized_title!r}"
    )
    print(
        f"  url_original={document.original_url} "
        f"url_normalized={document.normalized_url}"
    )
    print(
        f"  publisher_original={document.publisher_original!r} "
        f"publisher_normalized={document.publisher_normalized!r}"
    )
    print(
        f"  published_original={document.original_published_at!r} "
        f"published_normalized={document.normalized_published_at!r} "
        f"timezone_known={document.publication_timezone_known}"
    )
    print(f"  text_sha256={document.text_sha256}")
    print(f"  text_original={document.original_text}")
    print(f"  text_normalized={document.normalized_text}")


def _print_duplicate(relationship: DuplicateRelationship) -> None:
    print(
        f"{relationship.relationship_id} {relationship.relationship_type} "
        f"similarity={relationship.similarity:.3f} review={relationship.review_status}"
    )
    print(
        f"  documents={relationship.document_id},{relationship.related_document_id} "
        f"decision={relationship.review_decision} reviewer={relationship.reviewed_by}"
    )
    if relationship.rationale:
        print(f"  rationale={relationship.rationale}")


def _print_translation(translation: TranslationRecord) -> None:
    print(
        f"{translation.translation_id} document={translation.document_id} "
        f"{translation.source_language}->{translation.target_language} "
        f"method={translation.translation_method} translator={translation.translator} "
        f"translated_at={translation.translated_at}"
    )
    print(f"  source_text_sha256={translation.source_text_sha256}")
    print(f"  translated_text={translation.translated_text}")


def _print_event(event: Event, *, revision: int | None = None) -> None:
    version = "current" if revision is None else f"revision={revision}"
    print(
        f"{event.event_id} {event.event_type} [{event.status}] "
        f"{version} title={event.title}"
    )
    print(
        f"  event_time_start={event.start_time} event_time_end={event.end_time} "
        f"location={event.location} country={event.country}"
    )


def _print_event_match(match: EventSourceMatch) -> None:
    print(
        f"{match.match_id} event={match.event_id} document={match.document_id} "
        f"source={match.source_id} status={match.status}"
    )
    print(f"  proposed_by={match.proposed_by} proposed_at={match.proposed_at}")
    print(f"  proposal_rationale={match.rationale}")
    if match.review_decision is not None:
        print(
            f"  decision={match.review_decision} reviewer={match.reviewed_by} "
            f"reviewed_at={match.reviewed_at}"
        )
        print(f"  review_rationale={match.review_rationale}")


def _print_timeline_entry(entry: TimelineEntry) -> None:
    print(
        f"{entry.entry_id} revision={entry.revision} event={entry.event_id} "
        f"document={entry.document_id} source={entry.source_id} "
        f"publisher={entry.publisher} title={entry.source_title}"
    )
    print(
        f"  event_time={entry.event_time} publication_time_original="
        f"{entry.original_publication_time!r} publication_time_normalized="
        f"{entry.normalized_publication_time!r} "
        f"publication_timezone_known={entry.publication_timezone_known}"
    )
    print(
        f"  collection_time={entry.collection_time} source_url={entry.source_url}"
    )
    print(
        f"  event_time_rationale={entry.event_time_rationale} "
        f"updated_by={entry.updated_by} updated_at={entry.updated_at}"
    )


def _print_claim_extraction(candidate: ClaimExtraction) -> None:
    print(
        f"{candidate.candidate_id} event={candidate.event_id} "
        f"document={candidate.document_id} source={candidate.source_id} "
        f"review={candidate.review_status}"
    )
    print(
        f"  subject={candidate.subject!r} predicate={candidate.predicate!r} "
        f"object={candidate.object!r} type={candidate.claim_type} "
        f"modality={candidate.modality} attribution={candidate.attribution!r}"
    )
    print(
        f"  source_text_sha256={candidate.source_text_sha256} "
        f"span=[{candidate.span_start}:{candidate.span_end}] "
        f"quote={candidate.span_text!r}"
    )
    print(
        f"  extraction_method={candidate.extraction_method} "
        f"extractor={candidate.extractor} created_at={candidate.created_at}"
    )
    if candidate.review_decision is not None:
        print(
            f"  decision={candidate.review_decision} reviewer={candidate.reviewed_by} "
            f"reviewed_at={candidate.reviewed_at} "
            f"rationale={candidate.review_rationale!r}"
        )
    if candidate.correction_payload is not None:
        print(f"  latest_correction={candidate.correction_payload}")


def _print_provenance_link(link: ProvenanceLink) -> None:
    print(
        f"{link.link_id} {link.relationship_type} "
        f"{link.document_id} [{link.source_id}] -> "
        f"{link.upstream_document_id} [{link.upstream_source_id}] "
        f"status={link.review_status}"
    )
    print(
        f"  proposed_by={link.proposed_by} proposed_at={link.proposed_at} "
        f"rationale={link.rationale!r}"
    )
    if link.review_status != "PENDING":
        print(
            f"  reviewer={link.reviewed_by} reviewed_at={link.reviewed_at} "
            f"review_rationale={link.review_rationale!r}"
        )


def _print_provenance_origin(origin: ProvenanceOriginAssessment) -> None:
    print(
        f"{origin.assessment_id} document={origin.document_id} "
        f"origin={origin.origin_status} "
        f"earliest_origin={origin.earliest_origin_document_id}"
    )
    print(
        f"  analyst={origin.assessed_by} assessed_at={origin.assessed_at} "
        f"rationale={origin.rationale!r}"
    )
    print(f"  supporting_links={','.join(origin.supporting_link_ids) or 'none'}")


def _print_independence_assignment(assignment: IndependenceAssignment) -> None:
    print(
        f"{assignment.assignment_id} {assignment.member_type}={assignment.member_id} "
        f"group={assignment.group_id} status={assignment.review_status}"
    )
    print(
        f"  proposed_by={assignment.proposed_by} proposed_at={assignment.proposed_at} "
        f"rationale={assignment.rationale!r}"
    )
    if assignment.review_status != "PENDING":
        print(
            f"  reviewer={assignment.reviewed_by} reviewed_at={assignment.reviewed_at} "
            f"review_rationale={assignment.review_rationale!r}"
        )
        print(
            "  supporting_links="
            f"{','.join(assignment.supporting_link_ids) or 'none'}"
        )


def _print_evidence(
    evidence: Evidence, *, verification_status: str | None = None
) -> None:
    status = verification_status or evidence.verification_status
    print(
        f"{evidence.evidence_id} event={evidence.event_id} source={evidence.source_id} "
        f"type={evidence.evidence_type} directness={evidence.directness} "
        f"verification={status}"
    )
    print(
        f"  collection_time={evidence.collection_time} event_time={evidence.event_time} "
        f"independence_group={evidence.independence_group}"
    )
    print(f"  observation={evidence.observation}")
    print(f"  analyst_notes={evidence.analyst_notes}")
    if evidence.reliability_assessment:
        print(f"  reliability_assessment={evidence.reliability_assessment}")


def _print_evidence_review(review: EvidenceReview) -> None:
    print(
        f"{review.review_id} evidence={review.evidence_id} "
        f"verification={review.verification_status} reviewer={review.reviewed_by} "
        f"reviewed_at={review.reviewed_at}"
    )
    print(f"  rationale={review.rationale}")
    if review.analyst_note:
        print(f"  analyst_note={review.analyst_note}")


def _print_evidence_note(note: EvidenceNote) -> None:
    print(
        f"{note.note_id} evidence={note.evidence_id} analyst={note.analyst} "
        f"noted_at={note.noted_at}"
    )
    print(f"  note={note.note}")
    print(f"  rationale={note.rationale}")


def _print_claim_evidence_link(link: ClaimEvidenceLink) -> None:
    print(
        f"{link.link_id} event={link.event_id} claim={link.claim_id} "
        f"evidence={link.evidence_id} relationship={link.relationship} "
        f"status={link.review_status}"
    )
    print(f"  proposed_by={link.proposed_by} proposed_at={link.proposed_at}")
    print(f"  proposal_rationale={link.rationale}")
    if link.reviewed_by is not None:
        print(
            f"  reviewed_by={link.reviewed_by} reviewed_at={link.reviewed_at} "
            f"review_rationale={link.review_rationale}"
        )


def _print_evidence_gap(gap: EvidenceGap) -> None:
    print(
        f"{gap.gap_id} event={gap.event_id} claim={gap.claim_id} "
        f"type={gap.gap_type} status={gap.status}"
    )
    print(
        f"  description={gap.description} created_by={gap.created_by} "
        f"created_at={gap.created_at}"
    )
    print(f"  rationale={gap.rationale}")
    if gap.supporting_link_id or gap.contradicting_link_id:
        print(
            f"  supporting_link={gap.supporting_link_id} "
            f"contradicting_link={gap.contradicting_link_id}"
        )
    if gap.reviewed_by is not None:
        print(
            f"  reviewed_by={gap.reviewed_by} reviewed_at={gap.reviewed_at} "
            f"review_rationale={gap.review_rationale}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "config":
            settings = Settings.from_environment(config_dir_override=args.config_dir)
            summary = validate_configuration(settings.config_dir)
            print(
                "Configuration valid: "
                f"{summary.country_count} countries, "
                f"{summary.language_count} languages, "
                f"{summary.event_type_count} event types, "
                f"{summary.source_type_count} source types."
            )
            return 0

        if args.command == "matrix":
            config_dir = getattr(args, "config_dir", None)
            settings = Settings.from_environment(
                config_dir_override=config_dir,
                database_path_override=args.database,
            )
            database = Database(settings.database_path)
            command = args.matrix_command
            if command == "plan":
                catalog = load_country_coverage_catalog(settings.config_dir)
                countries, outside_catalog = _matrix_coverage_payload(
                    args.file, catalog
                )
                proposed_at = args.proposed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                plan = database.create_country_coverage_plan(
                    event_id=args.event_id,
                    config_sha256=catalog.config_sha256,
                    countries=countries,
                    outside_catalog=outside_catalog,
                    proposed_by=args.proposed_by,
                    proposed_at=proposed_at,
                    rationale=args.rationale,
                )
                _print_country_coverage_plan(plan)
                return 0
            if command == "review":
                reviewed_at = args.reviewed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                plan = database.review_country_coverage_plan(
                    plan_id=args.plan_id,
                    decision=args.decision,
                    reviewed_by=args.reviewer,
                    reviewed_at=reviewed_at,
                    rationale=args.rationale,
                )
                _print_country_coverage_plan(plan)
                return 0
            if command == "plans":
                plans = database.country_coverage_plans(event_id=args.event_id)
                if not plans:
                    print("No country coverage plans found.")
                for plan in plans:
                    _print_country_coverage_plan(plan)
                return 0
            if command == "assess":
                catalog = load_country_coverage_catalog(settings.config_dir)
                payload = _matrix_assessment_payload(args.file)
                created_at = args.created_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                assessment = database.record_country_matrix_assessment(
                    plan_id=args.plan_id,
                    country_code=args.country_code,
                    config_sha256=catalog.config_sha256,
                    dominant_frame=str(payload["dominant_frame"]),
                    attribution_summary=str(payload["attribution_summary"]),
                    occurrence_confidence=str(payload["occurrence_confidence"]),
                    method_confidence=str(payload["method_confidence"]),
                    attribution_confidence=str(payload["attribution_confidence"]),
                    omissions=str(payload["omissions"]),
                    contradictions=str(payload["contradictions"]),
                    supporting_source_ids=_matrix_string_list(
                        payload, "supporting_source_ids"
                    ),
                    supporting_claim_ids=_matrix_string_list(
                        payload, "supporting_claim_ids"
                    ),
                    supporting_evidence_ids=_matrix_string_list(
                        payload, "supporting_evidence_ids"
                    ),
                    analyst=args.analyst,
                    created_at=created_at,
                    rationale=args.rationale,
                )
                _print_matrix_assessment(assessment)
                return 0
            if command == "review-assessment":
                reviewed_at = args.reviewed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                assessment = database.review_country_matrix_assessment(
                    assessment_id=args.assessment_id,
                    decision=args.decision,
                    reviewed_by=args.reviewer,
                    reviewed_at=reviewed_at,
                    rationale=args.rationale,
                )
                _print_matrix_assessment(assessment)
                return 0
            if command == "assessments":
                assessments = database.country_matrix_assessments(
                    plan_id=args.plan_id
                )
                if not assessments:
                    print("No country matrix assessments found.")
                for assessment in assessments:
                    _print_matrix_assessment(assessment)
                return 0
            catalog = load_country_coverage_catalog(settings.config_dir)
            matrix = database.national_information_matrix(
                event_id=args.event_id, config_sha256=catalog.config_sha256
            )
            _print_national_information_matrix(matrix)
            return 0

        if args.command == "event":
            settings = Settings.from_environment(
                database_path_override=args.database
            )
            database = Database(settings.database_path)
            if args.event_command in {"create", "update"}:
                event_payload = json.loads(
                    Path(args.file).expanduser().read_text(encoding="utf-8")
                )
                event = parse_record("event", event_payload)
                if not isinstance(event, Event):
                    raise ValueError("Event JSON did not produce an event record.")
                if args.event_command == "create":
                    revision = database.create_event(event)
                else:
                    revision = database.update_event(event)
                print(f"Saved event {event.event_id} revision {revision}.")
                return 0
            if args.event_command == "list":
                events = database.list_events()
                if not events:
                    print("No events found.")
                for event in events:
                    _print_event(event)
                return 0
            if args.event_command == "show":
                event = database.get(
                    "event", args.event_id, revision=args.revision
                )
                if not isinstance(event, Event):
                    raise ValueError(f"Record {args.event_id} is not an event.")
                _print_event(event, revision=args.revision)
                if args.revision is None:
                    print(
                        "  revisions="
                        + ",".join(
                            str(revision)
                            for revision in database.revisions(
                                "event", args.event_id
                            )
                        )
                    )
                return 0
            if args.event_command == "match":
                proposed_at = args.proposed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                match = database.propose_event_source_match(
                    event_id=args.event_id,
                    document_id=args.document_id,
                    proposed_by=args.proposed_by,
                    proposed_at=proposed_at,
                    rationale=args.rationale,
                )
                _print_event_match(match)
                return 0
            if args.event_command == "match-history":
                for match in database.event_source_match_history(args.match_id):
                    _print_event_match(match)
                return 0
            if args.event_command == "matches":
                matches = database.event_source_matches(
                    event_id=args.event_id,
                    document_id=args.document_id,
                    status=args.status,
                )
                if not matches:
                    print("No candidate event/source matches found.")
                for match in matches:
                    _print_event_match(match)
                return 0
            if args.event_command == "review-match":
                reviewed_at = args.reviewed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                match = database.review_event_source_match(
                    match_id=args.match_id,
                    decision=args.decision,
                    reviewed_by=args.reviewer,
                    reviewed_at=reviewed_at,
                    rationale=args.rationale,
                )
                _print_event_match(match)
                return 0
            if args.event_command == "timeline":
                if not isinstance(database.get("event", args.event_id), Event):
                    raise ValueError(f"Record {args.event_id} is not an event.")
                entries = database.timeline_entries(args.event_id)
                if not entries:
                    print("No confirmed source links in this event timeline.")
                for entry in entries:
                    _print_timeline_entry(entry)
                return 0
            if args.event_command == "timeline-edit":
                updated_at = args.updated_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                entry = database.revise_timeline_entry(
                    entry_id=args.entry_id,
                    event_time=None if args.clear_event_time else args.event_time,
                    updated_by=args.analyst,
                    updated_at=updated_at,
                    rationale=args.rationale,
                )
                _print_timeline_entry(entry)
                return 0
            for entry in database.timeline_entry_history(args.entry_id):
                _print_timeline_entry(entry)
            return 0

        if args.command == "claim":
            settings = Settings.from_environment(
                database_path_override=args.database
            )
            database = Database(settings.database_path)
            if args.claim_command == "record":
                payload = json.loads(
                    Path(args.file).expanduser().read_text(encoding="utf-8")
                )
                if not isinstance(payload, dict):
                    raise ValueError("Claim input must be a JSON object.")
                claim = parse_record("claim", payload)
                if not isinstance(claim, Claim):
                    raise ValueError("Claim input did not produce a Claim record.")
                revision = database.save(claim)
                print(f"Stored claim {claim.claim_id} at revision {revision}.")
                return 0
            if args.claim_command == "extract":
                payload = json.loads(
                    Path(args.file).expanduser().read_text(encoding="utf-8")
                )
                if not isinstance(payload, list) or any(
                    not isinstance(candidate, dict) for candidate in payload
                ):
                    raise ValueError(
                        "Claim extraction input must be a JSON array of candidate objects."
                    )
                created_at = args.created_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                candidates = database.create_claim_extractions(
                    event_id=args.event_id,
                    document_id=args.document_id,
                    candidates=payload,
                    extraction_method=args.method,
                    extractor=args.extractor,
                    created_at=created_at,
                )
                print(f"Stored {len(candidates)} derived claim candidate(s).")
                for candidate in candidates:
                    _print_claim_extraction(candidate)
                return 0
            if args.claim_command == "list":
                candidates = database.claim_extractions(
                    event_id=args.event_id,
                    document_id=args.document_id,
                    review_status=args.status,
                )
                if not candidates:
                    print("No claim extraction candidates found.")
                for candidate in candidates:
                    _print_claim_extraction(candidate)
                return 0
            if args.claim_command == "review":
                correction = None
                if args.correction_file:
                    correction = json.loads(
                        Path(args.correction_file)
                        .expanduser()
                        .read_text(encoding="utf-8")
                    )
                    if not isinstance(correction, dict):
                        raise ValueError("Correction input must be a JSON object.")
                reviewed_at = args.reviewed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                candidate = database.review_claim_extraction(
                    candidate_id=args.candidate_id,
                    decision=args.decision,
                    reviewed_by=args.reviewer,
                    reviewed_at=reviewed_at,
                    rationale=args.rationale,
                    correction=correction,
                )
                _print_claim_extraction(candidate)
                return 0
            history = database.claim_extraction_history(args.candidate_id)
            for candidate in history:
                _print_claim_extraction(candidate)
            return 0

        if args.command == "evidence":
            settings = Settings.from_environment(
                database_path_override=args.database
            )
            database = Database(settings.database_path)
            command = args.evidence_command
            if command == "create":
                payload = json.loads(
                    Path(args.file).expanduser().read_text(encoding="utf-8")
                )
                if not isinstance(payload, dict):
                    raise ValueError("Evidence input must be a JSON object.")
                evidence = parse_record("evidence", payload)
                if not isinstance(evidence, Evidence):
                    raise ValueError("Evidence input did not produce an Evidence record.")
                database.record_evidence(evidence)
                _print_evidence(evidence)
                return 0
            if command == "list":
                records = database.evidence_records(
                    event_id=args.event_id, source_id=args.source_id
                )
                if not records:
                    print("No evidence records found.")
                for evidence in records:
                    _print_evidence(
                        evidence,
                        verification_status=database.evidence_verification_status(
                            evidence.evidence_id
                        ),
                    )
                return 0
            if command == "review":
                reviewed_at = args.reviewed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                review = database.review_evidence(
                    evidence_id=args.evidence_id,
                    verification_status=args.status,
                    reviewed_by=args.reviewer,
                    reviewed_at=reviewed_at,
                    rationale=args.rationale,
                    analyst_note=args.note,
                )
                _print_evidence_review(review)
                return 0
            if command == "note":
                noted_at = args.noted_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                note = database.add_evidence_note(
                    evidence_id=args.evidence_id,
                    analyst=args.analyst,
                    noted_at=noted_at,
                    note=args.note,
                    rationale=args.rationale,
                )
                _print_evidence_note(note)
                return 0
            if command == "notes":
                notes = database.evidence_notes(args.evidence_id)
                if not notes:
                    print("No attributed analyst notes found.")
                for note in notes:
                    _print_evidence_note(note)
                return 0
            if command == "history":
                history = database.evidence_review_history(args.evidence_id)
                if not history:
                    evidence = database.get("evidence", args.evidence_id)
                    if isinstance(evidence, Evidence):
                        print(
                            f"{evidence.evidence_id} initial verification="
                            f"{evidence.verification_status}; no analyst reviews."
                        )
                for review in history:
                    _print_evidence_review(review)
                return 0
            if command == "link":
                proposed_at = args.proposed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                link = database.propose_claim_evidence_link(
                    claim_id=args.claim_id,
                    evidence_id=args.evidence_id,
                    relationship=args.relationship,
                    proposed_by=args.proposed_by,
                    proposed_at=proposed_at,
                    rationale=args.rationale,
                )
                _print_claim_evidence_link(link)
                return 0
            if command == "review-link":
                reviewed_at = args.reviewed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                link = database.review_claim_evidence_link(
                    link_id=args.link_id,
                    decision=args.decision,
                    reviewed_by=args.reviewer,
                    reviewed_at=reviewed_at,
                    rationale=args.rationale,
                )
                _print_claim_evidence_link(link)
                return 0
            if command == "links":
                links = database.claim_evidence_links(
                    event_id=args.event_id,
                    claim_id=args.claim_id,
                    evidence_id=args.evidence_id,
                    review_status=args.status,
                )
                if not links:
                    print("No claim/evidence relationships found.")
                for link in links:
                    _print_claim_evidence_link(link)
                return 0
            if command == "link-history":
                for link in database.claim_evidence_link_history(args.link_id):
                    _print_claim_evidence_link(link)
                return 0
            if command == "gap":
                created_at = args.created_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                gap = database.record_evidence_gap(
                    event_id=args.event_id,
                    claim_id=args.claim_id,
                    gap_type=args.gap_type,
                    description=args.description,
                    created_by=args.analyst,
                    created_at=created_at,
                    rationale=args.rationale,
                    supporting_link_id=args.supporting_link,
                    contradicting_link_id=args.contradicting_link,
                )
                _print_evidence_gap(gap)
                return 0
            if command == "gaps":
                gaps = database.evidence_gaps(
                    event_id=args.event_id,
                    claim_id=args.claim_id,
                    gap_type=args.gap_type,
                    status=args.status,
                )
                if not gaps:
                    print("No evidence gaps found.")
                for gap in gaps:
                    _print_evidence_gap(gap)
                return 0
            if command == "review-gap":
                reviewed_at = args.reviewed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                gap = database.review_evidence_gap(
                    gap_id=args.gap_id,
                    decision=args.decision,
                    reviewed_by=args.reviewer,
                    reviewed_at=reviewed_at,
                    rationale=args.rationale,
                )
                _print_evidence_gap(gap)
                return 0
            for gap in database.evidence_gap_history(args.gap_id):
                _print_evidence_gap(gap)
            return 0

        if args.command == "provenance":
            settings = Settings.from_environment(
                database_path_override=args.database
            )
            database = Database(settings.database_path)
            command = args.provenance_command
            if command == "link":
                proposed_at = args.proposed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                link = database.propose_provenance_link(
                    document_id=args.document_id,
                    upstream_document_id=args.upstream_document_id,
                    relationship_type=args.type,
                    proposed_by=args.proposed_by,
                    proposed_at=proposed_at,
                    rationale=args.rationale,
                )
                _print_provenance_link(link)
                return 0
            if command == "review-link":
                reviewed_at = args.reviewed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                link = database.review_provenance_link(
                    link_id=args.link_id,
                    decision=args.decision,
                    reviewed_by=args.reviewer,
                    reviewed_at=reviewed_at,
                    rationale=args.rationale,
                )
                _print_provenance_link(link)
                return 0
            if command == "links":
                links = database.provenance_links(
                    document_id=args.document_id,
                    relationship_type=args.type,
                    review_status=args.status,
                )
                if not links:
                    print("No provenance links found.")
                for link in links:
                    _print_provenance_link(link)
                return 0
            if command == "graph":
                links = database.provenance_graph(args.document_id)
                print(f"Confirmed upstream provenance graph for {args.document_id}:")
                if not links:
                    print("  No confirmed upstream provenance links.")
                for link in links:
                    _print_provenance_link(link)
                origin = database.latest_provenance_origin(args.document_id)
                if origin is None:
                    print("Origin assessment: not yet recorded.")
                else:
                    print("Latest origin assessment:")
                    _print_provenance_origin(origin)
                return 0
            if command == "claim":
                history = database.claim_extraction_history(args.candidate_id)
                candidate = history[-1]
                links = database.provenance_graph(candidate.document_id)
                print(
                    f"Confirmed provenance graph for claim candidate "
                    f"{candidate.candidate_id} (document {candidate.document_id}):"
                )
                if not links:
                    print("  No confirmed upstream provenance links.")
                for link in links:
                    _print_provenance_link(link)
                origin = database.latest_provenance_origin(candidate.document_id)
                if origin is None:
                    print("Origin assessment: not yet recorded.")
                else:
                    print("Latest origin assessment:")
                    _print_provenance_origin(origin)
                return 0
            if command == "origin":
                assessed_at = args.assessed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                origin = database.record_provenance_origin(
                    document_id=args.document_id,
                    origin_status=args.status,
                    earliest_origin_document_id=args.earliest_origin_document_id,
                    assessed_by=args.analyst,
                    assessed_at=assessed_at,
                    rationale=args.rationale,
                    supporting_link_ids=args.supporting_link,
                )
                _print_provenance_origin(origin)
                return 0
            if command == "origin-history":
                history = database.provenance_origin_history(args.document_id)
                if not history:
                    print("No origin assessments recorded.")
                for origin in history:
                    _print_provenance_origin(origin)
                return 0
            if command == "assign":
                proposed_at = args.proposed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                assignment = database.propose_independence_assignment(
                    group_id=args.group_id,
                    member_type=args.member_type,
                    member_id=args.member_id,
                    proposed_by=args.proposed_by,
                    proposed_at=proposed_at,
                    rationale=args.rationale,
                )
                _print_independence_assignment(assignment)
                return 0
            if command == "review-assignment":
                reviewed_at = args.reviewed_at or (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                assignment = database.review_independence_assignment(
                    assignment_id=args.assignment_id,
                    decision=args.decision,
                    reviewed_by=args.reviewer,
                    reviewed_at=reviewed_at,
                    rationale=args.rationale,
                    supporting_link_ids=args.supporting_link,
                )
                _print_independence_assignment(assignment)
                return 0
            if command == "assignments":
                assignments = database.independence_assignments(
                    group_id=args.group_id,
                    member_type=args.member_type,
                    member_id=args.member_id,
                    review_status=args.status,
                )
                if not assignments:
                    print("No independence assignments found.")
                for assignment in assignments:
                    _print_independence_assignment(assignment)
                return 0
            for assignment in database.independence_assignment_history(
                args.assignment_id
            ):
                _print_independence_assignment(assignment)
            return 0

        if args.command == "source":
            if args.source_command == "list":
                settings = Settings.from_environment(config_dir_override=args.config_dir)
                validate_configuration(settings.config_dir)
                registrations = load_source_registry(settings.config_dir)
                if not registrations:
                    print("No sources are configured.")
                for registration in registrations:
                    state = (
                        "approved/enabled"
                        if registration.approved
                        else f"{registration.review_status}/disabled"
                    )
                    print(
                        f"{registration.source_id} [{state}] "
                        f"{registration.access_method} {registration.country}/"
                        f"{registration.language} {registration.publisher} | "
                        f"{registration.url}"
                    )
                return 0

            if args.source_command in {
                "record",
                "normalize",
                "documents",
                "duplicates",
                "review-duplicate",
                "translation-add",
                "translations",
            }:
                settings = Settings.from_environment(
                    database_path_override=args.database
                )
                database = Database(settings.database_path)
                if args.source_command == "record":
                    payload = json.loads(
                        Path(args.file).expanduser().read_text(encoding="utf-8")
                    )
                    if not isinstance(payload, dict):
                        raise ValueError("Source input must be a JSON object.")
                    source = parse_record("source", payload)
                    if not isinstance(source, Source):
                        raise ValueError("Source input did not produce a Source record.")
                    revision = database.save(source)
                    print(f"Stored source {source.source_id} at revision {revision}.")
                    return 0
                if args.source_command == "normalize":
                    documents, relationships = normalize_collection(
                        database, args.collection_id
                    )
                    print(
                        f"Normalized {len(documents)} document(s); "
                        f"recorded {len(relationships)} duplicate candidate(s)."
                    )
                    return 0
                if args.source_command == "documents":
                    if args.collection_id and args.source_id:
                        raise ValueError(
                            "Specify only one of --collection-id and --source-id."
                        )
                    documents = database.normalized_documents(
                        collection_id=args.collection_id,
                        source_id=args.source_id,
                    )
                    if not documents:
                        print("No normalized documents found.")
                    for document in documents:
                        _print_document(document)
                    return 0
                if args.source_command == "duplicates":
                    relationships = database.duplicate_relationships(
                        document_id=args.document_id,
                        review_status=args.status,
                    )
                    if not relationships:
                        print("No duplicate relationships found.")
                    for relationship in relationships:
                        _print_duplicate(relationship)
                    return 0
                if args.source_command == "review-duplicate":
                    reviewed_at = args.reviewed_at or (
                        datetime.now(timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z")
                    )
                    relationship = database.review_duplicate_relationship(
                        relationship_id=args.relationship_id,
                        decision=args.decision,
                        reviewed_by=args.reviewer,
                        reviewed_at=reviewed_at,
                        rationale=args.rationale,
                    )
                    _print_duplicate(relationship)
                    return 0
                if args.source_command == "translation-add":
                    text_path = Path(args.text_file).expanduser()
                    translated_text = text_path.read_bytes().decode("utf-8")
                    translated_at = args.translated_at or (
                        datetime.now(timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z")
                    )
                    translation = add_translation(
                        database,
                        document_id=args.document_id,
                        translated_text=translated_text,
                        target_language=args.target_language,
                        translation_method=args.method,
                        translator=args.translator,
                        translated_at=translated_at,
                    )
                    _print_translation(translation)
                    return 0
                translations = database.translations(args.document_id)
                if not translations:
                    print("No translations found.")
                for translation in translations:
                    _print_translation(translation)
                return 0

            if args.source_command == "results":
                settings = Settings.from_environment(
                    database_path_override=args.database
                )
                database = Database(settings.database_path)
                results = database.collection_results(args.source_id)
                if not results:
                    print("No collection attempts found.")
                for collection_result in results:
                    _print_collection_result(collection_result)
                return 0

            settings = Settings.from_environment(
                config_dir_override=args.config_dir,
                database_path_override=args.database,
            )
            validate_configuration(settings.config_dir)
            database = Database(settings.database_path)
            if args.source_command == "collect":
                registration = find_registration(settings.config_dir, args.source_id)
                result = collect_source(database, registration)
            elif args.source_command == "retry":
                previous = database.get_collection_result(args.collection_id)
                registration = find_registration(settings.config_dir, previous.source_id)
                result = retry_collection(database, registration, args.collection_id)
            _print_collection_result(result)
            return 0 if result.status == "SUCCEEDED" else 2

        settings = Settings.from_environment(database_path_override=args.database)
        database = Database(settings.database_path)
        migrations = database.migration_status()
        if args.database_command == "migrate":
            print(f"Database ready; {len(migrations)} migration(s) applied.")
        else:
            print(f"Database: {settings.database_path}")
            if not migrations:
                print("No migrations applied.")
            for version, applied_at in migrations:
                print(f"{version} applied at {applied_at}")
        return 0
    except (
        ConfigurationError,
        OSError,
        sqlite3.Error,
        ValueError,
        StorageError,
    ) as error:
        print(f"cnvs: error: {error}", file=sys.stderr)
        return 2
