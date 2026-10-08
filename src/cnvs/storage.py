import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterator

from cnvs.models import (
    Assessment,
    CanonicalRecord,
    Claim,
    ClaimEvidenceLink,
    ClaimExtraction,
    CollectionResult,
    CountryCoveragePlan,
    CountryMatrixAssessment,
    CountryMatrixCell,
    Evidence,
    EvidenceGap,
    EvidenceNote,
    EvidenceReview,
    DuplicateRelationship,
    Event,
    EventSourceMatch,
    IndependenceAssignment,
    JsonValue,
    NationalInformationMatrix,
    NormalizedDocument,
    ProvenanceLink,
    ProvenanceOriginAssessment,
    ReportReview,
    ReportSnapshot,
    RawSnapshot,
    Source,
    TimelineEntry,
    TranslationRecord,
)
from cnvs.validation import (
    RecordValidationError,
    parse_record,
    validate_record,
)


class StorageError(RuntimeError):
    """Raised when the database cannot safely store or reconstruct a record."""


def _validate_timestamp(value: str, name: str) -> None:
    if _RFC3339_TIMESTAMP.fullmatch(value) is None:
        raise StorageError(f"{name} must be an RFC3339 timestamp with a timezone.")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StorageError(f"{name} must be a valid RFC3339 timestamp.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise StorageError(f"{name} must include a timezone.")


@dataclass(frozen=True)
class ReconstructedAssessment:
    assessment: Assessment
    revision: int
    records: dict[tuple[str, str], CanonicalRecord]
    record_revisions: dict[tuple[str, str], int]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_text(payload: dict[str, JsonValue]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_array_text(payload: Sequence[JsonValue]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})$"
)


class Database:
    """SQLite persistence with checksummed migrations and immutable revisions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    @staticmethod
    def _execute_migration(connection: sqlite3.Connection, sql: str) -> None:
        statement = ""
        for line in sql.splitlines():
            statement += line + "\n"
            if sqlite3.complete_statement(statement):
                if statement.strip():
                    connection.execute(statement)
                statement = ""
        if statement.strip():
            raise StorageError("Migration contains an incomplete SQL statement.")

    def migrate(self) -> int:
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        checksum TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                resources = files("cnvs").joinpath("migrations")
                migration_files = sorted(
                    name for name in resources.iterdir() if name.name.endswith(".sql")
                )
                known_versions: set[str] = set()
                for resource in migration_files:
                    version = resource.name.removesuffix(".sql")
                    known_versions.add(version)
                    sql = resource.read_text(encoding="utf-8")
                    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        applied = connection.execute(
                            "SELECT checksum FROM schema_migrations WHERE version = ?",
                            (version,),
                        ).fetchone()
                        if applied is not None:
                            if applied["checksum"] != checksum:
                                raise StorageError(
                                    f"Applied migration {version} has changed; "
                                    "create a new migration."
                                )
                            connection.commit()
                            continue
                        self._execute_migration(connection, sql)
                        connection.execute(
                            """
                            INSERT INTO schema_migrations (version, checksum, applied_at)
                            VALUES (?, ?, ?)
                            """,
                            (version, checksum, _utc_now()),
                        )
                        connection.commit()
                    except StorageError:
                        connection.rollback()
                        raise
                    except sqlite3.Error as error:
                        connection.rollback()
                        raise StorageError(
                            f"Database migration {version} failed: {error}"
                        ) from error
                applied_versions = {
                    row["version"]
                    for row in connection.execute("SELECT version FROM schema_migrations")
                }
                unknown = applied_versions - known_versions
                if unknown:
                    versions = ", ".join(sorted(unknown))
                    raise StorageError(
                        f"Database contains unknown migration versions: {versions}."
                    )
                return len(known_versions)

    def migration_status(self) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        return [(row["version"], row["applied_at"]) for row in rows]

    def save(self, record: CanonicalRecord) -> int:
        return self._save(record)

    def _save(
        self,
        record: CanonicalRecord,
        *,
        create_only: bool = False,
        require_existing: bool = False,
    ) -> int:
        payload = validate_record(record)
        if (
            isinstance(record, Event)
            and record.start_time is not None
            and record.end_time is not None
        ):
            start_time = datetime.fromisoformat(
                record.start_time.replace("Z", "+00:00")
            )
            end_time = datetime.fromisoformat(record.end_time.replace("Z", "+00:00"))
            if end_time < start_time:
                raise StorageError("Event end_time must not precede start_time.")
        record_id = getattr(record, record.record_id_field)
        payload_json = _json_text(payload)
        timestamp = _utc_now()
        with self._lock, self._write_transaction() as connection:
            self._validate_links(connection, record)
            if isinstance(record, Source) and record.raw_content_ref.startswith("sha256:"):
                digest = record.raw_content_ref.removeprefix("sha256:")
                snapshot = connection.execute(
                    "SELECT 1 FROM raw_snapshots WHERE content_sha256 = ?", (digest,)
                ).fetchone()
                if snapshot is None:
                    raise StorageError(
                        f"Source {record.source_id} references a raw snapshot that is not stored."
                    )

            previous = connection.execute(
                """
                SELECT revision FROM canonical_records
                WHERE record_type = ? AND record_id = ?
                """,
                (record.record_type, record_id),
            ).fetchone()
            if create_only and previous is not None:
                raise StorageError(
                    f"{record.record_type.capitalize()} {record_id} already exists."
                )
            if require_existing and previous is None:
                raise StorageError(
                    f"{record.record_type.capitalize()} {record_id} was not found."
                )
            revision = 1 if previous is None else previous["revision"] + 1
            connection.execute(
                """
                INSERT INTO record_revisions
                    (record_type, record_id, revision, schema_version, payload_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_type,
                    record_id,
                    revision,
                    record.schema_version,
                    payload_json,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO canonical_records
                    (record_type, record_id, revision, schema_version, payload_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (record_type, record_id) DO UPDATE SET
                    revision = excluded.revision,
                    schema_version = excluded.schema_version,
                    payload_json = excluded.payload_json,
                    recorded_at = excluded.recorded_at
                """,
                (
                    record.record_type,
                    record_id,
                    revision,
                    record.schema_version,
                    payload_json,
                    timestamp,
                ),
            )
            if isinstance(record, Assessment):
                record_set = self._capture_record_set(connection, record)
                connection.execute(
                    """
                    INSERT INTO assessment_revisions
                        (assessment_id, revision, event_id, schema_version,
                         payload_json, record_set_json, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.assessment_id,
                        revision,
                        record.event_id,
                        record.schema_version,
                        payload_json,
                        _json_text(record_set),
                        timestamp,
                    ),
                )
            return revision

    def _validate_links(
        self, connection: sqlite3.Connection, record: CanonicalRecord
    ) -> None:
        if isinstance(record, (Claim, Evidence, Assessment)):
            self._require_record(connection, "event", record.event_id)
        if isinstance(record, (Claim, Evidence)):
            self._require_record(connection, "source", record.source_id)
        if isinstance(record, Source) and record.origin_source_id:
            self._require_record(connection, "source", record.origin_source_id)
        if isinstance(record, Claim):
            all_ids = set(record.supporting_evidence_ids) | set(
                record.contradicting_evidence_ids
            )
            for evidence_id in sorted(all_ids):
                evidence = self._require_record(connection, "evidence", evidence_id)
                if evidence["event_id"] != record.event_id:
                    raise StorageError(
                        f"Claim {record.claim_id} references evidence {evidence_id} "
                        "from a different event."
                    )
        if isinstance(record, Assessment):
            for claim_id in record.claim_ids:
                claim = self._require_record(connection, "claim", claim_id)
                if claim["event_id"] != record.event_id:
                    raise StorageError(
                        f"Assessment {record.assessment_id} references claim {claim_id} "
                        "from a different event."
                    )
            for evidence_id in record.evidence_ids:
                evidence = self._require_record(connection, "evidence", evidence_id)
                if evidence["event_id"] != record.event_id:
                    raise StorageError(
                        f"Assessment {record.assessment_id} references evidence "
                        f"{evidence_id} from a different event."
                    )

    @staticmethod
    def _require_record(
        connection: sqlite3.Connection, record_type: str, record_id: str
    ) -> dict[str, JsonValue]:
        row = connection.execute(
            """
            SELECT payload_json FROM canonical_records
            WHERE record_type = ? AND record_id = ?
            """,
            (record_type, record_id),
        ).fetchone()
        if row is None:
            raise StorageError(f"Referenced {record_type} record {record_id} does not exist.")
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise StorageError(
                f"Stored {record_type} record {record_id} is not a JSON object."
            )
        return payload

    def add_raw_snapshot(self, snapshot: RawSnapshot) -> str:
        if len(snapshot.content_sha256) != 64:
            raise StorageError("Raw snapshot SHA-256 digest must have 64 hexadecimal characters.")
        actual_digest = hashlib.sha256(snapshot.content).hexdigest()
        if actual_digest != snapshot.content_sha256:
            raise StorageError("Raw snapshot content does not match its SHA-256 digest.")
        if not isinstance(snapshot.media_type, str) or not snapshot.media_type.strip():
            raise StorageError("Raw snapshot media type must not be empty.")
        try:
            created_at = datetime.fromisoformat(
                snapshot.created_at.replace("Z", "+00:00")
            )
        except (AttributeError, ValueError) as error:
            raise StorageError("Raw snapshot created_at must be a valid timestamp.") from error
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise StorageError("Raw snapshot created_at must include a timezone.")
        with self._lock, self._write_transaction() as connection:
            existing = connection.execute(
                "SELECT media_type, content, byte_length FROM raw_snapshots "
                "WHERE content_sha256 = ?",
                (snapshot.content_sha256,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["media_type"] != snapshot.media_type
                    or bytes(existing["content"]) != snapshot.content
                    or existing["byte_length"] != len(snapshot.content)
                ):
                    raise StorageError(
                        "An existing immutable snapshot has conflicting content or media type."
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO raw_snapshots
                        (content_sha256, media_type, content, byte_length, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.content_sha256,
                        snapshot.media_type,
                        snapshot.content,
                        len(snapshot.content),
                        snapshot.created_at,
                    ),
                )
        return snapshot.content_ref

    def record_collection_result(
        self,
        *,
        source_id: str,
        source_metadata: dict[str, JsonValue] | None = None,
        source_url: str,
        status: str,
        started_at: str,
        completed_at: str,
        retry_of: str | None = None,
        final_url: str | None = None,
        http_status: int | None = None,
        content_type: str | None = None,
        content: bytes | None = None,
        archive_content: bool = False,
        origin_identifiers: tuple[str, ...] = (),
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> CollectionResult:
        if status not in {"SUCCEEDED", "FAILED", "BLOCKED"}:
            raise StorageError(f"Unsupported collection status: {status}.")
        if status == "SUCCEEDED" and (error_code is not None or error_message is not None):
            raise StorageError("Successful collection results cannot contain an error.")
        if status != "SUCCEEDED" and (not error_code or not error_message):
            raise StorageError("Failed and blocked collection results need an error code and message.")
        content_digest = hashlib.sha256(content).hexdigest() if content is not None else None
        collection_id = uuid.uuid4().hex
        source_metadata_json = _json_text(source_metadata or {})
        encoded_identifiers = json.dumps(
            list(origin_identifiers), ensure_ascii=False, separators=(",", ":")
        )

        with self._lock, self._write_transaction() as connection:
            if retry_of is None:
                previous = connection.execute(
                    """
                    SELECT attempt_number FROM collection_attempts
                    WHERE source_id = ? ORDER BY attempt_number DESC LIMIT 1
                    """,
                    (source_id,),
                ).fetchone()
                if previous is not None:
                    raise StorageError(
                        f"Source {source_id} already has a collection attempt; "
                        "retries must identify the failed attempt."
                    )
                attempt_number = 1
            else:
                previous = connection.execute(
                    """
                    SELECT source_id, attempt_number, status
                    FROM collection_attempts WHERE collection_id = ?
                    """,
                    (retry_of,),
                ).fetchone()
                if previous is None:
                    raise StorageError(f"Collection attempt {retry_of} was not found.")
                if previous["status"] != "FAILED":
                    raise StorageError("Only failed collection attempts can be retried.")
                if previous["source_id"] != source_id:
                    raise StorageError("A retry must use the same registered source.")
                latest = connection.execute(
                    """
                    SELECT collection_id FROM collection_attempts
                    WHERE source_id = ? ORDER BY attempt_number DESC LIMIT 1
                    """,
                    (source_id,),
                ).fetchone()
                if latest is None or latest["collection_id"] != retry_of:
                    raise StorageError("Only the latest collection attempt can be retried.")
                attempt_number = previous["attempt_number"] + 1

            archived_digest: str | None = None
            if archive_content and content is not None:
                existing = connection.execute(
                    "SELECT content FROM raw_snapshots WHERE content_sha256 = ?",
                    (content_digest,),
                ).fetchone()
                if existing is not None:
                    if bytes(existing["content"]) != content:
                        raise StorageError("Existing raw snapshot failed its digest integrity check.")
                else:
                    connection.execute(
                        """
                        INSERT INTO raw_snapshots
                            (content_sha256, media_type, content, byte_length, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            content_digest,
                            content_type or "application/octet-stream",
                            content,
                            len(content),
                            completed_at,
                        ),
                    )
                archived_digest = content_digest

            connection.execute(
                """
                INSERT INTO collection_attempts (
                    collection_id, source_id, source_metadata_json, attempt_number,
                    status, retry_of,
                    source_url, final_url, started_at, completed_at, http_status,
                    content_type, content_sha256, archived_content_sha256,
                    origin_identifiers_json, error_code, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    collection_id,
                    source_id,
                    source_metadata_json,
                    attempt_number,
                    status,
                    retry_of,
                    source_url,
                    final_url,
                    started_at,
                    completed_at,
                    http_status,
                    content_type,
                    content_digest,
                    archived_digest,
                    encoded_identifiers,
                    error_code,
                    error_message,
                ),
            )
        return self.get_collection_result(collection_id)

    @staticmethod
    def _collection_result_from_row(row: sqlite3.Row) -> CollectionResult:
        return CollectionResult(
            collection_id=row["collection_id"],
            source_id=row["source_id"],
            source_metadata=json.loads(row["source_metadata_json"]),
            attempt_number=row["attempt_number"],
            status=row["status"],
            retry_of=row["retry_of"],
            source_url=row["source_url"],
            final_url=row["final_url"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            http_status=row["http_status"],
            content_type=row["content_type"],
            content_sha256=row["content_sha256"],
            archived_content_ref=(
                f"sha256:{row['archived_content_sha256']}"
                if row["archived_content_sha256"] is not None
                else None
            ),
            origin_identifiers=tuple(json.loads(row["origin_identifiers_json"])),
            error_code=row["error_code"],
            error_message=row["error_message"],
        )

    def get_collection_result(self, collection_id: str) -> CollectionResult:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM collection_attempts WHERE collection_id = ?",
                (collection_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"Collection attempt {collection_id} was not found.")
        return self._collection_result_from_row(row)

    def collection_results(self, source_id: str | None = None) -> list[CollectionResult]:
        with self._connect() as connection:
            if source_id is None:
                rows = connection.execute(
                    "SELECT * FROM collection_attempts "
                    "ORDER BY started_at DESC, collection_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM collection_attempts WHERE source_id = ?
                    ORDER BY attempt_number DESC
                    """,
                    (source_id,),
                ).fetchall()
        return [self._collection_result_from_row(row) for row in rows]

    @staticmethod
    def _normalized_document_from_row(row: sqlite3.Row) -> NormalizedDocument:
        return NormalizedDocument(
            document_id=row["document_id"],
            collection_id=row["collection_id"],
            source_id=row["source_id"],
            item_index=row["item_index"],
            origin_identifier=row["origin_identifier"],
            original_title=row["original_title"],
            normalized_title=row["normalized_title"],
            original_text=row["original_text"],
            normalized_text=row["normalized_text"],
            original_url=row["original_url"],
            normalized_url=row["normalized_url"],
            publisher_original=row["publisher_original"],
            publisher_normalized=row["publisher_normalized"],
            language=row["language"],
            language_source=row["language_source"],
            language_review_status=row["language_review_status"],
            registry_language=row["registry_language"],
            declared_language=row["declared_language"],
            original_published_at=row["original_published_at"],
            normalized_published_at=row["normalized_published_at"],
            publication_timezone_known=bool(row["publication_timezone_known"]),
            text_sha256=row["text_sha256"],
            normalized_text_sha256=row["normalized_text_sha256"],
            created_at=row["created_at"],
        )

    def save_normalized_documents(
        self, documents: list[NormalizedDocument]
    ) -> tuple[list[NormalizedDocument], list[DuplicateRelationship]]:
        if not documents:
            return [], []
        collection_ids = {document.collection_id for document in documents}
        if len(collection_ids) != 1:
            raise StorageError("A normalization batch must belong to one collection.")
        collection_id = next(iter(collection_ids))
        result = self.get_collection_result(collection_id)
        if result.status != "SUCCEEDED":
            raise StorageError(
                f"Cannot normalize collection {collection_id} with status {result.status}."
            )
        if result.archived_content_ref is None:
            raise StorageError(
                f"Collection {collection_id} has no retained content for normalization."
            )
        snapshot = self.get_raw_snapshot(result.archived_content_ref)
        from cnvs.normalization import parse_collection_documents

        derived_documents = parse_collection_documents(
            collection_id=result.collection_id,
            source_id=result.source_id,
            source_metadata=result.source_metadata,
            source_url=result.source_url,
            final_url=result.final_url,
            content_type=result.content_type,
            content=snapshot.content,
            created_at=result.completed_at,
        )
        if documents != derived_documents:
            raise StorageError(
                f"Normalized documents do not match archived collection {collection_id}."
            )
        inserted_document_ids: list[str] = []
        with self._lock, self._write_transaction() as connection:
            collection = connection.execute(
                """
                SELECT source_id, status, archived_content_sha256
                FROM collection_attempts WHERE collection_id = ?
                """,
                (collection_id,),
            ).fetchone()
            if (
                collection is None
                or collection["source_id"] != result.source_id
                or collection["status"] != "SUCCEEDED"
                or collection["archived_content_sha256"] != snapshot.content_sha256
            ):
                raise StorageError(
                    f"Collection {collection_id} changed or failed its snapshot integrity check."
                )
            from cnvs.normalization import detect_relationship

            for document in documents:
                if document.source_id != result.source_id:
                    raise StorageError(
                        f"Document {document.document_id} source does not match its collection."
                    )
                if not document.original_text and not document.original_title:
                    raise StorageError(
                        f"Document {document.document_id} has no original title or text."
                    )
                actual_text_digest = hashlib.sha256(
                    document.original_text.encode("utf-8")
                ).hexdigest()
                normalized_digest = hashlib.sha256(
                    document.normalized_text.encode("utf-8")
                ).hexdigest()
                if (
                    actual_text_digest != document.text_sha256
                    or normalized_digest != document.normalized_text_sha256
                ):
                    raise StorageError(
                        f"Document {document.document_id} text digest validation failed."
                    )
                language_states = {
                    ("UNKNOWN", "UNKNOWN"),
                    ("UNKNOWN", "REVIEW_REQUIRED"),
                    ("DOCUMENT_DECLARED", "RECORDED"),
                    ("DOCUMENT_DECLARED", "REVIEW_REQUIRED"),
                    ("REGISTRY", "RECORDED"),
                    ("REGISTRY", "REVIEW_REQUIRED"),
                }
                if (
                    document.language_review_status == "UNKNOWN"
                    and document.language != "unknown"
                ) or (
                    (document.language_source, document.language_review_status)
                    not in language_states
                ):
                    raise StorageError(
                        f"Document {document.document_id} has inconsistent language provenance."
                    )
                if document.language == "unknown" and document.language_source != "UNKNOWN":
                    raise StorageError(
                        f"Document {document.document_id} must explicitly mark unknown language."
                    )
                if document.language not in {
                    "unknown", "pl", "de", "fr", "en", "fi", "et", "uk",
                    "ro", "ka", "be", "ru",
                }:
                    raise StorageError(
                        f"Document {document.document_id} has an unsupported language code."
                    )
                if document.normalized_published_at is not None:
                    try:
                        parsed_publication = datetime.fromisoformat(
                            document.normalized_published_at.replace("Z", "+00:00")
                        )
                    except ValueError as error:
                        raise StorageError(
                            f"Document {document.document_id} has an invalid normalized timestamp."
                        ) from error
                    has_timezone = (
                        parsed_publication.tzinfo is not None
                        and parsed_publication.utcoffset() is not None
                    )
                    if has_timezone != document.publication_timezone_known:
                        raise StorageError(
                            f"Document {document.document_id} has inconsistent "
                            "publication timezone metadata."
                        )
                    if document.publication_timezone_known and (
                        _RFC3339_TIMESTAMP.fullmatch(
                            document.normalized_published_at
                        )
                        is None
                    ):
                        raise StorageError(
                            f"Document {document.document_id} normalized publication "
                            "timestamp must be RFC3339."
                        )
                existing_row = connection.execute(
                    """
                    SELECT * FROM normalized_documents
                    WHERE document_id = ?
                    """,
                    (document.document_id,),
                ).fetchone()
                if existing_row is not None:
                    existing_document = self._normalized_document_from_row(
                        existing_row
                    )
                    if existing_document != document:
                        raise StorageError(
                            f"Normalized document {document.document_id} already exists "
                            "with different immutable content."
                        )
                    continue
                same_item = connection.execute(
                    """
                    SELECT document_id FROM normalized_documents
                    WHERE collection_id = ? AND item_index = ?
                    """,
                    (document.collection_id, document.item_index),
                ).fetchone()
                if same_item is not None:
                    raise StorageError(
                        f"Collection {collection_id} item {document.item_index} was "
                        "already normalized with a different identity."
                    )
                connection.execute(
                    """
                    INSERT INTO normalized_documents (
                        document_id, collection_id, source_id, item_index,
                        origin_identifier, original_title, normalized_title,
                        original_text, normalized_text, original_url, normalized_url,
                        publisher_original, publisher_normalized, language,
                        language_source, language_review_status, registry_language,
                        declared_language, original_published_at,
                        normalized_published_at, publication_timezone_known,
                        text_sha256, normalized_text_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document.document_id,
                        document.collection_id,
                        document.source_id,
                        document.item_index,
                        document.origin_identifier,
                        document.original_title,
                        document.normalized_title,
                        document.original_text,
                        document.normalized_text,
                        document.original_url,
                        document.normalized_url,
                        document.publisher_original,
                        document.publisher_normalized,
                        document.language,
                        document.language_source,
                        document.language_review_status,
                        document.registry_language,
                        document.declared_language,
                        document.original_published_at,
                        document.normalized_published_at,
                        int(document.publication_timezone_known),
                        document.text_sha256,
                        document.normalized_text_sha256,
                        document.created_at,
                    ),
                )
                inserted_document_ids.append(document.document_id)
                existing_rows = connection.execute(
                    """
                    SELECT * FROM normalized_documents
                    WHERE document_id != ?
                      AND (
                        normalized_text_sha256 = ?
                        OR (
                            normalized_url IS NOT NULL
                            AND normalized_url = ?
                        )
                    )
                    """,
                    (
                        document.document_id,
                        document.normalized_text_sha256,
                        document.normalized_url,
                    ),
                ).fetchall()
                if (
                    document.normalized_text
                    and len(document.normalized_text) >= 120
                ):
                    existing_rows += connection.execute(
                        """
                        SELECT * FROM normalized_documents
                        WHERE document_id != ?
                          AND length(normalized_text) >= 120
                          AND document_id NOT IN (
                              SELECT document_id FROM normalized_documents
                              WHERE document_id != ?
                                AND (
                                    normalized_text_sha256 = ?
                                    OR normalized_url = ?
                                )
                          )
                        """,
                        (
                            document.document_id,
                            document.document_id,
                            document.normalized_text_sha256,
                            document.normalized_url,
                        ),
                    ).fetchall()
                for existing_row in existing_rows:
                    existing = self._normalized_document_from_row(existing_row)
                    relationship = detect_relationship(document, existing)
                    if relationship is None:
                        continue
                    relationship_type, similarity = relationship
                    first_id, second_id = sorted(
                        (document.document_id, existing.document_id)
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO duplicate_relationships (
                            relationship_id, document_id, related_document_id,
                            relationship_type, similarity, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            first_id,
                            second_id,
                            relationship_type,
                            similarity,
                            document.created_at,
                        ),
                    )
            saved_rows = connection.execute(
                """
                SELECT * FROM normalized_documents
                WHERE collection_id = ? ORDER BY item_index
                """,
                (collection_id,),
            ).fetchall()
        saved_documents = [
            self._normalized_document_from_row(row) for row in saved_rows
        ]
        relationships = {
            relationship.relationship_id: relationship
            for document_id in (
                inserted_document_ids
                or [document.document_id for document in saved_documents]
            )
            for relationship in self.duplicate_relationships(
                document_id=document_id
            )
        }
        return saved_documents, list(relationships.values())

    def normalized_documents(
        self, *, collection_id: str | None = None, source_id: str | None = None
    ) -> list[NormalizedDocument]:
        if collection_id is not None and source_id is not None:
            raise ValueError("Specify collection_id or source_id, not both.")
        with self._connect() as connection:
            if collection_id is not None:
                rows = connection.execute(
                    """
                    SELECT * FROM normalized_documents
                    WHERE collection_id = ? ORDER BY item_index
                    """,
                    (collection_id,),
                ).fetchall()
            elif source_id is not None:
                rows = connection.execute(
                    """
                    SELECT * FROM normalized_documents
                    WHERE source_id = ? ORDER BY created_at, item_index
                    """,
                    (source_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM normalized_documents ORDER BY created_at, item_index"
                ).fetchall()
        return [self._normalized_document_from_row(row) for row in rows]

    def list_events(self) -> list[Event]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM canonical_records
                WHERE record_type = 'event'
                ORDER BY record_id
                """
            ).fetchall()
        return [
            parse_record("event", json.loads(row["payload_json"]))
            for row in rows
        ]

    def create_event(self, event: Event) -> int:
        return self._save(event, create_only=True)

    def update_event(self, event: Event) -> int:
        return self._save(event, require_existing=True)

    @staticmethod
    def _event_source_match_from_row(row: sqlite3.Row) -> EventSourceMatch:
        decision = row["review_decision"]
        return EventSourceMatch(
            match_id=row["match_id"],
            event_id=row["event_id"],
            document_id=row["document_id"],
            source_id=row["source_id"],
            status=decision or "PENDING",
            proposed_by=row["proposed_by"],
            proposed_at=row["proposed_at"],
            rationale=row["rationale"],
            review_decision=decision,
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_rationale=row["review_rationale"],
        )

    @staticmethod
    def _event_source_match_select() -> str:
        return """
            SELECT match.*,
                document.source_id,
                review.decision AS review_decision,
                review.reviewed_by,
                review.reviewed_at,
                review.rationale AS review_rationale
            FROM event_source_matches AS match
            JOIN normalized_documents AS document
                ON document.document_id = match.document_id
            LEFT JOIN event_source_match_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM event_source_match_reviews AS latest
                    WHERE latest.match_id = match.match_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
        """

    def propose_event_source_match(
        self,
        *,
        event_id: str,
        document_id: str,
        proposed_by: str,
        proposed_at: str,
        rationale: str,
    ) -> EventSourceMatch:
        if not proposed_by.strip() or not rationale.strip():
            raise StorageError(
                "An event match proposal needs an analyst and rationale."
            )
        _validate_timestamp(proposed_at, "proposed_at")
        match_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            self._require_record(connection, "event", event_id)
            document = connection.execute(
                "SELECT 1 FROM normalized_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if document is None:
                raise StorageError(f"Normalized document {document_id} was not found.")
            try:
                connection.execute(
                    """
                    INSERT INTO event_source_matches (
                        match_id, event_id, document_id, rationale,
                        proposed_by, proposed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        match_id,
                        event_id,
                        document_id,
                        rationale.strip(),
                        proposed_by.strip(),
                        proposed_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StorageError(
                    f"A candidate match already exists for event {event_id} "
                    f"and document {document_id}."
                ) from error
        return self.get_event_source_match(match_id)

    def get_event_source_match(self, match_id: str) -> EventSourceMatch:
        with self._connect() as connection:
            row = connection.execute(
                self._event_source_match_select()
                + " WHERE match.match_id = ?",
                (match_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"Event source match {match_id} was not found.")
        return self._event_source_match_from_row(row)

    def event_source_matches(
        self,
        *,
        event_id: str | None = None,
        document_id: str | None = None,
        status: str | None = None,
    ) -> list[EventSourceMatch]:
        if status not in {None, "PENDING", "LINKED", "REJECTED", "UNRESOLVED"}:
            raise ValueError("Unsupported event source match status.")
        query = self._event_source_match_select() + """
            WHERE (? IS NULL OR match.event_id = ?)
              AND (? IS NULL OR match.document_id = ?)
              AND (? IS NULL OR COALESCE(review.decision, 'PENDING') = ?)
            ORDER BY match.proposed_at, match.match_id
        """
        with self._connect() as connection:
            rows = connection.execute(
                query,
                (event_id, event_id, document_id, document_id, status, status),
            ).fetchall()
        return [self._event_source_match_from_row(row) for row in rows]

    def event_source_match_history(self, match_id: str) -> list[EventSourceMatch]:
        with self._connect() as connection:
            proposal = connection.execute(
                """
                SELECT match.*, document.source_id
                FROM event_source_matches AS match
                JOIN normalized_documents AS document
                    ON document.document_id = match.document_id
                WHERE match.match_id = ?
                """,
                (match_id,),
            ).fetchone()
            if proposal is None:
                raise StorageError(f"Event source match {match_id} was not found.")
            reviews = connection.execute(
                """
                SELECT decision, reviewed_by, reviewed_at, rationale
                FROM event_source_match_reviews
                WHERE match_id = ?
                ORDER BY rowid
                """,
                (match_id,),
            ).fetchall()
        history = [
            EventSourceMatch(
                match_id=proposal["match_id"],
                event_id=proposal["event_id"],
                document_id=proposal["document_id"],
                source_id=proposal["source_id"],
                status="PENDING",
                proposed_by=proposal["proposed_by"],
                proposed_at=proposal["proposed_at"],
                rationale=proposal["rationale"],
                review_decision=None,
                reviewed_by=None,
                reviewed_at=None,
                review_rationale=None,
            )
        ]
        history.extend(
            EventSourceMatch(
                match_id=proposal["match_id"],
                event_id=proposal["event_id"],
                document_id=proposal["document_id"],
                source_id=proposal["source_id"],
                status=review["decision"],
                proposed_by=proposal["proposed_by"],
                proposed_at=proposal["proposed_at"],
                rationale=proposal["rationale"],
                review_decision=review["decision"],
                reviewed_by=review["reviewed_by"],
                reviewed_at=review["reviewed_at"],
                review_rationale=review["rationale"],
            )
            for review in reviews
        )
        return history

    def review_event_source_match(
        self,
        *,
        match_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
    ) -> EventSourceMatch:
        if decision not in {"LINKED", "REJECTED", "UNRESOLVED"}:
            raise StorageError(f"Unsupported event match decision: {decision}.")
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("An event match review needs an analyst and rationale.")
        _validate_timestamp(reviewed_at, "reviewed_at")
        review_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            match = connection.execute(
                """
                SELECT event_id, document_id FROM event_source_matches
                WHERE match_id = ?
                """,
                (match_id,),
            ).fetchone()
            if match is None:
                raise StorageError(f"Event source match {match_id} was not found.")
            connection.execute(
                """
                INSERT INTO event_source_match_reviews (
                    review_id, match_id, decision, reviewed_by, reviewed_at,
                    rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    match_id,
                    decision,
                    reviewed_by.strip(),
                    reviewed_at,
                    rationale.strip(),
                ),
            )
            if decision == "LINKED":
                existing = connection.execute(
                    """
                    SELECT 1 FROM event_timeline_entries
                    WHERE event_id = ? AND document_id = ?
                    """,
                    (match["event_id"], match["document_id"]),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO event_timeline_entries (
                            entry_id, event_id, document_id, revision, event_time,
                            event_time_rationale, updated_by, updated_at
                        ) VALUES (?, ?, ?, 1, NULL, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            match["event_id"],
                            match["document_id"],
                            "Event occurrence time has not been established.",
                            reviewed_by.strip(),
                            reviewed_at,
                        ),
                    )
        return self.get_event_source_match(match_id)

    @staticmethod
    def _timeline_entry_from_row(row: sqlite3.Row) -> TimelineEntry:
        return TimelineEntry(
            entry_id=row["entry_id"],
            event_id=row["event_id"],
            document_id=row["document_id"],
            source_id=row["source_id"],
            publisher=row["publisher_original"],
            revision=row["revision"],
            event_time=row["event_time"],
            event_time_rationale=row["event_time_rationale"],
            updated_by=row["updated_by"],
            updated_at=row["updated_at"],
            original_publication_time=row["original_published_at"],
            normalized_publication_time=row["normalized_published_at"],
            publication_timezone_known=bool(row["publication_timezone_known"]),
            collection_time=row["collection_time"],
            source_title=row["original_title"] or row["normalized_title"],
            source_url=row["original_url"],
        )

    @staticmethod
    def _timeline_entry_select() -> str:
        return """
            SELECT timeline.entry_id, timeline.event_id, timeline.document_id,
                timeline.revision, timeline.event_time,
                timeline.event_time_rationale, timeline.updated_by,
                timeline.updated_at, document.original_published_at,
                document.normalized_published_at,
                document.publication_timezone_known,
                collection.completed_at AS collection_time,
                document.source_id, document.publisher_original,
                document.original_title, document.normalized_title,
                document.original_url
            FROM event_timeline_entries AS timeline
            JOIN normalized_documents AS document
                ON document.document_id = timeline.document_id
            JOIN collection_attempts AS collection
                ON collection.collection_id = document.collection_id
        """

    @staticmethod
    def _timeline_order_key(entry: TimelineEntry) -> tuple[str, str, str]:
        value = (
            entry.event_time
            or (
                entry.normalized_publication_time
                if entry.publication_timezone_known
                else entry.original_publication_time
            )
            or entry.collection_time
        )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            sortable = value
        else:
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                sortable = (
                    parsed.astimezone(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
            else:
                sortable = parsed.isoformat(timespec="microseconds")
        return (sortable, entry.event_id, entry.entry_id)

    def timeline_entries(self, event_id: str) -> list[TimelineEntry]:
        query = self._timeline_entry_select() + """
                JOIN event_source_matches AS match
                    ON match.event_id = timeline.event_id
                    AND match.document_id = timeline.document_id
                WHERE timeline.event_id = ?
                  AND timeline.revision = (
                      SELECT MAX(latest.revision)
                      FROM event_timeline_entries AS latest
                      WHERE latest.entry_id = timeline.entry_id
                  )
                  AND COALESCE((
                      SELECT review.decision
                      FROM event_source_match_reviews AS review
                      WHERE review.match_id = match.match_id
                      ORDER BY review.rowid DESC LIMIT 1
                  ), 'PENDING') = 'LINKED'
        """
        with self._connect() as connection:
            rows = connection.execute(query, (event_id,)).fetchall()
        entries = [self._timeline_entry_from_row(row) for row in rows]
        return sorted(entries, key=self._timeline_order_key)

    def timeline_entry_history(self, entry_id: str) -> list[TimelineEntry]:
        query = self._timeline_entry_select() + """
                WHERE timeline.entry_id = ?
                ORDER BY timeline.revision
        """
        with self._connect() as connection:
            rows = connection.execute(query, (entry_id,)).fetchall()
        if not rows:
            raise StorageError(f"Timeline entry {entry_id} was not found.")
        return [self._timeline_entry_from_row(row) for row in rows]

    def revise_timeline_entry(
        self,
        *,
        entry_id: str,
        event_time: str | None,
        updated_by: str,
        updated_at: str,
        rationale: str,
    ) -> TimelineEntry:
        if event_time is not None:
            _validate_timestamp(event_time, "event_time")
        if not updated_by.strip() or not rationale.strip():
            raise StorageError("A timeline correction needs an analyst and rationale.")
        _validate_timestamp(updated_at, "updated_at")
        with self._lock, self._write_transaction() as connection:
            previous = connection.execute(
                """
                SELECT event_id, document_id, revision
                FROM event_timeline_entries
                WHERE entry_id = ?
                ORDER BY revision DESC LIMIT 1
                """,
                (entry_id,),
            ).fetchone()
            if previous is None:
                raise StorageError(f"Timeline entry {entry_id} was not found.")
            current_match = connection.execute(
                self._event_source_match_select()
                + """
                    WHERE match.event_id = ? AND match.document_id = ?
                """,
                (previous["event_id"], previous["document_id"]),
            ).fetchone()
            if (
                current_match is None
                or self._event_source_match_from_row(current_match).status != "LINKED"
            ):
                raise StorageError(
                    "Timeline entries can only be corrected while the source link is confirmed."
                )
            connection.execute(
                """
                INSERT INTO event_timeline_entries (
                    entry_id, event_id, document_id, revision, event_time,
                    event_time_rationale, updated_by, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    previous["event_id"],
                    previous["document_id"],
                    previous["revision"] + 1,
                    event_time,
                    rationale.strip(),
                    updated_by.strip(),
                    updated_at,
                ),
            )
        return self.timeline_entry_history(entry_id)[-1]

    @staticmethod
    def _claim_extraction_from_row(row: sqlite3.Row) -> ClaimExtraction:
        available_columns = set(row.keys())
        decision = (
            row["review_decision"]
            if "review_decision" in available_columns
            else None
        )
        correction = (
            row["correction_json"]
            if "correction_json" in available_columns
            else None
        )
        return ClaimExtraction(
            candidate_id=row["candidate_id"],
            event_id=row["event_id"],
            document_id=row["document_id"],
            source_id=row["source_id"],
            source_text_sha256=row["source_text_sha256"],
            span_start=row["span_start"],
            span_end=row["span_end"],
            span_text=row["span_text"],
            subject=row["subject"],
            predicate=row["predicate"],
            object=row["object"],
            claim_type=row["claim_type"],
            attribution=row["attribution"],
            modality=row["modality"],
            extraction_method=row["extraction_method"],
            extractor=row["extractor"],
            created_at=row["created_at"],
            review_status=decision or "PENDING",
            review_decision=decision,
            reviewed_by=(
                row["reviewed_by"] if "reviewed_by" in available_columns else None
            ),
            reviewed_at=(
                row["reviewed_at"] if "reviewed_at" in available_columns else None
            ),
            review_rationale=(
                row["review_rationale"]
                if "review_rationale" in available_columns
                else None
            ),
            correction_payload=json.loads(correction) if correction else None,
        )

    @staticmethod
    def _claim_extraction_select() -> str:
        return """
            SELECT candidate.*,
                review.decision AS review_decision,
                review.reviewed_by,
                review.reviewed_at,
                review.rationale AS review_rationale,
                correction.correction_json
            FROM claim_extractions AS candidate
            LEFT JOIN claim_extraction_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM claim_extraction_reviews AS latest
                    WHERE latest.candidate_id = candidate.candidate_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
            LEFT JOIN claim_extraction_reviews AS correction
                ON correction.review_id = (
                    SELECT latest_correction.review_id
                    FROM claim_extraction_reviews AS latest_correction
                    WHERE latest_correction.candidate_id = candidate.candidate_id
                      AND latest_correction.decision = 'CORRECTED'
                    ORDER BY latest_correction.rowid DESC
                    LIMIT 1
                )
        """

    @staticmethod
    def _validate_claim_extraction_fields(
        fields: dict[str, JsonValue], source_text: str
    ) -> dict[str, JsonValue]:
        expected = {
            "span_start",
            "span_end",
            "subject",
            "predicate",
            "object",
            "claim_type",
            "attribution",
            "modality",
        }
        if set(fields) != expected:
            missing = ", ".join(sorted(expected - set(fields)))
            extra = ", ".join(sorted(set(fields) - expected))
            details = []
            if missing:
                details.append(f"missing: {missing}")
            if extra:
                details.append(f"unsupported: {extra}")
            raise StorageError(
                "Claim extraction fields are invalid (" + "; ".join(details) + ")."
            )
        start = fields["span_start"]
        end = fields["span_end"]
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or end > len(source_text)
        ):
            raise StorageError("Claim source span offsets are outside the source text.")
        if not source_text[start:end].strip():
            raise StorageError("Claim source span must contain non-whitespace text.")
        for field_name in (
            "subject",
            "predicate",
            "object",
            "claim_type",
            "modality",
        ):
            value = fields[field_name]
            if not isinstance(value, str) or not value.strip():
                raise StorageError(f"Claim extraction {field_name} must not be empty.")
        attribution = fields["attribution"]
        if attribution is not None and (
            not isinstance(attribution, str) or not attribution.strip()
        ):
            raise StorageError(
                "Claim extraction attribution must be a non-empty string or null."
            )
        return fields

    def create_claim_extractions(
        self,
        *,
        event_id: str,
        document_id: str,
        candidates: list[dict[str, JsonValue]],
        extraction_method: str,
        extractor: str,
        created_at: str,
    ) -> list[ClaimExtraction]:
        if not candidates:
            raise StorageError("At least one claim extraction candidate is required.")
        if not extraction_method.strip() or not extractor.strip():
            raise StorageError("Claim extraction method and extractor must be recorded.")
        _validate_timestamp(created_at, "created_at")
        candidate_ids = [uuid.uuid4().hex for _ in candidates]
        with self._lock, self._write_transaction() as connection:
            self._require_record(connection, "event", event_id)
            document = connection.execute(
                """
                SELECT source_id, original_text, text_sha256
                FROM normalized_documents WHERE document_id = ?
                """,
                (document_id,),
            ).fetchone()
            if document is None:
                raise StorageError(f"Normalized document {document_id} was not found.")
            source_text = document["original_text"]
            source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if source_digest != document["text_sha256"]:
                raise StorageError(
                    f"Normalized document {document_id} failed source-text integrity validation."
                )
            match = connection.execute(
                self._event_source_match_select()
                + """
                    WHERE match.event_id = ? AND match.document_id = ?
                """,
                (event_id, document_id),
            ).fetchone()
            if (
                match is None
                or self._event_source_match_from_row(match).status != "LINKED"
            ):
                raise StorageError(
                    "Claim extraction requires an explicitly confirmed event/source match."
                )
            for candidate_id, raw_fields in zip(candidate_ids, candidates):
                fields = self._validate_claim_extraction_fields(
                    raw_fields, source_text
                )
                span_start = fields["span_start"]
                span_end = fields["span_end"]
                connection.execute(
                    """
                    INSERT INTO claim_extractions (
                        candidate_id, event_id, document_id, source_id,
                        source_text_sha256, span_start, span_end, span_text,
                        subject, predicate, object, claim_type, attribution,
                        modality, extraction_method, extractor, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        event_id,
                        document_id,
                        document["source_id"],
                        source_digest,
                        span_start,
                        span_end,
                        source_text[span_start:span_end],
                        fields["subject"].strip(),
                        fields["predicate"].strip(),
                        fields["object"].strip(),
                        fields["claim_type"].strip(),
                        (
                            fields["attribution"].strip()
                            if isinstance(fields["attribution"], str)
                            else None
                        ),
                        fields["modality"].strip(),
                        extraction_method.strip(),
                        extractor.strip(),
                        created_at,
                    ),
                )
            rows = connection.execute(
                self._claim_extraction_select()
                + " WHERE candidate.candidate_id IN ("
                + ",".join("?" for _ in candidate_ids)
                + ") ORDER BY candidate.created_at, candidate.candidate_id",
                candidate_ids,
            ).fetchall()
        return [self._claim_extraction_from_row(row) for row in rows]

    def claim_extractions(
        self,
        *,
        event_id: str | None = None,
        document_id: str | None = None,
        review_status: str | None = None,
    ) -> list[ClaimExtraction]:
        allowed = {
            None,
            "PENDING",
            "ACCEPTED",
            "REJECTED",
            "CORRECTED",
            "UNRESOLVED",
        }
        if review_status not in allowed:
            raise ValueError("Unsupported claim extraction review status.")
        query = self._claim_extraction_select() + """
            WHERE (? IS NULL OR candidate.event_id = ?)
              AND (? IS NULL OR candidate.document_id = ?)
              AND (? IS NULL OR COALESCE(review.decision, 'PENDING') = ?)
            ORDER BY candidate.created_at, candidate.candidate_id
        """
        with self._connect() as connection:
            rows = connection.execute(
                query,
                (
                    event_id,
                    event_id,
                    document_id,
                    document_id,
                    review_status,
                    review_status,
                ),
            ).fetchall()
        return [self._claim_extraction_from_row(row) for row in rows]

    def claim_extraction_history(
        self, candidate_id: str
    ) -> list[ClaimExtraction]:
        with self._connect() as connection:
            candidate = connection.execute(
                """
                SELECT * FROM claim_extractions WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise StorageError(
                    f"Claim extraction candidate {candidate_id} was not found."
                )
            reviews = connection.execute(
                """
                SELECT decision, reviewed_by, reviewed_at, rationale,
                    correction_json
                FROM claim_extraction_reviews
                WHERE candidate_id = ?
                ORDER BY rowid
                """,
                (candidate_id,),
            ).fetchall()
        initial = self._claim_extraction_from_row(candidate)
        return [
            initial,
            *(
                ClaimExtraction(
                    **{
                        **{
                            field: getattr(initial, field)
                            for field in initial.__dataclass_fields__
                        },
                        "review_status": review["decision"],
                        "review_decision": review["decision"],
                        "reviewed_by": review["reviewed_by"],
                        "reviewed_at": review["reviewed_at"],
                        "review_rationale": review["rationale"],
                        "correction_payload": (
                            json.loads(review["correction_json"])
                            if review["correction_json"]
                            else None
                        ),
                    }
                )
                for review in reviews
            ),
        ]

    def review_claim_extraction(
        self,
        *,
        candidate_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
        correction: dict[str, JsonValue] | None = None,
    ) -> ClaimExtraction:
        decisions = {"ACCEPTED", "REJECTED", "CORRECTED", "UNRESOLVED"}
        if decision not in decisions:
            raise StorageError(f"Unsupported claim extraction decision: {decision}.")
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("Claim review requires a reviewer and rationale.")
        _validate_timestamp(reviewed_at, "reviewed_at")
        if (decision == "CORRECTED") != (correction is not None):
            raise StorageError(
                "A corrected review must include corrected claim fields; other "
                "decisions must not."
            )
        review_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            candidate = connection.execute(
                "SELECT * FROM claim_extractions WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise StorageError(
                    f"Claim extraction candidate {candidate_id} was not found."
                )
            source_document = connection.execute(
                """
                SELECT original_text, text_sha256
                FROM normalized_documents WHERE document_id = ?
                """,
                (candidate["document_id"],),
            ).fetchone()
            if (
                source_document is None
                or source_document["text_sha256"] != candidate["source_text_sha256"]
            ):
                raise StorageError(
                    f"Claim extraction {candidate_id} no longer matches its source text."
                )
            correction_json = None
            if correction is not None:
                validated_correction = self._validate_claim_extraction_fields(
                    correction, source_document["original_text"]
                )
                correction_json = _json_text(validated_correction)
            connection.execute(
                """
                INSERT INTO claim_extraction_reviews (
                    review_id, candidate_id, decision, reviewed_by, reviewed_at,
                    rationale, correction_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    candidate_id,
                    decision,
                    reviewed_by.strip(),
                    reviewed_at,
                    rationale.strip(),
                    correction_json,
                ),
            )
        candidates = self.claim_extractions()
        return next(
            candidate
            for candidate in candidates
            if candidate.candidate_id == candidate_id
        )

    def add_translation(
        self,
        *,
        document_id: str,
        translated_text: str,
        target_language: str,
        translation_method: str,
        translator: str,
        translated_at: str,
    ) -> TranslationRecord:
        if target_language not in {
            "pl", "de", "fr", "en", "fi", "et", "uk", "ro", "ka", "be", "ru"
        }:
            raise StorageError(f"Unsupported translation target language: {target_language}.")
        if not translated_text.strip():
            raise StorageError("Translated text must not be empty.")
        if not translation_method.strip() or not translator.strip():
            raise StorageError("Translation method and translator must be recorded.")
        if _RFC3339_TIMESTAMP.fullmatch(translated_at) is None:
            raise StorageError("translated_at must be an RFC3339 timestamp with a timezone.")
        try:
            parsed_at = datetime.fromisoformat(translated_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise StorageError("translated_at must be a valid RFC3339 timestamp.") from error
        if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
            raise StorageError("translated_at must include a timezone.")
        translation_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM normalized_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"Normalized document {document_id} was not found.")
            document = self._normalized_document_from_row(row)
            if document.language == "unknown":
                raise StorageError(
                    "Cannot attach translation provenance while source language is unknown."
                )
            if document.language == target_language:
                raise StorageError("Translation target language must differ from source language.")
            connection.execute(
                """
                INSERT INTO translations (
                    translation_id, document_id, source_text_sha256,
                    source_language, target_language, translated_text,
                    translation_method, translator, translated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    translation_id,
                    document.document_id,
                    document.text_sha256,
                    document.language,
                    target_language,
                    translated_text,
                    translation_method.strip(),
                    translator.strip(),
                    translated_at,
                ),
            )
            translation_row = connection.execute(
                "SELECT * FROM translations WHERE translation_id = ?",
                (translation_id,),
            ).fetchone()
        return self._translation_from_row(translation_row)

    @staticmethod
    def _translation_from_row(row: sqlite3.Row) -> TranslationRecord:
        return TranslationRecord(
            translation_id=row["translation_id"],
            document_id=row["document_id"],
            source_text_sha256=row["source_text_sha256"],
            source_language=row["source_language"],
            target_language=row["target_language"],
            translated_text=row["translated_text"],
            translation_method=row["translation_method"],
            translator=row["translator"],
            translated_at=row["translated_at"],
        )

    def translations(self, document_id: str) -> list[TranslationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM translations WHERE document_id = ?
                ORDER BY translated_at, translation_id
                """,
                (document_id,),
            ).fetchall()
        return [self._translation_from_row(row) for row in rows]

    def record_evidence(self, evidence: Evidence) -> int:
        if not isinstance(evidence, Evidence):
            raise StorageError("Only a validated Evidence record can be recorded.")
        return self._save(evidence, create_only=True)

    def evidence_records(
        self, *, event_id: str | None = None, source_id: str | None = None
    ) -> list[Evidence]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM canonical_records
                WHERE record_type = 'evidence'
                  AND (? IS NULL OR json_extract(payload_json, '$.event_id') = ?)
                  AND (? IS NULL OR json_extract(payload_json, '$.source_id') = ?)
                ORDER BY record_id
                """,
                (event_id, event_id, source_id, source_id),
            ).fetchall()
        return [
            parse_record("evidence", json.loads(row["payload_json"]))
            for row in rows
        ]

    @staticmethod
    def _evidence_note_from_row(row: sqlite3.Row) -> EvidenceNote:
        return EvidenceNote(
            note_id=row["note_id"],
            evidence_id=row["evidence_id"],
            analyst=row["analyst"],
            noted_at=row["noted_at"],
            note=row["note"],
            rationale=row["rationale"],
        )

    def add_evidence_note(
        self,
        *,
        evidence_id: str,
        analyst: str,
        noted_at: str,
        note: str,
        rationale: str,
    ) -> EvidenceNote:
        if not analyst.strip() or not note.strip() or not rationale.strip():
            raise StorageError("An evidence note needs an analyst, note and rationale.")
        _validate_timestamp(noted_at, "noted_at")
        note_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            self._require_record(connection, "evidence", evidence_id)
            connection.execute(
                """
                INSERT INTO evidence_notes (
                    note_id, evidence_id, analyst, noted_at, note, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    note_id,
                    evidence_id,
                    analyst.strip(),
                    noted_at,
                    note.strip(),
                    rationale.strip(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM evidence_notes WHERE note_id = ?", (note_id,)
            ).fetchone()
        return self._evidence_note_from_row(row)

    def evidence_notes(self, evidence_id: str) -> list[EvidenceNote]:
        with self._connect() as connection:
            self._require_record(connection, "evidence", evidence_id)
            rows = connection.execute(
                "SELECT * FROM evidence_notes WHERE evidence_id = ? ORDER BY rowid",
                (evidence_id,),
            ).fetchall()
        return [self._evidence_note_from_row(row) for row in rows]

    @staticmethod
    def _evidence_review_from_row(row: sqlite3.Row) -> EvidenceReview:
        return EvidenceReview(
            review_id=row["review_id"],
            evidence_id=row["evidence_id"],
            verification_status=row["verification_status"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            rationale=row["rationale"],
            analyst_note=row["analyst_note"],
        )

    def review_evidence(
        self,
        *,
        evidence_id: str,
        verification_status: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
        analyst_note: str | None = None,
    ) -> EvidenceReview:
        if verification_status not in {"IN_REVIEW", "VERIFIED", "REJECTED"}:
            raise StorageError(
                "Evidence review status must be IN_REVIEW, VERIFIED or REJECTED."
            )
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("Evidence review needs a reviewer and rationale.")
        if analyst_note is not None and not analyst_note.strip():
            raise StorageError("An evidence analyst note must not be empty.")
        _validate_timestamp(reviewed_at, "reviewed_at")
        review_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            self._require_record(connection, "evidence", evidence_id)
            connection.execute(
                """
                INSERT INTO evidence_verification_reviews (
                    review_id, evidence_id, verification_status, reviewed_by,
                    reviewed_at, rationale, analyst_note
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    evidence_id,
                    verification_status,
                    reviewed_by.strip(),
                    reviewed_at,
                    rationale.strip(),
                    analyst_note.strip() if analyst_note is not None else None,
                ),
            )
            row = connection.execute(
                "SELECT * FROM evidence_verification_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        return self._evidence_review_from_row(row)

    def evidence_review_history(self, evidence_id: str) -> list[EvidenceReview]:
        with self._connect() as connection:
            self._require_record(connection, "evidence", evidence_id)
            rows = connection.execute(
                """
                SELECT * FROM evidence_verification_reviews
                WHERE evidence_id = ? ORDER BY rowid
                """,
                (evidence_id,),
            ).fetchall()
        return [self._evidence_review_from_row(row) for row in rows]

    def evidence_verification_status(self, evidence_id: str) -> str:
        evidence = self.get("evidence", evidence_id)
        assert isinstance(evidence, Evidence)
        history = self.evidence_review_history(evidence_id)
        return history[-1].verification_status if history else evidence.verification_status

    @staticmethod
    def _claim_evidence_link_select() -> str:
        return """
            SELECT link.*,
                review.decision AS review_status,
                review.reviewed_by,
                review.reviewed_at,
                review.rationale AS review_rationale
            FROM claim_evidence_links AS link
            LEFT JOIN claim_evidence_link_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM claim_evidence_link_reviews AS latest
                    WHERE latest.link_id = link.link_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
        """

    @staticmethod
    def _claim_evidence_link_from_row(row: sqlite3.Row) -> ClaimEvidenceLink:
        return ClaimEvidenceLink(
            link_id=row["link_id"],
            event_id=row["event_id"],
            claim_id=row["claim_id"],
            evidence_id=row["evidence_id"],
            relationship=row["relationship"],
            proposed_by=row["proposed_by"],
            proposed_at=row["proposed_at"],
            rationale=row["rationale"],
            review_status=row["review_status"] or "PENDING",
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_rationale=row["review_rationale"],
        )

    def propose_claim_evidence_link(
        self,
        *,
        claim_id: str,
        evidence_id: str,
        relationship: str,
        proposed_by: str,
        proposed_at: str,
        rationale: str,
    ) -> ClaimEvidenceLink:
        if relationship not in {"SUPPORTS", "CONTRADICTS", "NOT_DIRECTLY_RELEVANT"}:
            raise StorageError("Unsupported claim/evidence relationship.")
        if not proposed_by.strip() or not rationale.strip():
            raise StorageError("A claim/evidence link needs an analyst and rationale.")
        _validate_timestamp(proposed_at, "proposed_at")
        link_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            claim = self._require_record(connection, "claim", claim_id)
            evidence = self._require_record(connection, "evidence", evidence_id)
            if claim["event_id"] != evidence["event_id"]:
                raise StorageError(
                    "Claim/evidence links must refer to records from the same event."
                )
            active = connection.execute(
                """
                SELECT link.link_id
                FROM claim_evidence_links AS link
                LEFT JOIN claim_evidence_link_reviews AS review
                    ON review.review_id = (
                        SELECT latest.review_id
                        FROM claim_evidence_link_reviews AS latest
                        WHERE latest.link_id = link.link_id
                        ORDER BY latest.rowid DESC LIMIT 1
                    )
                WHERE link.claim_id = ? AND link.evidence_id = ?
                  AND COALESCE(review.decision, 'PENDING')
                      IN ('PENDING', 'LINKED')
                LIMIT 1
                """,
                (claim_id, evidence_id),
            ).fetchone()
            if active is not None:
                raise StorageError(
                    "Evidence already has a pending or linked relationship to this claim; "
                    "resolve it before proposing another."
                )
            connection.execute(
                """
                INSERT INTO claim_evidence_links (
                    link_id, event_id, claim_id, evidence_id, relationship,
                    proposed_by, proposed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    claim["event_id"],
                    claim_id,
                    evidence_id,
                    relationship,
                    proposed_by.strip(),
                    proposed_at,
                    rationale.strip(),
                ),
            )
        return self.claim_evidence_links(link_id=link_id)[0]

    def claim_evidence_links(
        self,
        *,
        event_id: str | None = None,
        claim_id: str | None = None,
        evidence_id: str | None = None,
        review_status: str | None = None,
        link_id: str | None = None,
    ) -> list[ClaimEvidenceLink]:
        statuses = {None, "PENDING", "LINKED", "REJECTED", "UNRESOLVED"}
        if review_status not in statuses:
            raise ValueError("Unsupported claim/evidence link review status.")
        query = self._claim_evidence_link_select() + """
            WHERE (? IS NULL OR link.event_id = ?)
              AND (? IS NULL OR link.claim_id = ?)
              AND (? IS NULL OR link.evidence_id = ?)
              AND (? IS NULL OR link.link_id = ?)
              AND (? IS NULL OR COALESCE(review.decision, 'PENDING') = ?)
            ORDER BY link.proposed_at, link.link_id
        """
        parameters = (
            event_id, event_id, claim_id, claim_id, evidence_id, evidence_id,
            link_id, link_id, review_status, review_status,
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._claim_evidence_link_from_row(row) for row in rows]

    def review_claim_evidence_link(
        self,
        *,
        link_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
    ) -> ClaimEvidenceLink:
        if decision not in {"LINKED", "REJECTED", "UNRESOLVED"}:
            raise StorageError("Unsupported claim/evidence link review decision.")
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("A claim/evidence link review needs reviewer and rationale.")
        _validate_timestamp(reviewed_at, "reviewed_at")
        review_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            link = connection.execute(
                "SELECT * FROM claim_evidence_links WHERE link_id = ?", (link_id,)
            ).fetchone()
            if link is None:
                raise StorageError(f"Claim/evidence link {link_id} was not found.")
            if decision == "LINKED":
                active = connection.execute(
                    """
                    SELECT link.link_id
                    FROM claim_evidence_links AS link
                    JOIN claim_evidence_link_reviews AS review
                      ON review.review_id = (
                          SELECT latest.review_id
                          FROM claim_evidence_link_reviews AS latest
                          WHERE latest.link_id = link.link_id
                          ORDER BY latest.rowid DESC LIMIT 1
                      )
                    WHERE link.claim_id = ? AND link.evidence_id = ?
                      AND link.link_id != ? AND review.decision = 'LINKED'
                    LIMIT 1
                    """,
                    (link["claim_id"], link["evidence_id"], link_id),
                ).fetchone()
                if active is not None:
                    raise StorageError(
                        "Evidence already has a linked relationship to this claim."
                    )
            connection.execute(
                """
                INSERT INTO claim_evidence_link_reviews (
                    review_id, link_id, decision, reviewed_by, reviewed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    link_id,
                    decision,
                    reviewed_by.strip(),
                    reviewed_at,
                    rationale.strip(),
                ),
            )
        return self.claim_evidence_links(link_id=link_id)[0]

    def claim_evidence_link_history(
        self, link_id: str
    ) -> list[ClaimEvidenceLink]:
        with self._connect() as connection:
            link = connection.execute(
                "SELECT * FROM claim_evidence_links WHERE link_id = ?", (link_id,)
            ).fetchone()
            if link is None:
                raise StorageError(f"Claim/evidence link {link_id} was not found.")
            reviews = connection.execute(
                "SELECT * FROM claim_evidence_link_reviews WHERE link_id = ? ORDER BY rowid",
                (link_id,),
            ).fetchall()
        initial = self._claim_evidence_link_from_row(
            {**dict(link), "review_status": "PENDING", "reviewed_by": None,
             "reviewed_at": None, "review_rationale": None}
        )
        return [
            initial,
            *(
                replace(
                    initial,
                    review_status=review["decision"],
                    reviewed_by=review["reviewed_by"],
                    reviewed_at=review["reviewed_at"],
                    review_rationale=review["rationale"],
                )
                for review in reviews
            ),
        ]

    @staticmethod
    def _evidence_gap_select() -> str:
        return """
            SELECT gap.*,
                review.decision AS review_decision,
                review.reviewed_by,
                review.reviewed_at,
                review.rationale AS review_rationale,
                CASE
                    WHEN review.decision = 'REOPENED' THEN 'OPEN'
                    ELSE COALESCE(review.decision, 'OPEN')
                END AS status
            FROM evidence_gaps AS gap
            LEFT JOIN evidence_gap_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM evidence_gap_reviews AS latest
                    WHERE latest.gap_id = gap.gap_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
        """

    @staticmethod
    def _evidence_gap_from_row(row: sqlite3.Row) -> EvidenceGap:
        return EvidenceGap(
            gap_id=row["gap_id"],
            event_id=row["event_id"],
            claim_id=row["claim_id"],
            gap_type=row["gap_type"],
            description=row["description"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            rationale=row["rationale"],
            supporting_link_id=row["supporting_link_id"],
            contradicting_link_id=row["contradicting_link_id"],
            status=row["status"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_rationale=row["review_rationale"],
        )

    def record_evidence_gap(
        self,
        *,
        event_id: str,
        gap_type: str,
        description: str,
        created_by: str,
        created_at: str,
        rationale: str,
        claim_id: str | None = None,
        supporting_link_id: str | None = None,
        contradicting_link_id: str | None = None,
    ) -> EvidenceGap:
        if gap_type not in {"MISSING", "INSUFFICIENT", "CONFLICTING"}:
            raise StorageError("Evidence gap type must be MISSING, INSUFFICIENT or CONFLICTING.")
        if not description.strip() or not created_by.strip() or not rationale.strip():
            raise StorageError("An evidence gap needs a description, analyst and rationale.")
        _validate_timestamp(created_at, "created_at")
        if gap_type == "CONFLICTING" and (
            claim_id is None or supporting_link_id is None or contradicting_link_id is None
        ):
            raise StorageError(
                "A conflicting-evidence gap needs a claim and confirmed support/contradiction links."
            )
        if gap_type != "CONFLICTING" and (
            supporting_link_id is not None or contradicting_link_id is not None
        ):
            raise StorageError("Only a conflicting-evidence gap accepts relation links.")
        gap_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            self._require_record(connection, "event", event_id)
            if claim_id is not None:
                claim = self._require_record(connection, "claim", claim_id)
                if claim["event_id"] != event_id:
                    raise StorageError("Evidence gaps and claims must belong to the same event.")
            if gap_type == "CONFLICTING":
                links = []
                for link_id, expected in (
                    (supporting_link_id, "SUPPORTS"),
                    (contradicting_link_id, "CONTRADICTS"),
                ):
                    row = connection.execute(
                        self._claim_evidence_link_select()
                        + " WHERE link.link_id = ?",
                        (link_id,),
                    ).fetchone()
                    if (
                        row is None
                        or row["event_id"] != event_id
                        or row["claim_id"] != claim_id
                        or row["relationship"] != expected
                        or row["review_status"] != "LINKED"
                    ):
                        raise StorageError(
                            "A conflicting-evidence gap requires confirmed SUPPORTS "
                            "and CONTRADICTS links for the same claim."
                        )
                    links.append(row["link_id"])
                if links[0] == links[1]:
                    raise StorageError("Conflict support and contradiction must be distinct links.")
            connection.execute(
                """
                INSERT INTO evidence_gaps (
                    gap_id, event_id, claim_id, gap_type, description,
                    created_by, created_at, rationale, supporting_link_id,
                    contradicting_link_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gap_id, event_id, claim_id, gap_type, description.strip(),
                    created_by.strip(), created_at, rationale.strip(),
                    supporting_link_id, contradicting_link_id,
                ),
            )
        return self.evidence_gaps(gap_id=gap_id)[0]

    def evidence_gaps(
        self,
        *,
        event_id: str | None = None,
        claim_id: str | None = None,
        gap_type: str | None = None,
        status: str | None = None,
        gap_id: str | None = None,
    ) -> list[EvidenceGap]:
        if gap_type not in {None, "MISSING", "INSUFFICIENT", "CONFLICTING"}:
            raise ValueError("Unsupported evidence gap type.")
        if status not in {None, "OPEN", "RESOLVED", "DISMISSED"}:
            raise ValueError("Unsupported evidence gap status.")
        with self._connect() as connection:
            rows = connection.execute(
                self._evidence_gap_select()
                + """
                    WHERE (? IS NULL OR gap.event_id = ?)
                      AND (? IS NULL OR gap.claim_id = ?)
                      AND (? IS NULL OR gap.gap_type = ?)
                      AND (? IS NULL OR status = ?)
                      AND (? IS NULL OR gap.gap_id = ?)
                    ORDER BY gap.created_at, gap.gap_id
                """,
                (
                    event_id, event_id, claim_id, claim_id, gap_type, gap_type,
                    status, status, gap_id, gap_id,
                ),
            ).fetchall()
        return [self._evidence_gap_from_row(row) for row in rows]

    def review_evidence_gap(
        self,
        *,
        gap_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
    ) -> EvidenceGap:
        if decision not in {"RESOLVED", "DISMISSED", "REOPENED"}:
            raise StorageError("Evidence gap decision must be RESOLVED, DISMISSED or REOPENED.")
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("An evidence gap review needs a reviewer and rationale.")
        _validate_timestamp(reviewed_at, "reviewed_at")
        review_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM evidence_gaps WHERE gap_id = ?", (gap_id,)
            ).fetchone()
            if exists is None:
                raise StorageError(f"Evidence gap {gap_id} was not found.")
            connection.execute(
                """
                INSERT INTO evidence_gap_reviews (
                    review_id, gap_id, decision, reviewed_by, reviewed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (review_id, gap_id, decision, reviewed_by.strip(), reviewed_at, rationale.strip()),
            )
        return self.evidence_gaps(gap_id=gap_id)[0]

    def evidence_gap_history(self, gap_id: str) -> list[EvidenceGap]:
        with self._connect() as connection:
            gap = connection.execute(
                "SELECT * FROM evidence_gaps WHERE gap_id = ?", (gap_id,)
            ).fetchone()
            if gap is None:
                raise StorageError(f"Evidence gap {gap_id} was not found.")
            reviews = connection.execute(
                "SELECT * FROM evidence_gap_reviews WHERE gap_id = ? ORDER BY rowid",
                (gap_id,),
            ).fetchall()
        initial = self._evidence_gap_from_row(
            {**dict(gap), "status": "OPEN", "reviewed_by": None,
             "reviewed_at": None, "review_rationale": None}
        )
        result = [initial]
        for review in reviews:
            status = "OPEN" if review["decision"] == "REOPENED" else review["decision"]
            result.append(
                replace(
                    initial,
                    status=status,
                    reviewed_by=review["reviewed_by"],
                    reviewed_at=review["reviewed_at"],
                    review_rationale=review["rationale"],
                )
            )
        return result

    @staticmethod
    def _country_coverage_plan_from_row(row: sqlite3.Row) -> CountryCoveragePlan:
        return CountryCoveragePlan(
            plan_id=row["plan_id"],
            event_id=row["event_id"],
            revision=row["revision"],
            config_sha256=row["config_sha256"],
            countries=tuple(json.loads(row["countries_json"])),
            outside_catalog=tuple(json.loads(row["outside_catalog_json"])),
            proposed_by=row["proposed_by"],
            proposed_at=row["proposed_at"],
            rationale=row["rationale"],
            status=row["status"] or "PENDING",
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_rationale=row["review_rationale"],
        )

    @staticmethod
    def _country_coverage_plan_select() -> str:
        return """
            SELECT plan.*,
                review.decision AS status,
                review.reviewed_by,
                review.reviewed_at,
                review.rationale AS review_rationale
            FROM event_country_coverage_plans AS plan
            LEFT JOIN event_country_coverage_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM event_country_coverage_reviews AS latest
                    WHERE latest.plan_id = plan.plan_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
        """

    def create_country_coverage_plan(
        self,
        *,
        event_id: str,
        config_sha256: str,
        countries: list[dict[str, JsonValue]],
        outside_catalog: list[dict[str, JsonValue]],
        proposed_by: str,
        proposed_at: str,
        rationale: str,
    ) -> CountryCoveragePlan:
        if not re.fullmatch(r"[0-9a-f]{64}", config_sha256):
            raise StorageError("Country coverage configuration digest must be SHA-256.")
        if not countries:
            raise StorageError("An event coverage plan must select at least one country.")
        if not proposed_by.strip() or not rationale.strip():
            raise StorageError("A country coverage proposal needs an analyst and rationale.")
        _validate_timestamp(proposed_at, "proposed_at")
        codes: set[str] = set()
        for country in countries:
            code = country.get("code")
            languages = country.get("languages")
            if (
                not isinstance(code, str)
                or not re.fullmatch(r"[A-Z]{2}", code)
                or code in codes
                or not isinstance(languages, list)
                or not languages
                or any(not isinstance(language, str) or not language.strip()
                       for language in languages)
                or len(languages) != len(set(languages))
            ):
                raise StorageError("Coverage countries need unique codes and language lists.")
            codes.add(code)
        outside_countries: set[str] = set()
        for item in outside_catalog:
            country = item.get("country")
            reason = item.get("reason")
            if (
                not isinstance(country, str)
                or not country.strip()
                or not isinstance(reason, str)
                or not reason.strip()
            ):
                raise StorageError(
                    "Out-of-catalog coverage needs an unselected country and a rationale."
                )
            normalized_country = country.strip()
            if normalized_country in codes or normalized_country in outside_countries:
                raise StorageError(
                    "Out-of-catalog countries must be unselected and unique."
                )
            outside_countries.add(normalized_country)
        plan_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            self._require_record(connection, "event", event_id)
            pending = connection.execute(
                self._country_coverage_plan_select()
                + """
                    WHERE plan.event_id = ? AND COALESCE(review.decision, 'PENDING')
                        = 'PENDING'
                """,
                (event_id,),
            ).fetchone()
            if pending is not None:
                raise StorageError(
                    f"Event {event_id} already has a pending country coverage plan."
                )
            revision = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) + 1 AS revision
                FROM event_country_coverage_plans WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()["revision"]
            connection.execute(
                """
                INSERT INTO event_country_coverage_plans (
                    plan_id, event_id, revision, config_sha256, countries_json,
                    outside_catalog_json, proposed_by, proposed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id, event_id, revision, config_sha256,
                    _json_array_text(countries),
                    _json_array_text(outside_catalog),
                    proposed_by.strip(), proposed_at, rationale.strip(),
                ),
            )
        return self.country_coverage_plans(plan_id=plan_id)[0]

    def country_coverage_plans(
        self, *, event_id: str | None = None, plan_id: str | None = None
    ) -> list[CountryCoveragePlan]:
        with self._connect() as connection:
            rows = connection.execute(
                self._country_coverage_plan_select()
                + """
                    WHERE (? IS NULL OR plan.event_id = ?)
                      AND (? IS NULL OR plan.plan_id = ?)
                    ORDER BY plan.event_id, plan.revision
                """,
                (event_id, event_id, plan_id, plan_id),
            ).fetchall()
        return [self._country_coverage_plan_from_row(row) for row in rows]

    def review_country_coverage_plan(
        self,
        *,
        plan_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
    ) -> CountryCoveragePlan:
        if decision not in {"ACCEPTED", "REJECTED", "UNRESOLVED", "SUPERSEDED"}:
            raise StorageError("Unsupported country coverage plan review decision.")
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("Country coverage review needs a reviewer and rationale.")
        _validate_timestamp(reviewed_at, "reviewed_at")
        review_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            plan = connection.execute(
                "SELECT event_id FROM event_country_coverage_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if plan is None:
                raise StorageError(f"Country coverage plan {plan_id} was not found.")
            if decision == "SUPERSEDED":
                current = connection.execute(
                    self._country_coverage_plan_select()
                    + """
                        WHERE plan.event_id = ? AND plan.plan_id != ?
                          AND review.decision = 'PENDING'
                        ORDER BY plan.revision DESC LIMIT 1
                    """,
                    (plan["event_id"], plan_id),
                ).fetchone()
                if current is None:
                    raise StorageError(
                        "A coverage plan can be superseded only when its replacement "
                        "has a pending proposal."
                    )
            if decision == "ACCEPTED":
                active = connection.execute(
                    self._country_coverage_plan_select()
                    + """
                        WHERE plan.event_id = ? AND plan.plan_id != ?
                          AND review.decision = 'ACCEPTED'
                        ORDER BY plan.revision DESC LIMIT 1
                    """,
                    (plan["event_id"], plan_id),
                ).fetchone()
                if active is not None:
                    connection.execute(
                        """
                        INSERT INTO event_country_coverage_reviews (
                            review_id, plan_id, decision, reviewed_by, reviewed_at, rationale
                        ) VALUES (?, ?, 'SUPERSEDED', ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            active["plan_id"],
                            reviewed_by.strip(),
                            reviewed_at,
                            f"Superseded by accepted plan {plan_id}: {rationale.strip()}",
                        ),
                    )
            connection.execute(
                """
                INSERT INTO event_country_coverage_reviews (
                    review_id, plan_id, decision, reviewed_by, reviewed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id, plan_id, decision, reviewed_by.strip(),
                    reviewed_at, rationale.strip(),
                ),
            )
        return self.country_coverage_plans(plan_id=plan_id)[0]

    @staticmethod
    def _country_matrix_assessment_select() -> str:
        return """
            SELECT assessment.*,
                review.decision AS status,
                review.reviewed_by,
                review.reviewed_at,
                review.rationale AS review_rationale
            FROM country_matrix_assessments AS assessment
            LEFT JOIN country_matrix_assessment_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM country_matrix_assessment_reviews AS latest
                    WHERE latest.assessment_id = assessment.assessment_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
        """

    @staticmethod
    def _country_matrix_assessment_from_row(
        row: sqlite3.Row,
    ) -> CountryMatrixAssessment:
        return CountryMatrixAssessment(
            assessment_id=row["assessment_id"],
            plan_id=row["plan_id"],
            event_id=row["event_id"],
            country_code=row["country_code"],
            dominant_frame=row["dominant_frame"],
            attribution_summary=row["attribution_summary"],
            occurrence_confidence=row["occurrence_confidence"],
            method_confidence=row["method_confidence"],
            attribution_confidence=row["attribution_confidence"],
            omissions=row["omissions"],
            contradictions=row["contradictions"],
            supporting_source_ids=tuple(json.loads(row["supporting_source_ids_json"])),
            supporting_claim_ids=tuple(json.loads(row["supporting_claim_ids_json"])),
            supporting_evidence_ids=tuple(json.loads(row["supporting_evidence_ids_json"])),
            analyst=row["analyst"],
            created_at=row["created_at"],
            rationale=row["rationale"],
            status=row["status"] or "PENDING",
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_rationale=row["review_rationale"],
        )

    def _matrix_country_source_context(
        self, connection: sqlite3.Connection, event_id: str, country_code: str
    ) -> tuple[set[str], set[str]]:
        rows = connection.execute(
            """
            SELECT document.document_id, document.source_id,
                   collection.source_metadata_json
            FROM event_source_matches AS match
            JOIN event_source_match_reviews AS match_review
              ON match_review.review_id = (
                  SELECT latest.review_id
                  FROM event_source_match_reviews AS latest
                  WHERE latest.match_id = match.match_id
                  ORDER BY latest.rowid DESC LIMIT 1
              )
            JOIN normalized_documents AS document
              ON document.document_id = match.document_id
            JOIN collection_attempts AS collection
              ON collection.collection_id = document.collection_id
            WHERE match.event_id = ? AND match_review.decision = 'LINKED'
            """,
            (event_id,),
        ).fetchall()
        contexts: dict[str, dict[str, object]] = {}
        conflicting_sources: set[str] = set()
        source_documents: dict[str, set[str]] = {}
        for row in rows:
            metadata = json.loads(row["source_metadata_json"])
            context = {
                "country": metadata.get("country"),
                "source_class": metadata.get("source_class"),
            }
            source_id = row["source_id"]
            previous = contexts.setdefault(source_id, context)
            if previous != context:
                conflicting_sources.add(source_id)
            source_documents.setdefault(source_id, set()).add(row["document_id"])
        sources = {
            source_id
            for source_id, context in contexts.items()
            if source_id not in conflicting_sources
            and context.get("country") == country_code
        }
        documents = set().union(
            *(source_documents[source_id] for source_id in sources)
        ) if sources else set()
        return sources, documents

    def record_country_matrix_assessment(
        self,
        *,
        plan_id: str,
        country_code: str,
        config_sha256: str,
        dominant_frame: str,
        attribution_summary: str,
        occurrence_confidence: str,
        method_confidence: str,
        attribution_confidence: str,
        omissions: str,
        contradictions: str,
        supporting_source_ids: list[str],
        supporting_claim_ids: list[str],
        supporting_evidence_ids: list[str],
        analyst: str,
        created_at: str,
        rationale: str,
    ) -> CountryMatrixAssessment:
        confidence_values = {"LOW", "MEDIUM", "HIGH", "UNKNOWN"}
        if any(
            value not in confidence_values
            for value in (
                occurrence_confidence, method_confidence, attribution_confidence
            )
        ):
            raise StorageError("Each confidence dimension must be LOW, MEDIUM, HIGH or UNKNOWN.")
        text_values = (
            dominant_frame, attribution_summary, omissions, contradictions,
            analyst, rationale,
        )
        if any(not value.strip() for value in text_values):
            raise StorageError("Matrix assessment fields and rationale must not be empty.")
        if len(supporting_source_ids) != len(set(supporting_source_ids)):
            raise StorageError("Matrix assessment source references must not contain duplicates.")
        if len(supporting_claim_ids) != len(set(supporting_claim_ids)) or len(
            supporting_evidence_ids
        ) != len(set(supporting_evidence_ids)):
            raise StorageError("Matrix assessment references must not contain duplicates.")
        _validate_timestamp(created_at, "created_at")
        assessment_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            plan = connection.execute(
                """
                SELECT plan.event_id, plan.config_sha256,
                       review.decision AS status
                FROM event_country_coverage_plans AS plan
                LEFT JOIN event_country_coverage_reviews AS review
                  ON review.review_id = (
                      SELECT latest.review_id
                      FROM event_country_coverage_reviews AS latest
                      WHERE latest.plan_id = plan.plan_id
                      ORDER BY latest.rowid DESC LIMIT 1
                  )
                WHERE plan.plan_id = ?
                """,
                (plan_id,),
            ).fetchone()
            if plan is None:
                raise StorageError(f"Country coverage plan {plan_id} was not found.")
            if plan["status"] != "ACCEPTED":
                raise StorageError("Country assessments require an accepted coverage plan.")
            if plan["config_sha256"] != config_sha256:
                raise StorageError(
                    "The country/language configuration changed; propose and review a new coverage plan."
                )
            countries_row = connection.execute(
                "SELECT countries_json FROM event_country_coverage_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            countries = json.loads(countries_row["countries_json"])
            if not any(country["code"] == country_code for country in countries):
                raise StorageError(
                    f"Country {country_code} is not selected in plan {plan_id}."
                )
            pending_assessment = connection.execute(
                self._country_matrix_assessment_select()
                + """
                    WHERE assessment.plan_id = ? AND assessment.country_code = ?
                      AND COALESCE(review.decision, 'PENDING') = 'PENDING'
                    LIMIT 1
                """,
                (plan_id, country_code),
            ).fetchone()
            if pending_assessment is not None:
                raise StorageError(
                    f"Country {country_code} already has a pending matrix assessment."
                )
            country_sources, _ = self._matrix_country_source_context(
                connection, plan["event_id"], country_code
            )
            if not set(supporting_source_ids).issubset(country_sources):
                raise StorageError(
                    "Matrix assessment sources must be linked to this event and country."
                )
            for claim_id in supporting_claim_ids:
                claim = self._require_record(connection, "claim", claim_id)
                if (
                    claim["event_id"] != plan["event_id"]
                    or claim["source_id"] not in country_sources
                ):
                    raise StorageError(
                        f"Claim {claim_id} is not linked to this event and country."
                    )
            country_evidence_ids = {
                row["record_id"]
                for row in connection.execute(
                    "SELECT record_id, payload_json FROM canonical_records "
                    "WHERE record_type = 'evidence'"
                ).fetchall()
                if json.loads(row["payload_json"])["event_id"] == plan["event_id"]
                and json.loads(row["payload_json"])["source_id"] in country_sources
            }
            if not set(supporting_evidence_ids).issubset(country_evidence_ids):
                raise StorageError(
                    "Matrix assessment evidence must be linked to this event and country."
                )
            connection.execute(
                """
                INSERT INTO country_matrix_assessments (
                    assessment_id, plan_id, event_id, country_code,
                    dominant_frame, attribution_summary, occurrence_confidence,
                    method_confidence, attribution_confidence, omissions,
                    contradictions, supporting_source_ids_json,
                    supporting_claim_ids_json, supporting_evidence_ids_json,
                    analyst, created_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment_id, plan_id, plan["event_id"], country_code,
                    dominant_frame.strip(), attribution_summary.strip(),
                    occurrence_confidence, method_confidence, attribution_confidence,
                    omissions.strip(), contradictions.strip(),
                    _json_array_text(supporting_source_ids),
                    _json_array_text(supporting_claim_ids),
                    _json_array_text(supporting_evidence_ids),
                    analyst.strip(), created_at, rationale.strip(),
                ),
            )
        return self.country_matrix_assessments(assessment_id=assessment_id)[0]

    def country_matrix_assessments(
        self,
        *,
        plan_id: str | None = None,
        country_code: str | None = None,
        assessment_id: str | None = None,
    ) -> list[CountryMatrixAssessment]:
        with self._connect() as connection:
            rows = connection.execute(
                self._country_matrix_assessment_select()
                + """
                    WHERE (? IS NULL OR assessment.plan_id = ?)
                      AND (? IS NULL OR assessment.country_code = ?)
                      AND (? IS NULL OR assessment.assessment_id = ?)
                    ORDER BY assessment.created_at, assessment.assessment_id
                """,
                (
                    plan_id, plan_id, country_code, country_code,
                    assessment_id, assessment_id,
                ),
            ).fetchall()
        return [self._country_matrix_assessment_from_row(row) for row in rows]

    def review_country_matrix_assessment(
        self,
        *,
        assessment_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
    ) -> CountryMatrixAssessment:
        if decision not in {"ACCEPTED", "REJECTED", "UNRESOLVED"}:
            raise StorageError("Unsupported country matrix assessment decision.")
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("Matrix assessment review needs a reviewer and rationale.")
        _validate_timestamp(reviewed_at, "reviewed_at")
        review_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            assessment = connection.execute(
                """
                SELECT plan_id, country_code FROM country_matrix_assessments
                WHERE assessment_id = ?
                """,
                (assessment_id,),
            ).fetchone()
            if assessment is None:
                raise StorageError(f"Matrix assessment {assessment_id} was not found.")
            if decision == "ACCEPTED":
                active = connection.execute(
                    self._country_matrix_assessment_select()
                    + """
                        WHERE assessment.plan_id = ? AND assessment.country_code = ?
                          AND assessment.assessment_id != ?
                          AND review.decision = 'ACCEPTED'
                        LIMIT 1
                    """,
                    (assessment["plan_id"], assessment["country_code"], assessment_id),
                ).fetchone()
                if active is not None:
                    connection.execute(
                        """
                        INSERT INTO country_matrix_assessment_reviews (
                            review_id, assessment_id, decision, reviewed_by,
                            reviewed_at, rationale
                        ) VALUES (?, ?, 'UNRESOLVED', ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            active["assessment_id"],
                            reviewed_by.strip(),
                            reviewed_at,
                            f"Superseded by assessment {assessment_id}: {rationale.strip()}",
                        ),
                    )
            connection.execute(
                """
                INSERT INTO country_matrix_assessment_reviews (
                    review_id, assessment_id, decision, reviewed_by,
                    reviewed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id, assessment_id, decision,
                    reviewed_by.strip(), reviewed_at, rationale.strip(),
                ),
            )
        return self.country_matrix_assessments(assessment_id=assessment_id)[0]

    def national_information_matrix(
        self, *, event_id: str, config_sha256: str
    ) -> NationalInformationMatrix:
        with self._connect() as connection:
            self._require_record(connection, "event", event_id)
            active = connection.execute(
                self._country_coverage_plan_select()
                + """
                    WHERE plan.event_id = ? AND review.decision = 'ACCEPTED'
                    ORDER BY plan.revision DESC LIMIT 1
                """,
                (event_id,),
            ).fetchone()
            if active is None:
                plans = connection.execute(
                    self._country_coverage_plan_select()
                    + """
                        WHERE plan.event_id = ?
                        ORDER BY plan.revision DESC LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
                if plans is None:
                    raise StorageError(f"Event {event_id} has no country coverage plan.")
                active = plans
            plan = self._country_coverage_plan_from_row(active)
            if plan.status != "ACCEPTED":
                return NationalInformationMatrix(
                    event_id=event_id,
                    plan=plan,
                    config_matches=plan.config_sha256 == config_sha256,
                    cells=(),
                )
            matches = connection.execute(
                """
                SELECT document.document_id, document.source_id,
                       document.language, document.language_review_status,
                       collection.source_metadata_json
                FROM event_source_matches AS match
                JOIN event_source_match_reviews AS match_review
                  ON match_review.review_id = (
                      SELECT latest.review_id
                      FROM event_source_match_reviews AS latest
                      WHERE latest.match_id = match.match_id
                      ORDER BY latest.rowid DESC LIMIT 1
                  )
                JOIN normalized_documents AS document
                  ON document.document_id = match.document_id
                JOIN collection_attempts AS collection
                  ON collection.collection_id = document.collection_id
                WHERE match.event_id = ? AND match_review.decision = 'LINKED'
                """,
                (event_id,),
            ).fetchall()
            evidence_rows = connection.execute(
                """
                SELECT evidence.record_id, evidence.payload_json,
                    COALESCE(
                        (
                            SELECT review.verification_status
                            FROM evidence_verification_reviews AS review
                            WHERE review.evidence_id = evidence.record_id
                            ORDER BY review.rowid DESC LIMIT 1
                        ),
                        json_extract(evidence.payload_json, '$.verification_status')
                    ) AS current_verification_status
                FROM canonical_records AS evidence
                WHERE evidence.record_type = 'evidence'
                """
            ).fetchall()
            claim_rows = connection.execute(
                "SELECT record_id, payload_json FROM canonical_records "
                "WHERE record_type = 'claim'"
            ).fetchall()
            latest_relations = connection.execute(
                """
                SELECT link.claim_id, link.relationship
                FROM claim_evidence_links AS link
                JOIN claim_evidence_link_reviews AS review
                  ON review.review_id = (
                      SELECT latest.review_id
                      FROM claim_evidence_link_reviews AS latest
                      WHERE latest.link_id = link.link_id
                      ORDER BY latest.rowid DESC LIMIT 1
                  )
                WHERE link.event_id = ? AND review.decision = 'LINKED'
                """,
                (event_id,),
            ).fetchall()
            assignment_rows = connection.execute(
                self._independence_assignment_select()
                + """
                    WHERE review.decision = 'ACCEPTED'
                      AND assignment.rowid = (
                          SELECT MAX(current.rowid)
                          FROM independence_assignments AS current
                          WHERE current.member_type = assignment.member_type
                            AND current.member_id = assignment.member_id
                      )
                """
            ).fetchall()
            assessment_rows = connection.execute(
                self._country_matrix_assessment_select()
                + """
                    WHERE assessment.plan_id = ?
                    ORDER BY CASE review.decision
                        WHEN 'ACCEPTED' THEN 0
                        WHEN 'PENDING' THEN 1
                        ELSE 2
                    END,
                    assessment.created_at DESC, assessment.rowid DESC
                """,
                (plan.plan_id,),
            ).fetchall()

        source_context: dict[str, dict[str, object]] = {}
        conflicting_sources: set[str] = set()
        conflicting_countries: dict[str, set[str]] = {}
        documents_by_country: dict[str, set[str]] = {}
        languages_by_country: dict[str, set[str]] = {}
        for row in matches:
            metadata = json.loads(row["source_metadata_json"])
            country = metadata.get("country")
            if not isinstance(country, str):
                continue
            context = {
                "country": country,
                "source_class": metadata.get("source_class"),
            }
            previous_context = source_context.get(row["source_id"])
            if previous_context is None:
                source_context[row["source_id"]] = context
            elif previous_context != context:
                conflicting_sources.add(row["source_id"])
                for affected_country in (previous_context["country"], country):
                    if isinstance(affected_country, str):
                        conflicting_countries.setdefault(affected_country, set()).add(
                            row["source_id"]
                        )
            documents_by_country.setdefault(country, set()).add(row["document_id"])
            if (
                row["language_review_status"] == "RECORDED"
                and row["language"] != "unknown"
            ):
                languages_by_country.setdefault(country, set()).add(row["language"])
        for source_id in conflicting_sources:
            source_context.pop(source_id, None)

        source_ids_by_country: dict[str, set[str]] = {}
        for source_id, context in source_context.items():
            country = context["country"]
            if isinstance(country, str):
                source_ids_by_country.setdefault(country, set()).add(source_id)
        claims_by_country: dict[str, set[str]] = {}
        for row in claim_rows:
            payload = json.loads(row["payload_json"])
            if payload["event_id"] == event_id:
                context = source_context.get(payload["source_id"])
                country = context.get("country") if context is not None else None
                if isinstance(country, str):
                    claims_by_country.setdefault(country, set()).add(row["record_id"])
        evidence_by_country: dict[str, list[dict[str, JsonValue]]] = {}
        for row in evidence_rows:
            if row["current_verification_status"] != "VERIFIED":
                continue
            payload = json.loads(row["payload_json"])
            if payload["event_id"] == event_id:
                context = source_context.get(payload["source_id"])
                country = context.get("country") if context is not None else None
                if isinstance(country, str):
                    evidence_by_country.setdefault(country, []).append(payload)
        relation_counts: dict[str, dict[str, int]] = {}
        for row in latest_relations:
            country = next(
                (
                    country_code
                    for country_code, claim_ids in claims_by_country.items()
                    if row["claim_id"] in claim_ids
                ),
                None,
            )
            if country is not None:
                counts = relation_counts.setdefault(country, {"SUPPORTS": 0, "CONTRADICTS": 0})
                if row["relationship"] in counts:
                    counts[row["relationship"]] += 1
        groups_by_member: dict[tuple[str, str], set[str]] = {}
        for row in assignment_rows:
            groups_by_member.setdefault(
                (row["member_type"], row["member_id"]), set()
            ).add(row["group_id"])

        latest_assessments: dict[str, CountryMatrixAssessment] = {}
        for row in assessment_rows:
            latest_assessments.setdefault(
                row["country_code"], self._country_matrix_assessment_from_row(row)
            )

        cells: list[CountryMatrixCell] = []
        for country in plan.countries:
            country_code = str(country["code"])
            raw_languages = country.get("languages")
            selected_languages = (
                tuple(item for item in raw_languages if isinstance(item, str))
                if isinstance(raw_languages, list)
                else ()
            )
            docs = documents_by_country.get(country_code, set())
            sources = source_ids_by_country.get(country_code, set())
            metadata_classes = {
                source_context[source_id].get("source_class")
                for source_id in sources
                if source_id in source_context
            }
            evidence = evidence_by_country.get(country_code, [])
            primary_evidence = sum(
                1 for item in evidence if item.get("directness") == "PRIMARY"
            )
            uncovered = tuple(
                language
                for language in selected_languages
                if language not in languages_by_country.get(country_code, set())
            )
            gaps: list[str] = []
            if not docs:
                gaps.append("No confirmed event-linked reporting documents.")
            if not uncovered:
                pass
            elif docs:
                gaps.extend(f"No confirmed original-language document for {language}."
                            for language in uncovered)
            else:
                gaps.extend(f"Language {language} has no confirmed coverage."
                            for language in uncovered)
            if "INSTITUTIONAL_STATEMENT" not in metadata_classes:
                gaps.append("No linked institutional statement.")
            if primary_evidence == 0:
                gaps.append("No verified primary-directness evidence record.")
            conflicting = conflicting_countries.get(country_code, set())
            if conflicting:
                gaps.append(
                    f"{len(conflicting)} linked source(s) have conflicting collection "
                    "metadata and are excluded from source, claim and evidence counts."
                )
            groups: set[str] = set()
            for source_id in sources:
                groups.update(groups_by_member.get(("SOURCE", source_id), set()))
            for document_id in docs:
                groups.update(groups_by_member.get(("DOCUMENT", document_id), set()))
            for item in evidence:
                groups.update(
                    groups_by_member.get(("EVIDENCE", str(item["evidence_id"])), set())
                )
            cells.append(
                CountryMatrixCell(
                    country_code=country_code,
                    country_name=str(country["name"]),
                    languages=selected_languages,
                    reporting_documents=len(docs),
                    reporting_sources=len(sources),
                    institutional_sources=sum(
                        1 for source_id in sources
                        if source_context[source_id].get("source_class")
                        == "INSTITUTIONAL_STATEMENT"
                    ),
                    primary_observation_sources=sum(
                        1 for source_id in sources
                        if source_context[source_id].get("source_class")
                        == "PRIMARY_OBSERVATION"
                    ),
                    primary_evidence=primary_evidence,
                    claims=len(claims_by_country.get(country_code, set())),
                    supporting_relations=relation_counts.get(country_code, {}).get(
                        "SUPPORTS", 0
                    ),
                    contradicting_relations=relation_counts.get(country_code, {}).get(
                        "CONTRADICTS", 0
                    ),
                    accepted_independence_groups=tuple(sorted(groups)),
                    uncovered_languages=uncovered,
                    coverage_gaps=tuple(gaps),
                    assessment=latest_assessments.get(country_code),
                )
            )
        return NationalInformationMatrix(
            event_id=event_id,
            plan=plan,
            config_matches=plan.config_sha256 == config_sha256,
            cells=tuple(cells),
        )

    @staticmethod
    def _duplicate_relationship_from_row(row: sqlite3.Row) -> DuplicateRelationship:
        return DuplicateRelationship(
            relationship_id=row["relationship_id"],
            document_id=row["document_id"],
            related_document_id=row["related_document_id"],
            relationship_type=row["relationship_type"],
            similarity=row["similarity"],
            review_status=row["review_status"],
            review_decision=row["review_decision"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            rationale=row["rationale"],
            created_at=row["created_at"],
        )

    def get_duplicate_relationship(
        self, relationship_id: str
    ) -> DuplicateRelationship:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT relationship.*,
                    review.decision AS review_decision,
                    review.reviewed_by,
                    review.reviewed_at,
                    review.rationale,
                    CASE WHEN review.review_id IS NULL
                         THEN 'PENDING' ELSE 'REVIEWED' END AS review_status
                FROM duplicate_relationships AS relationship
                LEFT JOIN duplicate_reviews AS review
                    ON review.review_id = (
                        SELECT latest.review_id
                        FROM duplicate_reviews AS latest
                        WHERE latest.relationship_id = relationship.relationship_id
                        ORDER BY latest.rowid DESC
                        LIMIT 1
                    )
                WHERE relationship.relationship_id = ?
                """,
                (relationship_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"Duplicate relationship {relationship_id} was not found.")
        return self._duplicate_relationship_from_row(row)

    def duplicate_relationships(
        self,
        *,
        document_id: str | None = None,
        review_status: str | None = None,
    ) -> list[DuplicateRelationship]:
        if review_status not in {None, "PENDING", "REVIEWED"}:
            raise ValueError("review_status must be PENDING or REVIEWED.")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT relationship.*,
                    review.decision AS review_decision,
                    review.reviewed_by,
                    review.reviewed_at,
                    review.rationale,
                    CASE WHEN review.review_id IS NULL
                         THEN 'PENDING' ELSE 'REVIEWED' END AS review_status
                FROM duplicate_relationships AS relationship
                LEFT JOIN duplicate_reviews AS review
                    ON review.review_id = (
                        SELECT latest.review_id
                        FROM duplicate_reviews AS latest
                        WHERE latest.relationship_id = relationship.relationship_id
                        ORDER BY latest.rowid DESC
                        LIMIT 1
                    )
                WHERE (? IS NULL
                    OR relationship.document_id = ?
                    OR relationship.related_document_id = ?)
                  AND (? IS NULL
                    OR (? = 'PENDING' AND review.review_id IS NULL)
                    OR (? = 'REVIEWED' AND review.review_id IS NOT NULL))
                ORDER BY relationship.created_at, relationship.relationship_id
                """,
                (
                    document_id,
                    document_id,
                    document_id,
                    review_status,
                    review_status,
                    review_status,
                ),
            ).fetchall()
        return [self._duplicate_relationship_from_row(row) for row in rows]

    def review_duplicate_relationship(
        self,
        *,
        relationship_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
    ) -> DuplicateRelationship:
        decisions = {
            "CONFIRMED_DEPENDENT",
            "INDEPENDENT_EVIDENCE_DOCUMENTED",
            "REJECTED",
            "UNRESOLVED",
        }
        if decision not in decisions:
            raise StorageError(f"Unsupported duplicate review decision: {decision}.")
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("Duplicate review needs a reviewer and rationale.")
        if _RFC3339_TIMESTAMP.fullmatch(reviewed_at) is None:
            raise StorageError("reviewed_at must be an RFC3339 timestamp with a timezone.")
        try:
            timestamp = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise StorageError("reviewed_at must be a valid RFC3339 timestamp.") from error
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise StorageError("reviewed_at must include a timezone.")
        review_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM duplicate_relationships WHERE relationship_id = ?",
                (relationship_id,),
            ).fetchone()
            if exists is None:
                raise StorageError(
                    f"Duplicate relationship {relationship_id} was not found."
                )
            connection.execute(
                """
                INSERT INTO duplicate_reviews (
                    review_id, relationship_id, decision, reviewed_by,
                    reviewed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    relationship_id,
                    decision,
                    reviewed_by.strip(),
                    reviewed_at,
                    rationale.strip(),
                ),
            )
        return self.get_duplicate_relationship(relationship_id)

    @staticmethod
    def _provenance_link_select() -> str:
        return """
            SELECT link.*,
                document.source_id,
                upstream.source_id AS upstream_source_id,
                review.decision AS review_status,
                review.reviewed_by,
                review.reviewed_at,
                review.rationale AS review_rationale
            FROM provenance_links AS link
            JOIN normalized_documents AS document
                ON document.document_id = link.document_id
            JOIN normalized_documents AS upstream
                ON upstream.document_id = link.upstream_document_id
            LEFT JOIN provenance_link_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM provenance_link_reviews AS latest
                    WHERE latest.link_id = link.link_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
        """

    @staticmethod
    def _provenance_link_from_row(row: sqlite3.Row) -> ProvenanceLink:
        return ProvenanceLink(
            link_id=row["link_id"],
            document_id=row["document_id"],
            source_id=row["source_id"],
            upstream_document_id=row["upstream_document_id"],
            upstream_source_id=row["upstream_source_id"],
            relationship_type=row["relationship_type"],
            proposed_by=row["proposed_by"],
            proposed_at=row["proposed_at"],
            rationale=row["rationale"],
            review_status=row["review_status"] or "PENDING",
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_rationale=row["review_rationale"],
        )

    def propose_provenance_link(
        self,
        *,
        document_id: str,
        upstream_document_id: str,
        relationship_type: str,
        proposed_by: str,
        proposed_at: str,
        rationale: str,
    ) -> ProvenanceLink:
        relationship_types = {"CITES", "QUOTES", "SYNDICATED", "DERIVED_FROM"}
        if relationship_type not in relationship_types:
            raise StorageError(
                f"Unsupported provenance relationship type: {relationship_type}."
            )
        if document_id == upstream_document_id:
            raise StorageError("A provenance link cannot point a document to itself.")
        if not proposed_by.strip() or not rationale.strip():
            raise StorageError("A provenance link proposal needs an analyst and rationale.")
        _validate_timestamp(proposed_at, "proposed_at")
        link_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            for candidate_document_id in (document_id, upstream_document_id):
                exists = connection.execute(
                    "SELECT 1 FROM normalized_documents WHERE document_id = ?",
                    (candidate_document_id,),
                ).fetchone()
                if exists is None:
                    raise StorageError(
                        f"Normalized document {candidate_document_id} was not found."
                    )
            connection.execute(
                """
                INSERT INTO provenance_links (
                    link_id, document_id, upstream_document_id, relationship_type,
                    proposed_by, proposed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_id,
                    document_id,
                    upstream_document_id,
                    relationship_type,
                    proposed_by.strip(),
                    proposed_at,
                    rationale.strip(),
                ),
            )
        return self.get_provenance_link(link_id)

    def get_provenance_link(self, link_id: str) -> ProvenanceLink:
        with self._connect() as connection:
            row = connection.execute(
                self._provenance_link_select() + " WHERE link.link_id = ?",
                (link_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"Provenance link {link_id} was not found.")
        return self._provenance_link_from_row(row)

    def provenance_links(
        self,
        *,
        document_id: str | None = None,
        relationship_type: str | None = None,
        review_status: str | None = None,
    ) -> list[ProvenanceLink]:
        statuses = {None, "PENDING", "CONFIRMED", "REJECTED", "UNRESOLVED"}
        relationship_types = {None, "CITES", "QUOTES", "SYNDICATED", "DERIVED_FROM"}
        if review_status not in statuses:
            raise ValueError("Unsupported provenance link review status.")
        if relationship_type not in relationship_types:
            raise ValueError("Unsupported provenance relationship type.")
        query = self._provenance_link_select() + """
            WHERE (? IS NULL OR link.document_id = ?
                OR link.upstream_document_id = ?)
              AND (? IS NULL OR link.relationship_type = ?)
              AND (? IS NULL OR COALESCE(review.decision, 'PENDING') = ?)
            ORDER BY link.proposed_at, link.link_id
        """
        with self._connect() as connection:
            rows = connection.execute(
                query,
                (
                    document_id,
                    document_id,
                    document_id,
                    relationship_type,
                    relationship_type,
                    review_status,
                    review_status,
                ),
            ).fetchall()
        return [self._provenance_link_from_row(row) for row in rows]

    def _confirmed_provenance_links(
        self,
        connection: sqlite3.Connection,
        *,
        exclude_link_id: str | None = None,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT link.link_id, link.document_id, link.upstream_document_id
            FROM provenance_links AS link
            JOIN provenance_link_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM provenance_link_reviews AS latest
                    WHERE latest.link_id = link.link_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
            WHERE review.decision = 'CONFIRMED'
              AND (? IS NULL OR link.link_id != ?)
            """,
            (exclude_link_id, exclude_link_id),
        ).fetchall()

    @staticmethod
    def _provenance_path_exists(
        links: list[sqlite3.Row], start_document_id: str, target_document_id: str
    ) -> bool:
        upstream: dict[str, list[str]] = {}
        for link in links:
            upstream.setdefault(link["document_id"], []).append(
                link["upstream_document_id"]
            )
        pending = [start_document_id]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target_document_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(upstream.get(current, ()))
        return False

    def review_provenance_link(
        self,
        *,
        link_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
    ) -> ProvenanceLink:
        if decision not in {"CONFIRMED", "REJECTED", "UNRESOLVED"}:
            raise StorageError(f"Unsupported provenance link decision: {decision}.")
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("Provenance review needs a reviewer and rationale.")
        _validate_timestamp(reviewed_at, "reviewed_at")
        review_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            link = connection.execute(
                """
                SELECT document_id, upstream_document_id, relationship_type
                FROM provenance_links WHERE link_id = ?
                """,
                (link_id,),
            ).fetchone()
            if link is None:
                raise StorageError(f"Provenance link {link_id} was not found.")
            if decision == "CONFIRMED":
                if link["relationship_type"] == "SYNDICATED":
                    groups = self._active_syndication_groups(
                        connection,
                        [
                            link["document_id"],
                            link["upstream_document_id"],
                        ],
                    )
                    if len(groups) > 1:
                        raise StorageError(
                            "Documents in the syndication chain belong to different "
                            "independence groups; resolve the group assignments first."
                        )
                confirmed = self._confirmed_provenance_links(
                    connection, exclude_link_id=link_id
                )
                if self._provenance_path_exists(
                    confirmed, link["upstream_document_id"], link["document_id"]
                ):
                    raise StorageError(
                        "Confirming this provenance link would create a cycle."
                    )
            connection.execute(
                """
                INSERT INTO provenance_link_reviews (
                    review_id, link_id, decision, reviewed_by, reviewed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    link_id,
                    decision,
                    reviewed_by.strip(),
                    reviewed_at,
                    rationale.strip(),
                ),
            )
        return self.get_provenance_link(link_id)

    def provenance_link_history(self, link_id: str) -> list[ProvenanceLink]:
        with self._connect() as connection:
            proposal = connection.execute(
                self._provenance_link_select() + " WHERE link.link_id = ?",
                (link_id,),
            ).fetchone()
            reviews = connection.execute(
                """
                SELECT decision, reviewed_by, reviewed_at, rationale
                FROM provenance_link_reviews
                WHERE link_id = ? ORDER BY rowid
                """,
                (link_id,),
            ).fetchall()
        if proposal is None:
            raise StorageError(f"Provenance link {link_id} was not found.")
        initial = self._provenance_link_from_row(proposal)
        initial = ProvenanceLink(
            **{
                **{
                    field: getattr(initial, field)
                    for field in initial.__dataclass_fields__
                },
                "review_status": "PENDING",
                "reviewed_by": None,
                "reviewed_at": None,
                "review_rationale": None,
            }
        )
        return [
            initial,
            *(
                ProvenanceLink(
                    **{
                        **{
                            field: getattr(initial, field)
                            for field in initial.__dataclass_fields__
                        },
                        "review_status": review["decision"],
                        "reviewed_by": review["reviewed_by"],
                        "reviewed_at": review["reviewed_at"],
                        "review_rationale": review["rationale"],
                    }
                )
                for review in reviews
            ),
        ]

    def provenance_graph(self, document_id: str) -> list[ProvenanceLink]:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM normalized_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if exists is None:
                raise StorageError(f"Normalized document {document_id} was not found.")
            links = self._confirmed_provenance_links(connection)
            adjacency: dict[str, list[sqlite3.Row]] = {}
            for link in links:
                adjacency.setdefault(link["document_id"], []).append(link)
            reachable: set[str] = set()
            pending = [document_id]
            selected_link_ids: set[str] = set()
            while pending:
                current = pending.pop()
                if current in reachable:
                    continue
                reachable.add(current)
                for link in adjacency.get(current, ()):
                    selected_link_ids.add(link["link_id"])
                    pending.append(link["upstream_document_id"])
            if not selected_link_ids:
                return []
            query = self._provenance_link_select() + """
                WHERE link.link_id IN (
            """ + ",".join("?" for _ in selected_link_ids) + ")"
            rows = connection.execute(
                query, tuple(sorted(selected_link_ids))
            ).fetchall()
        return [self._provenance_link_from_row(row) for row in rows]

    @staticmethod
    def _provenance_origin_from_row(
        row: sqlite3.Row, supporting_link_ids: tuple[str, ...]
    ) -> ProvenanceOriginAssessment:
        return ProvenanceOriginAssessment(
            assessment_id=row["assessment_id"],
            document_id=row["document_id"],
            origin_status=row["origin_status"],
            earliest_origin_document_id=row["earliest_origin_document_id"],
            assessed_by=row["assessed_by"],
            assessed_at=row["assessed_at"],
            rationale=row["rationale"],
            supporting_link_ids=supporting_link_ids,
        )

    def _confirmed_support_links(
        self, connection: sqlite3.Connection, link_ids: list[str]
    ) -> list[sqlite3.Row]:
        if len(link_ids) != len(set(link_ids)):
            raise StorageError("A supporting provenance link cannot be repeated.")
        if not link_ids:
            return []
        rows = connection.execute(
            """
            SELECT link.link_id, link.document_id, link.upstream_document_id,
                review.decision
            FROM provenance_links AS link
            LEFT JOIN provenance_link_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM provenance_link_reviews AS latest
                    WHERE latest.link_id = link.link_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
            WHERE link.link_id IN (
            """ + ",".join("?" for _ in link_ids) + ")",
            link_ids,
        ).fetchall()
        by_id = {row["link_id"]: row for row in rows}
        if len(by_id) != len(link_ids):
            missing = next(link_id for link_id in link_ids if link_id not in by_id)
            raise StorageError(f"Supporting provenance link {missing} was not found.")
        for link_id in link_ids:
            if by_id[link_id]["decision"] != "CONFIRMED":
                raise StorageError(
                    f"Supporting provenance link {link_id} is not confirmed."
                )
        return [by_id[link_id] for link_id in link_ids]

    def record_provenance_origin(
        self,
        *,
        document_id: str,
        origin_status: str,
        earliest_origin_document_id: str | None,
        assessed_by: str,
        assessed_at: str,
        rationale: str,
        supporting_link_ids: list[str] | None = None,
    ) -> ProvenanceOriginAssessment:
        if origin_status not in {"IDENTIFIED", "UNCERTAIN", "UNKNOWN"}:
            raise StorageError(f"Unsupported origin status: {origin_status}.")
        if not assessed_by.strip() or not rationale.strip():
            raise StorageError("An origin assessment needs an analyst and rationale.")
        _validate_timestamp(assessed_at, "assessed_at")
        if origin_status == "IDENTIFIED" and not earliest_origin_document_id:
            raise StorageError("An IDENTIFIED origin requires an earliest origin document.")
        if origin_status == "UNKNOWN" and earliest_origin_document_id is not None:
            raise StorageError("An UNKNOWN origin cannot name an earliest origin document.")
        if earliest_origin_document_id == document_id:
            raise StorageError("A document cannot be its own earliest origin.")
        assessment_id = uuid.uuid4().hex
        support_ids = supporting_link_ids or []
        with self._lock, self._write_transaction() as connection:
            for candidate_document_id in (
                document_id,
                *(
                    [earliest_origin_document_id]
                    if earliest_origin_document_id is not None
                    else []
                ),
            ):
                exists = connection.execute(
                    "SELECT 1 FROM normalized_documents WHERE document_id = ?",
                    (candidate_document_id,),
                ).fetchone()
                if exists is None:
                    raise StorageError(
                        f"Normalized document {candidate_document_id} was not found."
                    )
            supporting_links = self._confirmed_support_links(connection, support_ids)
            if origin_status == "IDENTIFIED":
                if not support_ids:
                    raise StorageError(
                        "An IDENTIFIED origin needs supporting confirmed provenance links."
                    )
                if not self._provenance_path_exists(
                    supporting_links,
                    document_id,
                    earliest_origin_document_id or "",
                ):
                    raise StorageError(
                        "Supporting links do not connect the document to its identified origin."
                    )
            connection.execute(
                """
                INSERT INTO provenance_origin_assessments (
                    assessment_id, document_id, origin_status,
                    earliest_origin_document_id, assessed_by, assessed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assessment_id,
                    document_id,
                    origin_status,
                    earliest_origin_document_id,
                    assessed_by.strip(),
                    assessed_at,
                    rationale.strip(),
                ),
            )
            for link_id in support_ids:
                connection.execute(
                    """
                    INSERT INTO provenance_origin_support (assessment_id, link_id)
                    VALUES (?, ?)
                    """,
                    (assessment_id, link_id),
                )
        return self.provenance_origin_history(document_id)[-1]

    def provenance_origin_history(
        self, document_id: str
    ) -> list[ProvenanceOriginAssessment]:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM normalized_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if exists is None:
                raise StorageError(f"Normalized document {document_id} was not found.")
            rows = connection.execute(
                """
                SELECT * FROM provenance_origin_assessments
                WHERE document_id = ? ORDER BY rowid
                """,
                (document_id,),
            ).fetchall()
            result = []
            for row in rows:
                support_rows = connection.execute(
                    """
                    SELECT link_id FROM provenance_origin_support
                    WHERE assessment_id = ? ORDER BY rowid
                    """,
                    (row["assessment_id"],),
                ).fetchall()
                result.append(
                    self._provenance_origin_from_row(
                        row, tuple(item["link_id"] for item in support_rows)
                    )
                )
        return result

    def latest_provenance_origin(
        self, document_id: str
    ) -> ProvenanceOriginAssessment | None:
        history = self.provenance_origin_history(document_id)
        return history[-1] if history else None

    def _validate_independence_member(
        self, connection: sqlite3.Connection, member_type: str, member_id: str
    ) -> list[str]:
        if member_type == "DOCUMENT":
            row = connection.execute(
                "SELECT document_id FROM normalized_documents WHERE document_id = ?",
                (member_id,),
            ).fetchone()
            document_ids = [row["document_id"]] if row else []
        elif member_type == "SOURCE":
            exists = connection.execute(
                """
                SELECT 1
                WHERE EXISTS (
                    SELECT 1 FROM normalized_documents WHERE source_id = ?
                ) OR EXISTS (
                    SELECT 1 FROM collection_attempts WHERE source_id = ?
                ) OR EXISTS (
                    SELECT 1 FROM canonical_records
                    WHERE record_type = 'source' AND record_id = ?
                )
                """,
                (member_id, member_id, member_id),
            ).fetchone()
            if exists is None:
                raise StorageError(f"Source {member_id} was not found.")
            document_ids = [
                row["document_id"]
                for row in connection.execute(
                    "SELECT document_id FROM normalized_documents WHERE source_id = ?",
                    (member_id,),
                ).fetchall()
            ]
        elif member_type == "EVIDENCE":
            record = connection.execute(
                """
                SELECT payload_json FROM canonical_records
                WHERE record_type = 'evidence' AND record_id = ?
                """,
                (member_id,),
            ).fetchone()
            if record is None:
                raise StorageError(f"Evidence {member_id} was not found.")
            source_id = json.loads(record["payload_json"])["source_id"]
            document_ids = [
                row["document_id"]
                for row in connection.execute(
                    "SELECT document_id FROM normalized_documents WHERE source_id = ?",
                    (source_id,),
                ).fetchall()
            ]
        else:
            raise StorageError(
                "Independence members must be DOCUMENT, SOURCE or EVIDENCE."
            )
        if not document_ids:
            if member_type == "DOCUMENT":
                raise StorageError(f"Document {member_id} was not found.")
        return document_ids

    def _confirmed_syndication_component(
        self, connection: sqlite3.Connection, document_ids: list[str]
    ) -> set[str]:
        links = connection.execute(
            """
            SELECT link.document_id, link.upstream_document_id
            FROM provenance_links AS link
            JOIN provenance_link_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM provenance_link_reviews AS latest
                    WHERE latest.link_id = link.link_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
            WHERE link.relationship_type = 'SYNDICATED'
              AND review.decision = 'CONFIRMED'
            """
        ).fetchall()
        adjacent: dict[str, set[str]] = {}
        for link in links:
            document_id = link["document_id"]
            upstream_document_id = link["upstream_document_id"]
            adjacent.setdefault(document_id, set()).add(upstream_document_id)
            adjacent.setdefault(upstream_document_id, set()).add(document_id)
        component: set[str] = set()
        pending = list(document_ids)
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(adjacent.get(current, ()))
        return component

    def _validate_syndication_group(
        self,
        connection: sqlite3.Connection,
        *,
        group_id: str,
        document_ids: list[str],
    ) -> None:
        groups = self._active_syndication_groups(connection, document_ids)
        if any(existing_group != group_id for existing_group in groups):
            raise StorageError(
                "A document in a confirmed syndication chain already belongs "
                "to a different independence group; dependent reports must use "
                "the same group."
            )

    def _active_syndication_groups(
        self, connection: sqlite3.Connection, document_ids: list[str]
    ) -> set[str]:
        component = self._confirmed_syndication_component(connection, document_ids)
        if not component:
            return set()
        current_memberships = connection.execute(
            self._independence_assignment_select()
            + """
                WHERE assignment.rowid = (
                    SELECT MAX(current.rowid)
                    FROM independence_assignments AS current
                    WHERE current.member_type = assignment.member_type
                      AND current.member_id = assignment.member_id
                )
                  AND review.decision = 'ACCEPTED'
            """
        ).fetchall()
        groups: set[str] = set()
        for row in current_memberships:
            member_documents = self._validate_independence_member(
                connection, row["member_type"], row["member_id"]
            )
            if component.intersection(member_documents):
                groups.add(row["group_id"])
        return groups

    @staticmethod
    def _independence_assignment_select() -> str:
        return """
            SELECT assignment.*,
                review.decision AS review_status,
                review.review_id,
                review.reviewed_by,
                review.reviewed_at,
                review.rationale AS review_rationale
            FROM independence_assignments AS assignment
            LEFT JOIN independence_assignment_reviews AS review
                ON review.review_id = (
                    SELECT latest.review_id
                    FROM independence_assignment_reviews AS latest
                    WHERE latest.assignment_id = assignment.assignment_id
                    ORDER BY latest.rowid DESC
                    LIMIT 1
                )
        """

    @staticmethod
    def _independence_assignment_from_row(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> IndependenceAssignment:
        review_id = row["review_id"]
        support_rows = (
            connection.execute(
                """
                SELECT link_id FROM independence_review_support
                WHERE review_id = ? ORDER BY rowid
                """,
                (review_id,),
            ).fetchall()
            if review_id is not None
            else []
        )
        return IndependenceAssignment(
            assignment_id=row["assignment_id"],
            group_id=row["group_id"],
            member_type=row["member_type"],
            member_id=row["member_id"],
            proposed_by=row["proposed_by"],
            proposed_at=row["proposed_at"],
            rationale=row["rationale"],
            review_status=row["review_status"] or "PENDING",
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_rationale=row["review_rationale"],
            supporting_link_ids=tuple(item["link_id"] for item in support_rows),
        )

    def propose_independence_assignment(
        self,
        *,
        group_id: str,
        member_type: str,
        member_id: str,
        proposed_by: str,
        proposed_at: str,
        rationale: str,
    ) -> IndependenceAssignment:
        if not group_id.strip() or not member_id.strip():
            raise StorageError("An independence assignment needs a group and member.")
        if member_type not in {"DOCUMENT", "SOURCE", "EVIDENCE"}:
            raise StorageError(
                "Independence members must be DOCUMENT, SOURCE or EVIDENCE."
            )
        if not proposed_by.strip() or not rationale.strip():
            raise StorageError(
                "An independence assignment proposal needs an analyst and rationale."
            )
        _validate_timestamp(proposed_at, "proposed_at")
        assignment_id = uuid.uuid4().hex
        with self._lock, self._write_transaction() as connection:
            self._validate_independence_member(connection, member_type, member_id)
            existing = connection.execute(
                self._independence_assignment_select()
                + """
                    WHERE assignment.member_type = ? AND assignment.member_id = ?
                    ORDER BY assignment.rowid DESC LIMIT 1
                """,
                (member_type, member_id),
            ).fetchone()
            if existing is not None and (
                existing["review_status"] in (None, "ACCEPTED")
            ):
                raise StorageError(
                    f"{member_type.lower()} {member_id} already has a pending or "
                    "accepted independence assignment."
                )
            connection.execute(
                """
                INSERT INTO independence_assignments (
                    assignment_id, group_id, member_type, member_id,
                    proposed_by, proposed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    assignment_id,
                    group_id.strip(),
                    member_type,
                    member_id.strip(),
                    proposed_by.strip(),
                    proposed_at,
                    rationale.strip(),
                ),
            )
        return self.independence_assignments(
            assignment_id=assignment_id
        )[0]

    def independence_assignments(
        self,
        *,
        group_id: str | None = None,
        member_type: str | None = None,
        member_id: str | None = None,
        review_status: str | None = None,
        assignment_id: str | None = None,
    ) -> list[IndependenceAssignment]:
        if member_type not in {None, "DOCUMENT", "SOURCE", "EVIDENCE"}:
            raise ValueError("Unsupported independence member type.")
        if review_status not in {
            None,
            "PENDING",
            "ACCEPTED",
            "REJECTED",
            "UNRESOLVED",
        }:
            raise ValueError("Unsupported independence review status.")
        query = self._independence_assignment_select() + """
            WHERE (? IS NULL OR assignment.group_id = ?)
              AND (? IS NULL OR assignment.member_type = ?)
              AND (? IS NULL OR assignment.member_id = ?)
              AND (? IS NULL OR assignment.assignment_id = ?)
              AND (? IS NULL OR COALESCE(review.decision, 'PENDING') = ?)
            ORDER BY assignment.proposed_at, assignment.assignment_id
        """
        parameters = (
            group_id,
            group_id,
            member_type,
            member_type,
            member_id,
            member_id,
            assignment_id,
            assignment_id,
            review_status,
            review_status,
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            assignments = [
                self._independence_assignment_from_row(connection, row)
                for row in rows
            ]
        return assignments

    def review_independence_assignment(
        self,
        *,
        assignment_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
        supporting_link_ids: list[str] | None = None,
    ) -> IndependenceAssignment:
        if decision not in {"ACCEPTED", "REJECTED", "UNRESOLVED"}:
            raise StorageError(
                f"Unsupported independence assignment decision: {decision}."
            )
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("Independence review needs a reviewer and rationale.")
        _validate_timestamp(reviewed_at, "reviewed_at")
        review_id = uuid.uuid4().hex
        support_ids = supporting_link_ids or []
        with self._lock, self._write_transaction() as connection:
            assignment = connection.execute(
                """
                SELECT * FROM independence_assignments
                WHERE assignment_id = ?
                """,
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise StorageError(
                    f"Independence assignment {assignment_id} was not found."
                )
            member_documents = self._validate_independence_member(
                connection, assignment["member_type"], assignment["member_id"]
            )
            supporting_links = self._confirmed_support_links(connection, support_ids)
            if support_ids and (
                not member_documents
                or not any(
                    link["document_id"] in member_documents
                    or link["upstream_document_id"] in member_documents
                    for link in supporting_links
                )
            ):
                raise StorageError(
                    "Supporting provenance links do not involve this independence member."
                )
            if decision == "ACCEPTED":
                other_accepted = connection.execute(
                    self._independence_assignment_select()
                    + """
                        WHERE assignment.member_type = ?
                          AND assignment.member_id = ?
                          AND assignment.assignment_id != ?
                          AND review.decision = 'ACCEPTED'
                        LIMIT 1
                    """,
                    (
                        assignment["member_type"],
                        assignment["member_id"],
                        assignment_id,
                    ),
                ).fetchone()
                if other_accepted is not None:
                    raise StorageError(
                        "An independence member cannot have multiple accepted groups; "
                        "resolve its existing assignment first."
                    )
                self._validate_syndication_group(
                    connection,
                    group_id=assignment["group_id"],
                    document_ids=member_documents,
                )
            connection.execute(
                """
                INSERT INTO independence_assignment_reviews (
                    review_id, assignment_id, decision, reviewed_by,
                    reviewed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    assignment_id,
                    decision,
                    reviewed_by.strip(),
                    reviewed_at,
                    rationale.strip(),
                ),
            )
            for link_id in support_ids:
                connection.execute(
                    """
                    INSERT INTO independence_review_support (review_id, link_id)
                    VALUES (?, ?)
                    """,
                    (review_id, link_id),
                )
        return self.independence_assignments(assignment_id=assignment_id)[0]

    def independence_assignment_history(
        self, assignment_id: str
    ) -> list[IndependenceAssignment]:
        with self._connect() as connection:
            assignment = connection.execute(
                "SELECT * FROM independence_assignments WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise StorageError(
                    f"Independence assignment {assignment_id} was not found."
                )
            reviews = connection.execute(
                """
                SELECT * FROM independence_assignment_reviews
                WHERE assignment_id = ? ORDER BY rowid
                """,
                (assignment_id,),
            ).fetchall()
            initial_values = {
                "assignment_id": assignment["assignment_id"],
                "group_id": assignment["group_id"],
                "member_type": assignment["member_type"],
                "member_id": assignment["member_id"],
                "proposed_by": assignment["proposed_by"],
                "proposed_at": assignment["proposed_at"],
                "rationale": assignment["rationale"],
            }
            history = [IndependenceAssignment(
                **initial_values,
                review_status="PENDING",
                reviewed_by=None,
                reviewed_at=None,
                review_rationale=None,
                supporting_link_ids=(),
            )]
            for review in reviews:
                support_rows = connection.execute(
                    """
                    SELECT link_id FROM independence_review_support
                    WHERE review_id = ? ORDER BY rowid
                    """,
                    (review["review_id"],),
                ).fetchall()
                history.append(
                    IndependenceAssignment(
                        **initial_values,
                        review_status=review["decision"],
                        reviewed_by=review["reviewed_by"],
                        reviewed_at=review["reviewed_at"],
                        review_rationale=review["rationale"],
                        supporting_link_ids=tuple(
                            row["link_id"] for row in support_rows
                        ),
                    )
                )
        return history

    def get_raw_snapshot(self, content_ref: str) -> RawSnapshot:
        if not content_ref.startswith("sha256:"):
            raise StorageError("Raw snapshot reference must use the sha256: prefix.")
        digest = content_ref.removeprefix("sha256:")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT content_sha256, media_type, content, created_at
                FROM raw_snapshots WHERE content_sha256 = ?
                """,
                (digest,),
            ).fetchone()
        if row is None:
            raise StorageError(f"Raw snapshot {content_ref} was not found.")
        snapshot = RawSnapshot(
            content_sha256=row["content_sha256"],
            media_type=row["media_type"],
            content=bytes(row["content"]),
            created_at=row["created_at"],
        )
        if hashlib.sha256(snapshot.content).hexdigest() != snapshot.content_sha256:
            raise StorageError(f"Raw snapshot {content_ref} failed its integrity check.")
        return snapshot

    def get(
        self, record_type: str, record_id: str, *, revision: int | None = None
    ) -> CanonicalRecord:
        if record_type not in {"event", "source", "claim", "evidence", "assessment"}:
            raise StorageError(f"Unsupported canonical record type: {record_type}.")
        with self._connect() as connection:
            if revision is None:
                row = connection.execute(
                    """
                    SELECT payload_json FROM canonical_records
                    WHERE record_type = ? AND record_id = ?
                    """,
                    (record_type, record_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT payload_json FROM record_revisions
                    WHERE record_type = ? AND record_id = ? AND revision = ?
                    """,
                    (record_type, record_id, revision),
                ).fetchone()
        if row is None:
            version = f" revision {revision}" if revision is not None else ""
            raise StorageError(f"{record_type} record {record_id}{version} was not found.")
        try:
            return parse_record(record_type, json.loads(row["payload_json"]))
        except (json.JSONDecodeError, RecordValidationError) as error:
            raise StorageError(
                f"Stored {record_type} record {record_id} is invalid: {error}"
            ) from error

    def revisions(self, record_type: str, record_id: str) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT revision FROM record_revisions
                WHERE record_type = ? AND record_id = ? ORDER BY revision
                """,
                (record_type, record_id),
            ).fetchall()
        return [row["revision"] for row in rows]

    def _capture_record_set(
        self, connection: sqlite3.Connection, assessment: Assessment
    ) -> list[dict[str, JsonValue]]:
        references: set[tuple[str, str]] = {
            ("event", assessment.event_id),
            *(("claim", claim_id) for claim_id in assessment.claim_ids),
            *(("evidence", evidence_id) for evidence_id in assessment.evidence_ids),
        }
        evidence_ids = set(assessment.evidence_ids)
        for claim_id in assessment.claim_ids:
            claim_payload = self._require_record(connection, "claim", claim_id)
            references.add(("source", claim_payload["source_id"]))
            evidence_ids.update(
                claim_payload["supporting_evidence_ids"]
                + claim_payload["contradicting_evidence_ids"]
            )
        references.update(("evidence", evidence_id) for evidence_id in evidence_ids)
        for evidence_id in evidence_ids:
            evidence_payload = self._require_record(connection, "evidence", evidence_id)
            if evidence_payload["event_id"] != assessment.event_id:
                raise StorageError(
                    f"Assessment references evidence {evidence_id} from a different event."
                )
            references.add(("source", evidence_payload["source_id"]))

        record_set: list[dict[str, JsonValue]] = []
        for record_type, record_id in sorted(references):
            row = connection.execute(
                """
                SELECT revision FROM canonical_records
                WHERE record_type = ? AND record_id = ?
                """,
                (record_type, record_id),
            ).fetchone()
            if row is None:
                raise StorageError(
                    f"Assessment references missing {record_type} record {record_id}."
                )
            record_set.append(
                {
                    "record_type": record_type,
                    "record_id": record_id,
                    "revision": row["revision"],
                }
            )
        return record_set

    def reconstruct_assessment(
        self, assessment_id: str, *, revision: int | None = None
    ) -> ReconstructedAssessment:
        if revision is None:
            assessment = self.get("assessment", assessment_id)
            assert isinstance(assessment, Assessment)
            revision = self.revisions("assessment", assessment_id)[-1]
        else:
            assessment = self.get("assessment", assessment_id, revision=revision)
            assert isinstance(assessment, Assessment)

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT record_set_json FROM assessment_revisions
                WHERE assessment_id = ? AND revision = ?
                """,
                (assessment_id, revision),
            ).fetchone()
        if row is None:
            raise StorageError(
                f"Assessment snapshot {assessment_id} revision {revision} was not found."
            )
        references = json.loads(row["record_set_json"])
        records = {
            (item["record_type"], item["record_id"]): self.get(
                item["record_type"], item["record_id"], revision=item["revision"]
            )
            for item in references
        }
        revisions = {
            (item["record_type"], item["record_id"]): item["revision"]
            for item in references
        }
        return ReconstructedAssessment(
            assessment=assessment,
            revision=revision,
            records=records,
            record_revisions=revisions,
        )

    def create_report_snapshot(
        self,
        *,
        report_id: str,
        event_id: str,
        assessment_id: str,
        assessment_revision: int,
        high_impact: bool,
        generated_at: str,
        input_snapshot: dict[str, JsonValue],
        markdown: str,
        html: str,
    ) -> ReportSnapshot:
        if not report_id.strip():
            raise StorageError("A report needs an identifier.")
        if assessment_revision < 1:
            raise StorageError("Assessment revision must be positive.")
        _validate_timestamp(generated_at, "generated_at")
        report_input = input_snapshot.get("report")
        assessment_input = input_snapshot.get("canonical_assessment_snapshot")
        if not isinstance(report_input, dict) or not isinstance(assessment_input, dict):
            raise StorageError(
                "A report snapshot must include its report and canonical assessment inputs."
            )
        assessment_record = assessment_input.get("assessment")
        if (
            report_input.get("report_id") != report_id
            or report_input.get("report_event_id") != event_id
            or report_input.get("assessment_id") != assessment_id
            or report_input.get("assessment_revision") != assessment_revision
            or report_input.get("generated_at") != generated_at
            or report_input.get("high_impact") is not high_impact
            or not isinstance(assessment_record, dict)
            or assessment_record.get("assessment_id") != assessment_id
            or assessment_record.get("event_id") != event_id
        ):
            raise StorageError(
                "Report metadata does not match its stored assessment snapshot."
            )
        input_snapshot_json = _json_text(input_snapshot)
        input_sha256 = hashlib.sha256(input_snapshot_json.encode("utf-8")).hexdigest()
        content_sha256 = hashlib.sha256(
            (markdown + "\0" + html).encode("utf-8")
        ).hexdigest()
        with self._lock, self._write_transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO report_snapshots (
                        report_id, event_id, assessment_id, assessment_revision,
                        high_impact, generated_at, input_snapshot_json,
                        input_sha256, markdown, html, content_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_id,
                        event_id,
                        assessment_id,
                        assessment_revision,
                        int(high_impact),
                        generated_at,
                        input_snapshot_json,
                        input_sha256,
                        markdown,
                        html,
                        content_sha256,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise StorageError(
                    f"Could not store report snapshot {report_id}: {error}"
                ) from error
        return self.get_report(report_id)

    def get_report(self, report_id: str) -> ReportSnapshot:
        with self._connect() as connection:
            row = connection.execute(
                self._report_select()
                + " WHERE report.report_id = ?",
                (report_id,),
            ).fetchone()
        if row is None:
            raise StorageError(f"Report {report_id} was not found.")
        report = self._report_from_row(row)
        input_hash = hashlib.sha256(
            report.input_snapshot_json.encode("utf-8")
        ).hexdigest()
        content_hash = hashlib.sha256(
            (report.markdown + "\0" + report.html).encode("utf-8")
        ).hexdigest()
        if input_hash != report.input_sha256 or content_hash != report.content_sha256:
            raise StorageError(f"Report {report_id} failed its integrity check.")
        return report

    def reports(self, *, event_id: str | None = None) -> list[ReportSnapshot]:
        with self._connect() as connection:
            rows = connection.execute(
                self._report_select()
                + """
                    WHERE (? IS NULL OR report.event_id = ?)
                    ORDER BY report.generated_at, report.report_id
                """,
                (event_id, event_id),
            ).fetchall()
        reports = [self._report_from_row(row) for row in rows]
        for report in reports:
            input_hash = hashlib.sha256(
                report.input_snapshot_json.encode("utf-8")
            ).hexdigest()
            content_hash = hashlib.sha256(
                (report.markdown + "\0" + report.html).encode("utf-8")
            ).hexdigest()
            if input_hash != report.input_sha256 or content_hash != report.content_sha256:
                raise StorageError(f"Report {report.report_id} failed its integrity check.")
        return reports

    def review_report(
        self,
        *,
        report_id: str,
        decision: str,
        reviewed_by: str,
        reviewed_at: str,
        rationale: str,
    ) -> ReportSnapshot:
        if decision not in {"APPROVED", "REJECTED"}:
            raise StorageError("Report review decision must be APPROVED or REJECTED.")
        if not reviewed_by.strip() or not rationale.strip():
            raise StorageError("Report review needs a reviewer and rationale.")
        _validate_timestamp(reviewed_at, "reviewed_at")
        with self._lock, self._write_transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM report_snapshots WHERE report_id = ?", (report_id,)
            ).fetchone()
            if exists is None:
                raise StorageError(f"Report {report_id} was not found.")
            connection.execute(
                """
                INSERT INTO report_reviews (
                    review_id, report_id, decision, reviewed_by, reviewed_at, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    report_id,
                    decision,
                    reviewed_by.strip(),
                    reviewed_at,
                    rationale.strip(),
                ),
            )
        return self.get_report(report_id)

    def report_review_history(self, report_id: str) -> list[ReportReview]:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM report_snapshots WHERE report_id = ?", (report_id,)
            ).fetchone()
            if exists is None:
                raise StorageError(f"Report {report_id} was not found.")
            rows = connection.execute(
                """
                SELECT decision, reviewed_by, reviewed_at, rationale
                FROM report_reviews
                WHERE report_id = ?
                ORDER BY sequence
                """,
                (report_id,),
            ).fetchall()
        return [
            ReportReview(
                report_id=report_id,
                decision=row["decision"],
                reviewed_by=row["reviewed_by"],
                reviewed_at=row["reviewed_at"],
                rationale=row["rationale"],
            )
            for row in rows
        ]

    @staticmethod
    def _report_select() -> str:
        return """
            SELECT report.*,
                   COALESCE(review.decision, 'PENDING') AS status,
                   review.reviewed_by,
                   review.reviewed_at,
                   review.rationale AS review_rationale
            FROM report_snapshots AS report
            LEFT JOIN report_reviews AS review
                ON review.sequence = (
                    SELECT MAX(latest.sequence)
                    FROM report_reviews AS latest
                    WHERE latest.report_id = report.report_id
                )
        """

    @staticmethod
    def _report_from_row(row: sqlite3.Row) -> ReportSnapshot:
        return ReportSnapshot(
            report_id=row["report_id"],
            event_id=row["event_id"],
            assessment_id=row["assessment_id"],
            assessment_revision=row["assessment_revision"],
            high_impact=bool(row["high_impact"]),
            generated_at=row["generated_at"],
            input_snapshot_json=row["input_snapshot_json"],
            input_sha256=row["input_sha256"],
            content_sha256=row["content_sha256"],
            markdown=row["markdown"],
            html=row["html"],
            status=row["status"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
            review_rationale=row["review_rationale"],
        )
