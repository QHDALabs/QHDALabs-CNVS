import argparse
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
    NormalizedDocument,
    TranslationRecord,
)
from cnvs.normalization import (
    add_translation,
    normalize_collection,
)
from cnvs.registry import load_source_registry
from cnvs.settings import Settings
from cnvs.storage import Database, StorageError


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
