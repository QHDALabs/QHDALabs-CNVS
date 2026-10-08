# Analyst Guide

This guide describes the intended review discipline for CNVS records. The CLI
supports versioned assessments and reviewed report snapshots; it does not
authenticate users or enforce analyst/internal-reader roles.

## Review sequence

1. Define the event and its time and geographic scope.
2. Separate direct observations from claims about those observations.
3. Trace claims to their earliest identifiable source.
4. Group dependent sources and evaluate independent evidence.
5. Compare country narratives, including omissions and differing attributions.
6. Record contradictions and evidence gaps.
7. Assess occurrence, method and attribution independently.
8. State what is known, unknown and assessed, with confidence and rationale.
9. Review the exact report snapshot and record a reasoned human decision before
   high-impact reports are approved for internal export.

## Reporting discipline

Use explicit labels for **FACT**, **CLAIM**, **ASSESSMENT**, **CONFIDENCE** and
**GAP**. Describe confidence as scoped to the relevant question. Never turn
repetition into confirmation or present a model-generated statement as primary
evidence.

Create an assessment record from validated JSON with
`cnvs assessment record --file assessment.json`, then create a report with
`cnvs report create ASSESSMENT-ID --high-impact`. Inspect the stored draft using
`cnvs report preview REPORT-ID` before recording an `APPROVED` or `REJECTED`
decision with `cnvs report review REPORT-ID --decision ... --reviewer ...
--rationale ...`. Approval records the reviewer label, timestamp and rationale
against that report's immutable content; the latest decision controls whether
local Markdown and HTML export is allowed. A later rejection blocks further
export of that report. The assessment's `human_reviewed` flag alone never
approves a report.

Report citations display source, claim and evidence identifiers and revisions.
Unknown confidence, unresolved attribution, contradictions, dependence and
gaps must remain visible; do not turn repetition or coverage counts into
independent confirmation. Reviewers should check the report's source and
evidence links, provenance/independence, timeline, country coverage and
limitations before approval.

Reviewer identity is currently a CLI-supplied label and is not authenticated.
This review log is an audit record, not proof of account identity or an
authorization boundary. The sample report in `examples/event-report.json`
demonstrates the distinctions; it is synthetic and is not an operational
assessment. Reports are for internal use; the CLI does not publish them
externally.

## Collection and processing failures

Before interpreting an empty or partial set of source documents, inspect
`cnvs source results` and `cnvs source metrics`. A failed or blocked collection
is not evidence that the source had no relevant reporting. Review the recorded
error and source access conditions before using either of the two permitted
manual retries; blocked results require source/access review and cannot be
retried. Normalization failures are recorded separately from successful
collection and must be resolved before treating extracted items as complete.
Metrics are aggregate diagnostics, not proof of coverage or an authenticated
audit trail.

For each investigation, distinguish source observations from claims and
assessments; trace claims to the earliest known origin; review duplicate,
syndication and independence decisions; retain contradictions, unknown
confidence and evidence gaps; separate occurrence, method and attribution;
and inspect event time separately from publication and collection time.
Missing or failed inputs must remain visible in the assessment and report
rather than being silently treated as negative evidence.
