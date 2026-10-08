import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Any, Iterator

from cnvs.models import (
    Assessment,
    CanonicalRecord,
    Claim,
    ClaimExtraction,
    CollectionResult,
    Evidence,
    DuplicateRelationship,
    Event,
    EventSourceMatch,
    JsonValue,
    NormalizedDocument,
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
    records: dict[tuple[str, str], CanonicalRecord]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_text(payload: dict[str, JsonValue]) -> str:
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
        return ReconstructedAssessment(assessment=assessment, records=records)
