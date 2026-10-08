from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, ClassVar, TypeAlias


JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True)
class Event:
    schema_version: str
    event_id: str
    event_type: str
    title: str
    description: str
    status: str
    created_at: str
    updated_at: str
    start_time: str | None
    end_time: str | None
    location: str | None
    country: str | None
    coordinates: tuple[float, float] | None
    entities: tuple[str, ...]

    record_type: ClassVar[str] = "event"
    record_id_field: ClassVar[str] = "event_id"


@dataclass(frozen=True)
class Source:
    schema_version: str
    source_id: str
    source_class: str
    publisher: str
    title: str
    url: str
    language: str
    published_at: str | None
    collected_at: str
    origin_source_id: str | None
    independence_group: str
    raw_content_ref: str
    country: str | None
    notes: str

    record_type: ClassVar[str] = "source"
    record_id_field: ClassVar[str] = "source_id"


@dataclass(frozen=True)
class Claim:
    schema_version: str
    claim_id: str
    event_id: str
    subject: str
    predicate: str
    object: str
    claim_type: str
    attribution: str | None
    modality: str
    source_id: str
    publication_time: str | None
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    status: str
    confidence: str

    record_type: ClassVar[str] = "claim"
    record_id_field: ClassVar[str] = "claim_id"


@dataclass(frozen=True)
class Evidence:
    schema_version: str
    evidence_id: str
    event_id: str
    evidence_type: str
    observation: str
    source_id: str
    collection_time: str
    event_time: str | None
    directness: str
    independence_group: str
    verification_status: str
    analyst_notes: str
    reliability_assessment: str | None

    record_type: ClassVar[str] = "evidence"
    record_id_field: ClassVar[str] = "evidence_id"


@dataclass(frozen=True)
class Assessment:
    schema_version: str
    assessment_id: str
    event_id: str
    created_at: str
    pipeline_version: str
    facts: tuple[str, ...]
    claim_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    confidence: dict[str, str]
    contradictions: tuple[str, ...]
    information_gaps: tuple[str, ...]
    assessment_text: str
    human_reviewed: bool

    record_type: ClassVar[str] = "assessment"
    record_id_field: ClassVar[str] = "assessment_id"


@dataclass(frozen=True)
class RawSnapshot:
    content_sha256: str
    media_type: str
    content: bytes
    created_at: str

    @classmethod
    def from_content(
        cls, content: bytes, *, media_type: str, created_at: str
    ) -> "RawSnapshot":
        if not isinstance(content, bytes):
            raise TypeError("Raw snapshot content must be bytes.")
        return cls(
            content_sha256=sha256(content).hexdigest(),
            media_type=media_type,
            content=content,
            created_at=created_at,
        )

    @property
    def content_ref(self) -> str:
        return f"sha256:{self.content_sha256}"


@dataclass(frozen=True)
class CollectionResult:
    collection_id: str
    source_id: str
    source_metadata: dict[str, JsonValue]
    attempt_number: int
    status: str
    retry_of: str | None
    source_url: str
    final_url: str | None
    started_at: str
    completed_at: str
    http_status: int | None
    content_type: str | None
    content_sha256: str | None
    archived_content_ref: str | None
    origin_identifiers: tuple[str, ...]
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class NormalizedDocument:
    document_id: str
    collection_id: str
    source_id: str
    item_index: int
    origin_identifier: str
    original_title: str
    normalized_title: str
    original_text: str
    normalized_text: str
    original_url: str | None
    normalized_url: str | None
    publisher_original: str
    publisher_normalized: str
    language: str
    language_source: str
    language_review_status: str
    registry_language: str | None
    declared_language: str | None
    original_published_at: str | None
    normalized_published_at: str | None
    publication_timezone_known: bool
    text_sha256: str
    normalized_text_sha256: str
    created_at: str


@dataclass(frozen=True)
class TranslationRecord:
    translation_id: str
    document_id: str
    source_text_sha256: str
    source_language: str
    target_language: str
    translated_text: str
    translation_method: str
    translator: str
    translated_at: str


@dataclass(frozen=True)
class DuplicateRelationship:
    relationship_id: str
    document_id: str
    related_document_id: str
    relationship_type: str
    similarity: float
    review_status: str
    review_decision: str | None
    reviewed_by: str | None
    reviewed_at: str | None
    rationale: str | None
    created_at: str


CanonicalRecord: TypeAlias = Event | Source | Claim | Evidence | Assessment
RECORD_TYPES: dict[str, type[CanonicalRecord]] = {
    model.record_type: model
    for model in (Event, Source, Claim, Evidence, Assessment)
}


def record_to_payload(record: CanonicalRecord) -> dict[str, JsonValue]:
    """Serialize a typed canonical model to its JSON Schema representation."""
    payload = asdict(record)
    for key, value in tuple(payload.items()):
        if isinstance(value, tuple):
            payload[key] = list(value)
    return payload


def record_from_payload(record_type: str, payload: dict[str, Any]) -> CanonicalRecord:
    """Build a typed model from a schema-validated JSON payload."""
    model = RECORD_TYPES.get(record_type)
    if model is None:
        raise ValueError(f"Unsupported canonical record type: {record_type}.")
    normalized = dict(payload)
    tuple_fields = {
        Event: ("entities",),
        Claim: ("supporting_evidence_ids", "contradicting_evidence_ids"),
        Assessment: (
            "facts",
            "claim_ids",
            "evidence_ids",
            "contradictions",
            "information_gaps",
        ),
    }
    for field in tuple_fields.get(model, ()):
        normalized[field] = tuple(normalized[field])
    if model is Event and normalized["coordinates"] is not None:
        normalized["coordinates"] = tuple(normalized["coordinates"])
    return model(**normalized)
