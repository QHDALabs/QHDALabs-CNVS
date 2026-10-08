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


@dataclass(frozen=True)
class EventSourceMatch:
    match_id: str
    event_id: str
    document_id: str
    source_id: str
    status: str
    proposed_by: str
    proposed_at: str
    rationale: str
    review_decision: str | None
    reviewed_by: str | None
    reviewed_at: str | None
    review_rationale: str | None


@dataclass(frozen=True)
class TimelineEntry:
    entry_id: str
    event_id: str
    document_id: str
    source_id: str
    publisher: str
    revision: int
    event_time: str | None
    event_time_rationale: str
    updated_by: str
    updated_at: str
    original_publication_time: str | None
    normalized_publication_time: str | None
    publication_timezone_known: bool
    collection_time: str
    source_title: str
    source_url: str | None


@dataclass(frozen=True)
class ClaimExtraction:
    candidate_id: str
    event_id: str
    document_id: str
    source_id: str
    source_text_sha256: str
    span_start: int
    span_end: int
    span_text: str
    subject: str
    predicate: str
    object: str
    claim_type: str
    attribution: str | None
    modality: str
    extraction_method: str
    extractor: str
    created_at: str
    review_status: str
    review_decision: str | None
    reviewed_by: str | None
    reviewed_at: str | None
    review_rationale: str | None
    correction_payload: dict[str, JsonValue] | None


@dataclass(frozen=True)
class ProvenanceLink:
    link_id: str
    document_id: str
    source_id: str
    upstream_document_id: str
    upstream_source_id: str
    relationship_type: str
    proposed_by: str
    proposed_at: str
    rationale: str
    review_status: str
    reviewed_by: str | None
    reviewed_at: str | None
    review_rationale: str | None


@dataclass(frozen=True)
class ProvenanceOriginAssessment:
    assessment_id: str
    document_id: str
    origin_status: str
    earliest_origin_document_id: str | None
    assessed_by: str
    assessed_at: str
    rationale: str
    supporting_link_ids: tuple[str, ...]


@dataclass(frozen=True)
class IndependenceAssignment:
    assignment_id: str
    group_id: str
    member_type: str
    member_id: str
    proposed_by: str
    proposed_at: str
    rationale: str
    review_status: str
    reviewed_by: str | None
    reviewed_at: str | None
    review_rationale: str | None
    supporting_link_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceReview:
    review_id: str
    evidence_id: str
    verification_status: str
    reviewed_by: str
    reviewed_at: str
    rationale: str
    analyst_note: str | None


@dataclass(frozen=True)
class EvidenceNote:
    note_id: str
    evidence_id: str
    analyst: str
    noted_at: str
    note: str
    rationale: str


@dataclass(frozen=True)
class ClaimEvidenceLink:
    link_id: str
    event_id: str
    claim_id: str
    evidence_id: str
    relationship: str
    proposed_by: str
    proposed_at: str
    rationale: str
    review_status: str
    reviewed_by: str | None
    reviewed_at: str | None
    review_rationale: str | None


@dataclass(frozen=True)
class EvidenceGap:
    gap_id: str
    event_id: str
    claim_id: str | None
    gap_type: str
    description: str
    created_by: str
    created_at: str
    rationale: str
    supporting_link_id: str | None
    contradicting_link_id: str | None
    status: str
    reviewed_by: str | None
    reviewed_at: str | None
    review_rationale: str | None


@dataclass(frozen=True)
class CountryCoveragePlan:
    plan_id: str
    event_id: str
    revision: int
    config_sha256: str
    countries: tuple[dict[str, JsonValue], ...]
    outside_catalog: tuple[dict[str, JsonValue], ...]
    proposed_by: str
    proposed_at: str
    rationale: str
    status: str
    reviewed_by: str | None
    reviewed_at: str | None
    review_rationale: str | None


@dataclass(frozen=True)
class CountryMatrixAssessment:
    assessment_id: str
    plan_id: str
    event_id: str
    country_code: str
    dominant_frame: str
    attribution_summary: str
    occurrence_confidence: str
    method_confidence: str
    attribution_confidence: str
    omissions: str
    contradictions: str
    supporting_source_ids: tuple[str, ...]
    supporting_claim_ids: tuple[str, ...]
    supporting_evidence_ids: tuple[str, ...]
    analyst: str
    created_at: str
    rationale: str
    status: str
    reviewed_by: str | None
    reviewed_at: str | None
    review_rationale: str | None


@dataclass(frozen=True)
class CountryMatrixCell:
    country_code: str
    country_name: str
    languages: tuple[str, ...]
    reporting_documents: int
    reporting_sources: int
    institutional_sources: int
    primary_observation_sources: int
    primary_evidence: int
    claims: int
    supporting_relations: int
    contradicting_relations: int
    accepted_independence_groups: tuple[str, ...]
    uncovered_languages: tuple[str, ...]
    coverage_gaps: tuple[str, ...]
    assessment: CountryMatrixAssessment | None


@dataclass(frozen=True)
class NationalInformationMatrix:
    event_id: str
    plan: CountryCoveragePlan
    config_matches: bool
    cells: tuple[CountryMatrixCell, ...]


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
