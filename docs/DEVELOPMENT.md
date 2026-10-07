# Development setup

The Python CLI validates the reviewed MVP configuration, manages local SQLite
persistence, and supports explicitly approved public RSS/URL collection.
Automated event analysis, report generation, authentication and an API are not
implemented.

## Requirements

- Python 3.12 or newer
- Internet access to install the declared dependencies (unless already
  available to pip)

Live collection requires outbound DNS and HTTP(S) access to the configured
public source and its `robots.txt`. No API credentials, service containers or
`.env` file are required.

## Windows PowerShell

From the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --constraint requirements.txt --editable .
python -m cnvs config validate
python -m unittest discover -s tests -v
```

If Python is installed at a known path, use that interpreter to create the
virtual environment, for example:

```powershell
C:\Python312\python.exe -m venv .venv
```

## Linux and macOS

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --constraint requirements.txt --editable .
python -m cnvs config validate
python -m unittest discover -s tests -v
```

The checked-in `requirements.txt` pins the runtime dependency used for local
development and CI. Editable installation registers the `cnvs` command and
installs the project dependencies declared in `pyproject.toml`. The test suite
uses Python's standard-library `unittest`; no separate test package is needed.

## CLI and configuration

```text
cnvs --version
cnvs config validate
cnvs config validate --config-dir path/to/config
cnvs source list
cnvs source collect SOURCE-ID
cnvs source results
cnvs source results --source-id SOURCE-ID
cnvs source retry COLLECTION-ID
```

Configuration directory resolution is:

1. `--config-dir`, when provided;
2. `CNVS_CONFIG_DIR`, when set;
3. `./config` relative to the current working directory.

The CLI reports an actionable error and non-zero exit status if configuration
is missing or inconsistent. Validation checks the approved MVP country,
language, event-type and source-type catalogs, the registry schema and
country/language relationships, unique IDs, source review state and collection
constraints. A registry entry must be explicitly analyst-approved and enabled
before collection; the checked-in synthetic entry is pending and disabled.
Scope changes require updating the requirements, configuration and validator
together.

### Source registry and collection

Configure sources in `config/source_registry.yaml`. Each entry records its
publisher, source class, MVP country and language, public access method (`RSS`
or `URL`), event relevance, review metadata and explicit collection limits.
`review.status: approved`, a named reviewer, a timezone-aware `reviewed_at`,
and `enabled: true` are required before `source collect` will make a network
request. Source approval is an analyst attestation, not a legal determination.
Never put credentials or tokens in source URLs.

The collector accepts only HTTP(S), checks that each destination and redirect
resolves to public IP addresses, limits redirects, download size and time, and
honors `robots.txt`. It does not bypass access controls, retry automatically,
or fetch individual RSS articles. A URL collection stores its original and
final URL as origin identifiers; RSS collection also records feed item IDs
and links. Each attempt has an immutable database result with status,
timestamps, HTTP/media metadata, digest, failure details and optional snapshot
reference, together with a snapshot of the registry metadata used for that
attempt. `source retry` is manual and only accepts a failed latest attempt;
blocked attempts require source/access review rather than retrying. Results
remain inspectable even if the registry later changes.

Set `archive_content: true` only when applicable access terms permit retaining
the content and provide a `retention_basis`. When archiving is disabled, CNVS
stores provenance metadata and a digest but not the fetched body. These
controls do not provide legal advice, retention/deletion administration or
protection against a source's terms changing after review.

## Local database

Stages 3-4 use SQLite and apply checksummed migrations automatically when the
database is opened. The default database path is `.data/cnvs.sqlite3`; override
it with `CNVS_DATABASE_PATH` or `--database` on a database command:

```powershell
python -m cnvs db migrate
python -m cnvs db status
python -m cnvs db status --database .data/research.sqlite3
```

Database files are local runtime data and are excluded from Git. Back up and
protect them according to the sensitivity and retention of the records they
contain. This storage layer does not implement authentication, role-based
permissions, administrator deletion workflows, encryption at rest or a
production backup policy.

Canonical JSON Schema records are validated before storage, and database
revisions preserve prior record payloads. Raw snapshots are content-addressed;
collection attempts are append-only and immutable. `cnvs db status` shows the
applied schema migrations; changed migration files are rejected and require a
new migration instead.

`CNVS_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`;
the default is `WARNING`. Environment values are read from the process
environment. CNVS does not implicitly load `.env` files. Do not put credentials
or private analyst data in tracked configuration.

The configuration is a reviewed MVP catalog; extend or change it only through
a documented scope change in `docs/MVP_REQUIREMENTS.md`.
