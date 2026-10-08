# Source Policy

Every source record should preserve enough context to reconstruct where
information came from and how it entered the investigation.

## Minimum handling requirements

- Record the original URL or other origin identifier, publisher and source type.
- Preserve publication and collection times separately, including timezone
  when known.
- Record the earliest identifiable origin and any known syndication or
  quotation chain.
- Assign independence groups to evidence chains, not simply to individual
  websites or countries.
- Distinguish first-hand reporting, institutional statements, secondary
  reporting and narrative-only material.
- Keep corrections and source changes auditable; do not silently replace raw
  content.
- Note access limitations, translation and uncertainty.

## Independence

Multiple outlets repeating a single wire report or official statement remain
one dependent information chain unless separate evidence is demonstrated.
Uncertain dependencies should be marked uncertain rather than assumed
independent.

The provenance workflow records document-to-document `CITES`, `QUOTES`,
`SYNDICATED` and `DERIVED_FROM` links as analyst-reviewed proposals. Only
confirmed links form the established graph; duplicate and syndication
similarity candidates remain separate from that graph until reviewed. For a
confirmed syndication chain, existing accepted group assignments must not
conflict. If a newly proposed syndication link would connect different
accepted groups, resolve and re-review the assignments before confirming it.
The system does not infer group membership or calculate independent
confirmation counts.

`config/source_classes.yaml` and `examples/source.json` are illustrative
starting points, not an endorsement or reliability ranking of any source.
Source quality must be assessed in context and reviewed by an analyst.

## Registry and collection

`config/source_registry.yaml` is the local analyst-managed source registry.
Every entry records its publisher, source class, country, language, public
access method, event relevance, review state and collection constraints.
Collection is permitted only for an entry explicitly marked `approved` by a
named reviewer, with a timezone-aware review timestamp, and enabled.

The repository's registry entry is a synthetic, disabled fixture on a reserved
`.invalid` hostname. It is not an initial live-source recommendation or a
claim that an event-relevant source set has been approved. Add real sources
only after event-specific relevance, access terms, retention, privacy and
applicable legal requirements have been reviewed by the responsible analyst
and organization.

The collector uses only HTTP(S), rejects non-public destinations and
redirects, honors `robots.txt`, and applies per-source timeout, response-size
and media-type constraints. HTTP access restrictions and `robots.txt`
disallowances are recorded as blocked results, not bypassed. Collection does
not automatically retry; an analyst may manually retry a failed latest
attempt after reviewing it. Content is archived only when the registry says
it is permitted and provides a retention basis. Metadata and content hashes
remain in collection history when body archival is disabled.

This software cannot determine whether collection or retention is lawful. A
configuration review is not a substitute for legal/privacy approval, and
source conditions may change after review. Do not place credentials, access
tokens or personal analyst data in the registry.

## Normalization and dependency review

Normalization is derived data. Preserve the exact retained source snapshot and
the text extracted from it; store normalized text, canonical URLs, normalized
publisher names and parsed timestamps as separate values. Do not invent
publication timezones or replace an original language declaration when it
conflicts with registry metadata. Record unknown language and flag unresolved
language conflicts for review. A translation must retain a link to the source
text digest, source and target language, method, translator and timestamp.
Translation is not independent evidence.

Exact text matches and likely syndication/republication links are review
candidates, not source merges, truth determinations or automatic
independence assignments. Until an analyst records a rationale-bearing
decision, a possible copy relationship remains pending and must not be counted
as independent confirmation. Preserve both source records and their original
provenance, including when a candidate relationship is later rejected.
