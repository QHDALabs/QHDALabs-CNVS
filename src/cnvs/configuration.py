from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


CONFIG_FILES = (
    "countries.yaml",
    "languages.yaml",
    "event_types.yaml",
    "source_types.yaml",
    "source_classes.yaml",
    "source_registry.yaml",
)
MVP_COUNTRIES = {
    "PL": ("Poland", frozenset({"pl"})),
    "DE": ("Germany", frozenset({"de"})),
    "FR": ("France", frozenset({"fr"})),
    "GB": ("United Kingdom", frozenset({"en"})),
    "FI": ("Finland", frozenset({"fi"})),
    "EE": ("Estonia", frozenset({"et"})),
    "UA": ("Ukraine", frozenset({"uk", "ru"})),
    "MD": ("Moldova", frozenset({"ro", "ru"})),
    "GE": ("Georgia", frozenset({"ka", "ru"})),
    "BY": ("Belarus", frozenset({"be", "ru"})),
    "RU": ("Russia", frozenset({"ru"})),
    "US": ("United States", frozenset({"en"})),
}
MVP_COUNTRY_CODES = frozenset(MVP_COUNTRIES)
MVP_LANGUAGES = {
    "pl": "Polish",
    "de": "German",
    "fr": "French",
    "en": "English",
    "fi": "Finnish",
    "et": "Estonian",
    "uk": "Ukrainian",
    "ro": "Romanian",
    "ka": "Georgian",
    "be": "Belarusian",
    "ru": "Russian",
}
MVP_LANGUAGE_CODES = frozenset(MVP_LANGUAGES)
MVP_EVENT_TYPES = frozenset(
    {
        "MILITARY",
        "SABOTAGE",
        "INFRASTRUCTURE",
        "CYBER",
        "MARITIME",
        "AVIATION",
        "DIPLOMATIC",
        "POLITICAL",
        "ENERGY",
    }
)
MVP_OUT_OF_SCOPE_EVENT_TYPES = frozenset({"INDUSTRIAL", "NATURAL", "OTHER"})
MVP_SOURCE_TYPES = frozenset({"RSS", "URL"})
MVP_OUT_OF_SCOPE_SOURCE_TYPES = frozenset(
    {"SOCIAL_MEDIA", "AUTHENTICATED", "PAYWALLED", "THIRD_PARTY_API"}
)


class ConfigurationError(ValueError):
    """Raised when a CNVS runtime configuration is missing or inconsistent."""


@dataclass(frozen=True)
class ConfigurationSummary:
    country_count: int
    language_count: int
    event_type_count: int
    source_type_count: int


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
    except FileNotFoundError as error:
        raise ConfigurationError(f"Required configuration file is missing: {path.name}.") from error
    except yaml.YAMLError as error:
        raise ConfigurationError(f"Configuration file {path.name} is not valid YAML.") from error

    if not isinstance(value, dict):
        raise ConfigurationError(f"Configuration file {path.name} must contain a YAML mapping.")
    return value


def _unique_values(values: list[Any], *, field: str, filename: str) -> set[str]:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ConfigurationError(f"{filename}: {field} entries must be non-empty strings.")
    if len(values) != len(set(values)):
        raise ConfigurationError(f"{filename}: {field} entries must be unique.")
    return set(values)


def _read_string_list(
    document: dict[str, Any], *, key: str, filename: str
) -> list[str]:
    values = document.get(key)
    if not isinstance(values, list):
        raise ConfigurationError(f"{filename}: {key} must be a list.")
    if not values:
        raise ConfigurationError(f"{filename}: {key} must not be empty.")
    _unique_values(values, field=key, filename=filename)
    return values


def _require_exact_set(
    actual: set[str], expected: frozenset[str], *, filename: str, field: str
) -> None:
    if actual == expected:
        return
    missing = ", ".join(sorted(expected - actual)) or "none"
    unexpected = ", ".join(sorted(actual - expected)) or "none"
    raise ConfigurationError(
        f"{filename}: {field} do not match the approved MVP scope "
        f"(missing: {missing}; unexpected: {unexpected})."
    )


def validate_configuration(config_dir: Path) -> ConfigurationSummary:
    """Load and validate the reviewed MVP runtime configuration set."""
    config = {name: _load_mapping(config_dir / name) for name in CONFIG_FILES}

    for filename, document in config.items():
        if document.get("schema_version") != "0.1":
            raise ConfigurationError(f"{filename}: schema_version must be '0.1'.")

    language_entries = config["languages.yaml"].get("languages")
    if not isinstance(language_entries, list) or not language_entries:
        raise ConfigurationError("languages.yaml: languages must be a non-empty list.")
    language_codes: list[str] = []
    for entry in language_entries:
        if not isinstance(entry, dict):
            raise ConfigurationError("languages.yaml: each language must be a mapping.")
        code, name = entry.get("code"), entry.get("name")
        if not isinstance(code, str) or not code.strip():
            raise ConfigurationError("languages.yaml: each language needs a non-empty code.")
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError(f"languages.yaml: language {code!r} needs a name.")
        language_codes.append(code)
    language_code_set = _unique_values(
        language_codes, field="language codes", filename="languages.yaml"
    )
    _require_exact_set(
        language_code_set,
        MVP_LANGUAGE_CODES,
        filename="languages.yaml",
        field="language codes",
    )
    language_names = {entry["code"]: entry["name"] for entry in language_entries}
    if language_names != MVP_LANGUAGES:
        raise ConfigurationError(
            "languages.yaml: language names must match the approved MVP catalog."
        )

    country_entries = config["countries.yaml"].get("countries")
    if not isinstance(country_entries, list) or not country_entries:
        raise ConfigurationError("countries.yaml: countries must be a non-empty list.")
    country_codes: list[str] = []
    for entry in country_entries:
        if not isinstance(entry, dict):
            raise ConfigurationError("countries.yaml: each country must be a mapping.")
        code, name, languages = (
            entry.get("code"),
            entry.get("name"),
            entry.get("languages"),
        )
        if (
            not isinstance(code, str)
            or len(code) != 2
            or not code.isascii()
            or not code.isalpha()
            or not code.isupper()
        ):
            raise ConfigurationError(
                "countries.yaml: each country code must be a two-letter uppercase code."
            )
        if not isinstance(name, str) or not name.strip():
            raise ConfigurationError(f"countries.yaml: country {code} needs a name.")
        if not isinstance(languages, list) or not languages:
            raise ConfigurationError(
                f"countries.yaml: country {code} needs at least one language code."
            )
        _unique_values(languages, field=f"languages for {code}", filename="countries.yaml")
        unknown_languages = set(languages) - language_code_set
        if unknown_languages:
            unknown = ", ".join(sorted(unknown_languages))
            raise ConfigurationError(
                f"countries.yaml: country {code} references unknown languages: {unknown}."
            )
        expected_name, expected_languages = MVP_COUNTRIES.get(code, (None, frozenset()))
        if name != expected_name or set(languages) != expected_languages:
            raise ConfigurationError(
                f"countries.yaml: country {code} name and languages must match "
                "the approved MVP catalog."
            )
        country_codes.append(code)
    country_code_set = _unique_values(
        country_codes, field="country codes", filename="countries.yaml"
    )
    _require_exact_set(
        country_code_set,
        MVP_COUNTRY_CODES,
        filename="countries.yaml",
        field="country codes",
    )

    events = config["event_types.yaml"]
    if events.get("scope") != "mvp":
        raise ConfigurationError("event_types.yaml: scope must be 'mvp'.")
    supported_events = _read_string_list(
        events, key="supported_event_types", filename="event_types.yaml"
    )
    deferred_events = _read_string_list(
        events, key="out_of_scope_event_types", filename="event_types.yaml"
    )
    _require_exact_set(
        set(supported_events),
        MVP_EVENT_TYPES,
        filename="event_types.yaml",
        field="supported_event_types",
    )
    _require_exact_set(
        set(deferred_events),
        MVP_OUT_OF_SCOPE_EVENT_TYPES,
        filename="event_types.yaml",
        field="out_of_scope_event_types",
    )
    if set(supported_events) & set(deferred_events):
        raise ConfigurationError(
            "event_types.yaml: supported and out-of-scope event types must not overlap."
        )

    sources = config["source_types.yaml"]
    if sources.get("scope") != "mvp":
        raise ConfigurationError("source_types.yaml: scope must be 'mvp'.")
    source_entries = sources.get("supported_source_types")
    if not isinstance(source_entries, list) or not source_entries:
        raise ConfigurationError(
            "source_types.yaml: supported_source_types must be a non-empty list."
        )
    source_types: list[str] = []
    for entry in source_entries:
        if not isinstance(entry, dict):
            raise ConfigurationError(
                "source_types.yaml: each supported source type must be a mapping."
            )
        source_id, access = entry.get("id"), entry.get("access")
        if not isinstance(source_id, str) or source_id not in {"RSS", "URL"} or access != "PUBLIC":
            raise ConfigurationError(
                "source_types.yaml: MVP sources must be public RSS or URL sources."
            )
        source_types.append(source_id)
    _unique_values(source_types, field="source type IDs", filename="source_types.yaml")
    _require_exact_set(
        set(source_types),
        MVP_SOURCE_TYPES,
        filename="source_types.yaml",
        field="supported_source_types",
    )

    out_of_scope_sources = _read_string_list(
        sources, key="out_of_scope_source_types", filename="source_types.yaml"
    )
    _require_exact_set(
        set(out_of_scope_sources),
        MVP_OUT_OF_SCOPE_SOURCE_TYPES,
        filename="source_types.yaml",
        field="out_of_scope_source_types",
    )
    if set(source_types) & set(out_of_scope_sources):
        raise ConfigurationError(
            "source_types.yaml: supported and out-of-scope source types must not overlap."
        )
    policy = sources.get("policy")
    if not isinstance(policy, dict):
        raise ConfigurationError("source_types.yaml: policy must be a mapping.")
    if policy.get("bypass_access_controls") is not False:
        raise ConfigurationError(
            "source_types.yaml: bypass_access_controls must be false."
        )
    if policy.get("archive_content_only_when_permitted") is not True:
        raise ConfigurationError(
            "source_types.yaml: archive_content_only_when_permitted must be true."
        )

    from cnvs.registry import RegistryError, load_source_registry

    try:
        load_source_registry(config_dir)
    except RegistryError as error:
        raise ConfigurationError(str(error)) from error
    return ConfigurationSummary(
        country_count=len(country_codes),
        language_count=len(language_code_set),
        event_type_count=len(supported_events),
        source_type_count=len(source_types),
    )
