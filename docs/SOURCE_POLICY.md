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

`config/source_classes.yaml` and `examples/source.json` are illustrative
starting points, not an endorsement or reliability ranking of any source.
Source quality must be assessed in context and reviewed by an analyst.
