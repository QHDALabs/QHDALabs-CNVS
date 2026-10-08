import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from cnvs import __version__
from cnvs.collection import collect_source, find_registration, retry_collection
from cnvs.configuration import ConfigurationError, validate_configuration
from cnvs.models import (
    CollectionResult,
    DuplicateRelationship,
    Event,
    EventSourceMatch,
    NormalizedDocument,
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
    return parser


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
