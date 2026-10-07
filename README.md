# QHDALabs Cross-National Verification System (CNVS)

CNVS is a human-in-the-loop research system for investigating events across
national information environments. It keeps observable facts, evidence,
source-attributed claims, attribution and analyst assessment distinct, while
recording where information originated and whether evidence chains are
independent.

> **Status: early MVP implementation.** The CLI can validate the reviewed
> configuration, manage a local SQLite database, collect approved public RSS
> feeds and URLs, and inspect immutable collection results. The checked-in
> source registry contains only a disabled synthetic example: no live source
> has been reviewed or approved. CNVS is not a verification engine or analyst
> application.

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
├── .gitignore
├── README.md
├── MANIFEST.md
├── ARCHITECTURE.md
├── AGENT.md              # Working guidance for AI coding agents
├── pyproject.toml        # Python 3.12+ package metadata and CLI entry point
├── requirements.txt      # Pinned runtime dependency for local setup and CI
├── log.log               # Completed work and roadmap to the MVP
├── docs/                 # Data model, development, MVP requirements and policies
├── CONTRIBUTING.md       # Contribution and validation guidance
├── LICENSE               # MIT License
├── SECURITY.md           # Private vulnerability reporting
├── .github/              # CI, Dependabot and contribution templates
├── config/               # MVP catalogs, source policy and source registry
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
├── src/cnvs/             # Python runtime, public RSS/URL intake and CLI
├── schemas/              # JSON Schemas for canonical records
├── tests/                # Fixture and schema consistency tests
└── examples/             # Synthetic event, source, claim, evidence and report records
```

The component directories are intentionally lightweight Python package
boundaries; no production pipeline or infrastructure dependency is implied.
Their responsibilities and the canonical record shapes are described in
[docs/DATA_MODEL.md](./docs/DATA_MODEL.md). Confirmed MVP scope and measurable
acceptance criteria are in [docs/MVP_REQUIREMENTS.md](./docs/MVP_REQUIREMENTS.md).

Completed repository work and the staged plan to reach the MVP are tracked in
[log.log](./log.log). Planned capabilities in that log are not implemented
unless their status is explicitly updated.

## Example records

The synthetic incident in `examples/` demonstrates the separation between an
event, an original source, a claim, and evidence. It is illustrative only and
does not describe a real event.

- [Event](./examples/event.json)
- [Source](./examples/source.json)
- [Claim](./examples/claim.json)
- [Evidence](./examples/evidence.json)
- [Assessment snapshot](./examples/assessment.json)
- [Analyst report](./examples/event-report.json)

The examples use stable IDs to link records. The event report keeps occurrence,
method and attribution confidence separate and states remaining gaps explicitly.

## Development setup

The project requires Python 3.12 or newer. From the repository root, create an
environment, install the package in editable mode, validate configuration and
run the tests:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --constraint requirements.txt --editable .
python -m cnvs config validate
python -m unittest discover -s tests -v
```

The `cnvs` command validates configuration, manages the local SQLite schema,
and collects approved public RSS/URL sources. Typed records, append-only
collection history, provenance identifiers and immutable snapshots are
available as runtime components. Event analysis and operational report
generation are not implemented. See
[docs/DEVELOPMENT.md](./docs/DEVELOPMENT.md) for platform-specific setup and
environment variable details.

## Configuration

The country, language, event-type and source-type files in `config/` define the
confirmed MVP starting scope; select country coverage according to each event.
Add sources to `config/source_registry.yaml` only after an analyst has reviewed
their event relevance, access conditions and collection constraints. The
checked-in entry is synthetic, pending review and disabled. Source classes and
scoring remain uncalibrated examples. Changes to MVP scope require documented
review. See [docs/SCORING.md](./docs/SCORING.md) and
[docs/SOURCE_POLICY.md](./docs/SOURCE_POLICY.md).

```powershell
python -m cnvs source list
python -m cnvs source collect SOURCE-ID
python -m cnvs source results
python -m cnvs source retry COLLECTION-ID
```

## Validation

The test runner uses Python's standard-library `unittest`. Install the project
first to make its YAML and JSON Schema runtime dependencies available:

```powershell
python -m pip install --constraint requirements.txt --editable .
python -m unittest discover -s tests -v
```

Tests check JSON Schema validity and parity, example references, the confirmed
MVP configuration, registry approval constraints, mocked RSS/URL collection,
robots and public-address restrictions, append-only collection results,
database migrations, record revision history, immutable source snapshots and
assessment reconstruction. They do not validate live source availability,
source rights, analyst conclusions or source approvals.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for contribution and validation
guidance. Repository-specific instructions for AI coding agents are in
[AGENT.md](./AGENT.md). GitHub Actions runs the fixture tests on supported
Python versions for pushes and pull requests.

## Security and analytical limitations

External material is untrusted data, never executable instructions. Automated
extraction is derived data and is not primary evidence. High-impact conclusions
require human review. See [SECURITY.md](./SECURITY.md),
[docs/SECURITY.md](./docs/SECURITY.md) and
[docs/ANALYST_GUIDE.md](./docs/ANALYST_GUIDE.md).

## License

This project is licensed under the [MIT License](./LICENSE).

## Project motto

> Do not count repetition as confirmation.
