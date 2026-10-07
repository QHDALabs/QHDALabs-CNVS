# Development setup

Stage 2 establishes a minimal Python runtime and command-line entry point. The
CLI currently validates the reviewed MVP configuration; source collection,
event analysis, persistence, report generation and an API are not implemented.

## Requirements

- Python 3.12 or newer
- Internet access to install the declared PyYAML dependency (unless it is
  already available to pip)

No database, API credentials, service containers or `.env` file are required
for the current scaffold.

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
```

Configuration directory resolution is:

1. `--config-dir`, when provided;
2. `CNVS_CONFIG_DIR`, when set;
3. `./config` relative to the current working directory.

The CLI reports an actionable error and non-zero exit status if configuration
is missing or inconsistent. The current validation checks the exact approved
MVP country, language, event-type and source-type catalogs, unique
identifiers, country-to-language references, supported source access policy
and supported/out-of-scope category separation. Scope changes require updating
the requirements, configuration and validator together.

`CNVS_LOG_LEVEL` accepts `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`;
the default is `WARNING`. Environment values are read from the process
environment. CNVS does not implicitly load `.env` files. Do not put credentials
or private analyst data in tracked configuration.

The configuration is a reviewed MVP catalog; extend or change it only through
a documented scope change in `docs/MVP_REQUIREMENTS.md`.
