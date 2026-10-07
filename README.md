# QHDALabs Cross-National Verification System (CNVS)

CNVS is a human-in-the-loop research system for investigating events across
national information environments. It keeps observable facts, evidence,
source-attributed claims, attribution and analyst assessment distinct, while
recording where information originated and whether evidence chains are
independent.

> **Status: architecture scaffold.** This repository currently contains the
> project principles, architecture, initial configuration examples, data
> schemas and sample analytical records. It is not yet an operational ingestion
> service, verification engine or analyst application.

## Guiding principles

- Repetition is not independent confirmation.
- Preserve raw sources and their provenance.
- Keep observations, evidence, claims and assessments as separate records.
- Treat attribution as unresolved until evidence supports a conclusion.
- Represent uncertainty, contradictions and information gaps explicitly.
- Use automated analysis to support, not replace, human analysts.

See [MANIFEST.md](./MANIFEST.md) for the mission and analytical principles, and
[ARCHITECTURE.md](./ARCHITECTURE.md) for the proposed system design and
implementation phases.

## Repository layout

```text
.
├── README.md
├── MANIFEST.md
├── ARCHITECTURE.md
├── docs/                 # Data model, scoring, source policy, analyst and security guides
├── config/               # Example country, language, source-class and scoring settings
├── ingestion/            # Source collection boundaries
├── normalization/        # Language and record normalization boundaries
├── extraction/           # Claim and attribution extraction boundaries
├── provenance/           # Source lineage and independence relationships
├── evidence/             # Evidence records and verification workflow
├── analysis/             # Event, country and narrative analysis
├── scoring/              # Explainable analytical indicators
├── retrieval/            # Search and retrieval integrations
├── reporting/            # Analyst-reviewed reports
├── api/                  # Future REST API boundary
├── workers/              # Future asynchronous processing boundary
├── ui/                   # Future analyst interface boundary
├── schemas/              # JSON Schemas for canonical records
├── tests/                # Fixture and schema consistency tests
└── examples/             # Synthetic event, source, claim, evidence and report records
```

The component directories are intentionally lightweight Python package
boundaries; no production pipeline or infrastructure dependency is implied.
Their responsibilities and the canonical record shapes are described in
[docs/DATA_MODEL.md](./docs/DATA_MODEL.md).

## Example records

The synthetic incident in `examples/` demonstrates the separation between an
event, an original source, a claim, and evidence. It is illustrative only and
does not describe a real event.

- [Event](./examples/event.json)
- [Source](./examples/source.json)
- [Claim](./examples/claim.json)
- [Evidence](./examples/evidence.json)
- [Analyst report](./examples/event-report.json)

The examples use stable IDs to link records. The event report keeps occurrence,
method and attribution confidence separate and states remaining gaps explicitly.

## Configuration

Configuration files under `config/` are starting examples, not authoritative
country coverage, source ratings or calibrated scoring policy. Country selection
must be driven by the event, and scoring changes require documented review.
See [docs/SCORING.md](./docs/SCORING.md) and
[docs/SOURCE_POLICY.md](./docs/SOURCE_POLICY.md).

## Validation

The current fixture tests use only the Python standard library:

```powershell
python -m unittest discover -s tests -v
```

The tests check JSON syntax, required top-level fields and cross-record
references. They do not validate a live pipeline or analyst conclusions.

## Security and analytical limitations

External material is untrusted data, never executable instructions. Automated
extraction is derived data and is not primary evidence. High-impact conclusions
require human review. See [docs/SECURITY.md](./docs/SECURITY.md) and
[docs/ANALYST_GUIDE.md](./docs/ANALYST_GUIDE.md).

## Project motto

> Do not count repetition as confirmation.
