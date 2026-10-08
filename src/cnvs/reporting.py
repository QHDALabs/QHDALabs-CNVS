import html
import re
import uuid
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from typing import TypeGuard
from urllib.parse import quote, urlsplit

from cnvs.models import (
    Assessment,
    Claim,
    Evidence,
    Event,
    JsonValue,
    NationalInformationMatrix,
    ReportSnapshot,
    Source,
)
from cnvs.storage import Database, ReconstructedAssessment, StorageError


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+.!|~-])")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Report snapshot dictionaries must have string keys.")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError(f"Unsupported report snapshot value: {type(value).__name__}.")


def _is_item(value: JsonValue) -> TypeGuard[dict[str, JsonValue]]:
    return isinstance(value, dict) and isinstance(value.get("text"), str)


def _safe_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _markdown_text(value: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value).replace("\r\n", "\n").replace("\r", "\n")


def render_markdown(snapshot: dict[str, JsonValue]) -> str:
    title = snapshot["title"]
    generated_at = snapshot["generated_at"]
    assessment_id = snapshot["assessment_id"]
    assessment_revision = snapshot["assessment_revision"]
    assert isinstance(title, str)
    assert isinstance(generated_at, str)
    assert isinstance(assessment_id, str)
    assert isinstance(assessment_revision, int)
    lines = [
        f"# {_markdown_text(title)}",
        "",
        f"Generated: `{_markdown_text(generated_at)}`",
        (
            f"Assessment snapshot: `{_markdown_text(assessment_id)}` "
            f"revision `{assessment_revision}`"
        ),
        "",
    ]
    sections = snapshot["sections"]
    assert isinstance(sections, list)
    for section in sections:
        assert isinstance(section, dict)
        heading = section["heading"]
        items = section["items"]
        assert isinstance(heading, str)
        assert isinstance(items, list)
        lines.extend((f"## {_markdown_text(heading)}", ""))
        if not items:
            lines.extend(("_No recorded items._", ""))
            continue
        for item in items:
            assert _is_item(item)
            text_value = item["text"]
            assert isinstance(text_value, str)
            text = _markdown_text(text_value)
            url = item.get("url")
            if isinstance(url, str) and _safe_url(url) is not None:
                safe_url = quote(url, safe=":/?#[]@!$&'*+,;=%")
                text = f"[{text}](<{safe_url}>)"
            lines.extend((f"- {text}",))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_html(snapshot: dict[str, JsonValue]) -> str:
    title = snapshot["title"]
    generated_at = snapshot["generated_at"]
    assessment_id = snapshot["assessment_id"]
    assessment_revision = snapshot["assessment_revision"]
    assert isinstance(title, str)
    assert isinstance(generated_at, str)
    assert isinstance(assessment_id, str)
    assert isinstance(assessment_revision, int)
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head><meta charset=\"utf-8\">",
        f"<title>{html.escape(title)}</title></head>",
        "<body>",
        f"<h1>{html.escape(title)}</h1>",
        "<dl>",
        f"<dt>Generated</dt><dd>{html.escape(generated_at)}</dd>",
        (
            "<dt>Assessment snapshot</dt>"
            f"<dd>{html.escape(assessment_id)} revision {assessment_revision}</dd>"
        ),
        "</dl>",
    ]
    sections = snapshot["sections"]
    assert isinstance(sections, list)
    for section in sections:
        assert isinstance(section, dict)
        heading = section["heading"]
        items = section["items"]
        assert isinstance(heading, str)
        assert isinstance(items, list)
        parts.append(f"<section><h2>{html.escape(heading)}</h2>")
        if items:
            parts.append("<ul>")
            for item in items:
                assert _is_item(item)
                text_value = item["text"]
                assert isinstance(text_value, str)
                text = html.escape(text_value)
                url = item.get("url")
                if isinstance(url, str) and (safe_url := _safe_url(url)) is not None:
                    text = f'<a href="{html.escape(safe_url, quote=True)}">{text}</a>'
                parts.append(f"<li>{text}</li>")
            parts.append("</ul>")
        else:
            parts.append("<p>No recorded items.</p>")
        parts.append("</section>")
    parts.extend(("</body>", "</html>", ""))
    return "\n".join(parts)


def _record_snapshot(reconstructed: ReconstructedAssessment) -> dict[str, JsonValue]:
    return {
        "revisions": [
            {
                "record_type": record_type,
                "record_id": record_id,
                "revision": revision,
            }
            for (record_type, record_id), revision in sorted(
                reconstructed.record_revisions.items()
            )
        ],
        "records": [
            _json_value(record)
            for _, record in sorted(reconstructed.records.items())
        ],
    }


def _linked_relations(
    database: Database, assessment: Assessment
) -> list[JsonValue]:
    claim_ids = set(assessment.claim_ids)
    evidence_ids = set(assessment.evidence_ids)
    relations = database.claim_evidence_links(
        event_id=assessment.event_id, review_status="LINKED"
    )
    return [
        _json_value(relation)
        for relation in relations
        if relation.claim_id in claim_ids and relation.evidence_id in evidence_ids
    ]


def _build_sections(
    database: Database,
    reconstructed: ReconstructedAssessment,
    *,
    generated_at: str,
    high_impact: bool,
    config_sha256: str,
) -> tuple[list[JsonValue], dict[str, JsonValue]]:
    assessment = reconstructed.assessment
    records = reconstructed.records
    event = records.get(("event", assessment.event_id))
    if not isinstance(event, Event):
        raise StorageError(
            f"Assessment {assessment.assessment_id} snapshot does not contain its event."
        )

    claims = [
        records.get(("claim", claim_id)) for claim_id in assessment.claim_ids
    ]
    evidence = [
        records.get(("evidence", evidence_id)) for evidence_id in assessment.evidence_ids
    ]
    if any(not isinstance(claim, Claim) for claim in claims):
        raise StorageError(
            f"Assessment {assessment.assessment_id} snapshot has a missing claim."
        )
    if any(not isinstance(item, Evidence) for item in evidence):
        raise StorageError(
            f"Assessment {assessment.assessment_id} snapshot has missing evidence."
        )

    sources = {
        record.source_id: record
        for (record_type, _), record in records.items()
        if record_type == "source" and isinstance(record, Source)
    }

    def revision_label(record_type: str, record_id: str) -> str:
        revision = reconstructed.record_revisions.get((record_type, record_id))
        return f"{record_id}@{revision}" if revision is not None else record_id

    all_matches = database.event_source_matches(event_id=assessment.event_id)
    matches = [
        match
        for match in all_matches
        if match.status == "LINKED" and match.source_id in sources
    ]
    unresolved_matches = [
        match
        for match in all_matches
        if match.status in {"PENDING", "UNRESOLVED"}
    ]
    documents = sorted({match.document_id for match in matches})
    provenance = [
        link
        for document_id in documents
        for link in database.provenance_graph(document_id)
    ]
    member_ids = (
        {("SOURCE", source_id) for source_id in sources}
        | {("EVIDENCE", item.evidence_id) for item in evidence if isinstance(item, Evidence)}
        | {("DOCUMENT", document_id) for document_id in documents}
    )
    independence = [
        assignment
        for member_type, member_id in sorted(member_ids)
        for assignment in database.independence_assignments(
            member_type=member_type,
            member_id=member_id,
            review_status="ACCEPTED",
        )
    ]
    timeline = database.timeline_entries(assessment.event_id)
    gaps = database.evidence_gaps(event_id=assessment.event_id)
    relations = _linked_relations(database, assessment)
    all_assessment_relations = [
        relation
        for relation in database.claim_evidence_links(
            event_id=assessment.event_id
        )
        if relation.claim_id in set(assessment.claim_ids)
        and relation.evidence_id in set(assessment.evidence_ids)
    ]
    unresolved_relations = [
        relation
        for relation in all_assessment_relations
        if relation.review_status in {"PENDING", "UNRESOLVED"}
    ]
    source_urls = {
        source_id: _safe_url(source.url) for source_id, source in sources.items()
    }

    fact_items = [
        {"text": fact} for fact in assessment.facts
    ]
    claim_items: list[dict[str, JsonValue]] = []
    for claim in claims:
        assert isinstance(claim, Claim)
        attribution = f"; attributed to {claim.attribution}" if claim.attribution else ""
        claim_items.append(
            {
                "text": (
                    f"{revision_label('claim', claim.claim_id)}: "
                    f"{claim.subject} {claim.predicate} "
                    f"{claim.object}{attribution}; type={claim.claim_type}; "
                    f"modality={claim.modality}; status={claim.status}; "
                    f"claim confidence={claim.confidence}; "
                    f"source={revision_label('source', claim.source_id)}"
                ),
                "url": source_urls.get(claim.source_id),
            }
        )
    evidence_items = []
    for item in evidence:
        assert isinstance(item, Evidence)
        evidence_items.append(
            {
                "text": (
                    f"{revision_label('evidence', item.evidence_id)}: "
                    f"{item.observation}; type={item.evidence_type}; "
                    f"directness={item.directness}; "
                    f"source={revision_label('source', item.source_id)}; "
                    f"recorded verification={item.verification_status}; "
                    f"effective verification={database.evidence_verification_status(item.evidence_id)}; "
                    f"independence group={item.independence_group}; "
                    f"collection time={item.collection_time}; event time={item.event_time or 'UNKNOWN'}"
                ),
                "url": source_urls.get(item.source_id),
            }
        )
    confidence = assessment.confidence
    confidence_items = [
        {"text": f"{dimension}: {confidence[dimension]}"}
        for dimension in ("occurrence", "method", "attribution")
    ]
    assessment_items = [
        {
            "text": (
                f"{event.title} ({event.event_id}); event status={event.status}; "
                f"assessment={assessment.assessment_id}; "
                f"pipeline version={assessment.pipeline_version}; "
                f"impact classification={'HIGH' if high_impact else 'STANDARD'}"
            )
        },
        {"text": assessment.assessment_text or "No narrative assessment was recorded."},
        {
            "text": (
                "The human_reviewed flag on the assessment is not treated as report approval; "
                "approval is recorded separately against this immutable report snapshot."
            )
        },
    ]
    contradiction_items = [{"text": text} for text in assessment.contradictions]
    contradiction_items.extend(
        {
            "text": (
                f"{item['claim_id']} {item['relationship']} {item['evidence_id']}; "
                f"link={item['link_id']}; rationale={item['rationale']}"
            )
        }
        for item in relations
        if isinstance(item, dict) and item.get("relationship") == "CONTRADICTS"
    )
    claim_evidence_items = [
        {
            "text": (
                f"{revision_label('claim', relation.claim_id)} "
                f"{relation.relationship} "
                f"{revision_label('evidence', relation.evidence_id)}; "
                f"reviewed by={relation.reviewed_by or 'UNKNOWN'}; "
                f"rationale={relation.review_rationale or relation.rationale}"
            )
        }
        for relation in all_assessment_relations
        if relation.review_status == "LINKED"
    ]
    gap_items = [{"text": text} for text in assessment.information_gaps]
    gap_items.extend(
        {
            "text": (
                f"{gap.gap_type} {gap.status}: {gap.description}; "
                f"gap={gap.gap_id}; claim={gap.claim_id or 'UNSPECIFIED'}; "
                f"rationale={gap.rationale}"
            )
        }
        for gap in gaps
    )
    source_items = [
        {
            "text": (
                f"{revision_label('source', source_id)}: {source.title}; "
                f"publisher={source.publisher}; "
                f"class={source.source_class}; language={source.language}; "
                f"published={source.published_at or 'UNKNOWN'}; "
                f"origin source={source.origin_source_id or 'UNKNOWN'}; "
                f"independence group={source.independence_group}; "
                f"URL={source.url}"
            ),
            "url": _safe_url(source.url),
        }
        for source_id, source in sorted(sources.items())
    ]
    source_items.extend(
        {
            "text": (
                f"linked document={match.document_id}; source={match.source_id}; "
                f"match={match.match_id}; reviewed by={match.reviewed_by or 'UNKNOWN'}; "
                f"review rationale={match.review_rationale or match.rationale}"
            )
        }
        for match in matches
    )
    source_items.extend(
        {
            "text": (
                f"{'confirmed' if link.review_status == 'CONFIRMED' else link.review_status.lower() + ' provenance proposal'} "
                f"{link.relationship_type} relationship: {link.document_id} -> "
                f"{link.upstream_document_id}; rationale={link.review_rationale or link.rationale}"
            )
        }
        for link in provenance
    )
    source_items.extend(
        {
            "text": (
                f"accepted independence assignment: {assignment.member_type} "
                f"{assignment.member_id} -> {assignment.group_id}; "
                f"rationale={assignment.review_rationale or assignment.rationale}"
            )
        }
        for assignment in independence
    )
    timeline_items = [
        {
            "text": (
                f"{entry.event_time or 'UNKNOWN event time'}; "
                f"timeline entry={entry.entry_id}@{entry.revision}; "
                f"document={entry.document_id}; "
                f"source={entry.source_id}; publisher={entry.publisher}; "
                f"title={entry.source_title}; published="
                f"{entry.normalized_publication_time or entry.original_publication_time or 'UNKNOWN'}; "
                f"collection={entry.collection_time}; event-time rationale="
                f"{entry.event_time_rationale}"
            ),
            "url": _safe_url(entry.source_url),
        }
        for entry in timeline
    ]
    record_revision_items = [
        {
            "text": f"{record_type} {record_id} revision {revision}"
        }
        for (record_type, record_id), revision
        in sorted(reconstructed.record_revisions.items())
    ]
    unresolved_source_items = [
        {
            "text": (
                f"{match.status} event/source match {match.match_id}: "
                f"document={match.document_id}; source={match.source_id}; "
                f"rationale={match.review_rationale or match.rationale}"
            )
        }
        for match in unresolved_matches
    ]
    unresolved_relation_items = [
        {
            "text": (
                f"{relation.review_status} claim/evidence relationship "
                f"{relation.link_id}: {relation.claim_id} "
                f"{relation.relationship} {relation.evidence_id}; "
                f"rationale={relation.review_rationale or relation.rationale}"
            )
        }
        for relation in unresolved_relations
    ]

    coverage_items: list[dict[str, JsonValue]] = []
    plans = database.country_coverage_plans(event_id=assessment.event_id)
    selected_plan = next((plan for plan in reversed(plans) if plan.status == "ACCEPTED"), None)
    matrix: NationalInformationMatrix | None = None
    if selected_plan is not None:
        matrix = database.national_information_matrix(
            event_id=assessment.event_id, config_sha256=config_sha256
        )
        coverage_items.append(
            {
                "text": (
                    f"accepted plan={selected_plan.plan_id} revision={selected_plan.revision}; "
                    f"configuration matches current catalog={matrix.config_matches}"
                )
            }
        )
        coverage_items.extend(
            {
                "text": (
                    f"{cell.country_code} {cell.country_name}: reporting documents="
                    f"{cell.reporting_documents}; reporting sources={cell.reporting_sources}; "
                    f"primary evidence={cell.primary_evidence}; claims={cell.claims}; "
                    f"supporting relations={cell.supporting_relations}; "
                    f"contradicting relations={cell.contradicting_relations}; "
                    f"uncovered languages={', '.join(cell.uncovered_languages) or 'none'}; "
                    f"coverage gaps={'; '.join(cell.coverage_gaps) or 'none'}"
                )
            }
            for cell in matrix.cells
        )
        for cell in matrix.cells:
            if cell.assessment is not None:
                coverage_items.append(
                    {
                        "text": (
                            f"{cell.country_code} analyst assessment "
                            f"{cell.assessment.assessment_id}: dominant frame="
                            f"{cell.assessment.dominant_frame}; "
                            f"attribution={cell.assessment.attribution_summary}; "
                            f"occurrence confidence={cell.assessment.occurrence_confidence}; "
                            f"method confidence={cell.assessment.method_confidence}; "
                            f"attribution confidence={cell.assessment.attribution_confidence}; "
                            f"omissions={cell.assessment.omissions}; "
                            f"contradictions={cell.assessment.contradictions}"
                        )
                    }
                )
        coverage_items.extend(
            {
                "text": (
                    f"{country.get('country', country.get('country_code', 'UNSPECIFIED'))} "
                    "outside catalog: "
                    f"{country.get('reason', country.get('rationale', 'No rationale recorded.'))}"
                )
            }
            for country in selected_plan.outside_catalog
        )
    else:
        coverage_items.append(
            {"text": "No accepted country coverage plan is recorded for this event."}
        )

    sections: list[JsonValue] = [
        _json_value({"heading": "FACT", "items": fact_items}),
        _json_value({"heading": "CLAIM", "items": claim_items}),
        _json_value({"heading": "ASSESSMENT", "items": assessment_items}),
        _json_value({"heading": "CONFIDENCE", "items": confidence_items}),
        _json_value({"heading": "CLAIM/EVIDENCE LINKS", "items": claim_evidence_items}),
        _json_value({"heading": "CONTRADICTIONS", "items": contradiction_items}),
        _json_value({"heading": "EVIDENCE", "items": evidence_items}),
        _json_value({"heading": "SOURCE PROVENANCE & INDEPENDENCE", "items": source_items}),
        _json_value({"heading": "TIMELINE", "items": timeline_items}),
        _json_value({"heading": "COUNTRY COVERAGE", "items": coverage_items}),
        _json_value({"heading": "RECORD REVISIONS", "items": record_revision_items}),
        _json_value({"heading": "UNRESOLVED SOURCE LINKS", "items": unresolved_source_items}),
        _json_value({"heading": "UNRESOLVED EVIDENCE LINKS", "items": unresolved_relation_items}),
        _json_value({"heading": "GAP", "items": gap_items}),
    ]
    snapshot: dict[str, JsonValue] = {
        "title": f"Analyst report: {event.title}",
        "report_event_id": event.event_id,
        "assessment_id": assessment.assessment_id,
        "assessment_revision": reconstructed.revision,
        "generated_at": generated_at,
        "high_impact": high_impact,
        "sections": sections,
    }
    input_snapshot: dict[str, JsonValue] = {
        "report": snapshot,
        "canonical_assessment_snapshot": {
            "assessment": _json_value(assessment),
            **_record_snapshot(reconstructed),
        },
        "linked_claim_evidence_relations": relations,
        "assessment_claim_evidence_relations": [
            _json_value(relation) for relation in all_assessment_relations
        ],
        "event_source_matches": [_json_value(match) for match in all_matches],
        "provenance_links": [_json_value(link) for link in provenance],
        "independence_assignments": [_json_value(item) for item in independence],
        "timeline": [_json_value(item) for item in timeline],
        "evidence_gaps": [_json_value(item) for item in gaps],
        "country_coverage_plan": _json_value(selected_plan) if selected_plan else None,
        "country_matrix": _json_value(matrix) if matrix else None,
    }
    return sections, input_snapshot


def create_report(
    database: Database,
    *,
    assessment_id: str,
    assessment_revision: int | None,
    high_impact: bool,
    config_sha256: str,
    generated_at: str | None = None,
) -> ReportSnapshot:
    reconstructed = database.reconstruct_assessment(
        assessment_id, revision=assessment_revision
    )
    actual_revision = reconstructed.revision
    timestamp = generated_at or _utc_now()
    sections, input_snapshot = _build_sections(
        database,
        reconstructed,
        generated_at=timestamp,
        high_impact=high_impact,
        config_sha256=config_sha256,
    )
    report_id = uuid.uuid4().hex
    report_snapshot = input_snapshot["report"]
    assert isinstance(report_snapshot, dict)
    report_snapshot["assessment_revision"] = actual_revision
    report_snapshot["report_id"] = report_id
    rendered_snapshot = {
        "title": report_snapshot["title"],
        "generated_at": timestamp,
        "assessment_id": assessment_id,
        "assessment_revision": actual_revision,
        "sections": sections,
    }
    markdown = render_markdown(rendered_snapshot)
    html_content = render_html(rendered_snapshot)
    return database.create_report_snapshot(
        report_id=report_id,
        event_id=reconstructed.assessment.event_id,
        assessment_id=assessment_id,
        assessment_revision=actual_revision,
        high_impact=high_impact,
        generated_at=timestamp,
        input_snapshot=input_snapshot,
        markdown=markdown,
        html=html_content,
    )
