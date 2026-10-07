import hashlib
import json
import sqlite3
import threading
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
    Evidence,
    JsonValue,
    RawSnapshot,
    Source,
)
from cnvs.validation import (
    RecordValidationError,
    parse_record,
    validate_record,
)


class StorageError(RuntimeError):
    """Raised when the database cannot safely store or reconstruct a record."""


@dataclass(frozen=True)
class ReconstructedAssessment:
    assessment: Assessment
    records: dict[tuple[str, str], CanonicalRecord]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_text(payload: dict[str, JsonValue]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        payload = validate_record(record)
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
