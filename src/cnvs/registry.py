import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from typing import Any

import yaml

from cnvs.configuration import MVP_COUNTRIES, MVP_LANGUAGE_CODES, MVP_SOURCE_TYPES


REGISTRY_FILENAME = "source_registry.yaml"
SOURCE_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{1,63}$")
RFC3339_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})$"
)
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 60
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "key",
        "pass",
        "password",
        "pwd",
        "secret",
        "session",
        "signature",
        "sig",
        "token",
    }
)


class RegistryError(ValueError):
    """Raised when a configured source registry entry is invalid."""


@dataclass(frozen=True)
class CollectionConstraints:
    max_bytes: int
    allowed_content_types: tuple[str, ...]
    archive_content: bool
    retention_basis: str | None
    respect_robots_txt: bool
    timeout_seconds: int


@dataclass(frozen=True)
class SourceRegistration:
    source_id: str
    publisher: str
    source_class: str
    country: str
    language: str
    access_method: str
    url: str
    event_relevance: str
    review_status: str
    reviewer: str | None
    reviewed_at: str | None
    enabled: bool
    constraints: CollectionConstraints

    @property
    def approved(self) -> bool:
        return self.review_status == "approved" and self.enabled


def _required_text(entry: dict[str, Any], key: str, source_id: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{REGISTRY_FILENAME}: source {source_id}: {key} must be non-empty.")
    return value.strip()


def _validate_public_url(url: str, source_id: str) -> None:
    if any(character.isspace() or ord(character) < 32 for character in url):
        raise RegistryError(
            f"{REGISTRY_FILENAME}: source {source_id}: URL cannot contain whitespace "
            "or control characters."
        )
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise RegistryError(
            f"{REGISTRY_FILENAME}: source {source_id}: invalid URL: {error}."
        ) from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "%" in parsed.hostname
    ):
        raise RegistryError(
            f"{REGISTRY_FILENAME}: source {source_id}: URL must be a public HTTP(S) "
            "URL without credentials or a fragment."
        )
    if any(
        key.lower() in SENSITIVE_QUERY_KEYS
        for key, _ in parse_qsl(parsed.query)
    ):
        raise RegistryError(
            f"{REGISTRY_FILENAME}: source {source_id}: credentials and tokens "
            "must not be embedded in URLs."
        )
    if port is not None and not 1 <= port <= 65535:
        raise RegistryError(f"{REGISTRY_FILENAME}: source {source_id}: invalid URL port.")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in {"localhost"} or hostname.endswith((".localhost", ".local", ".internal")):
        raise RegistryError(
            f"{REGISTRY_FILENAME}: source {source_id}: local hostnames are not allowed."
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise RegistryError(
            f"{REGISTRY_FILENAME}: source {source_id}: URL address must be public."
        )


def load_source_registry(config_dir: Path) -> list[SourceRegistration]:
    """Load the YAML registry and reject entries outside the reviewed MVP scope."""
    path = config_dir / REGISTRY_FILENAME
    try:
        with path.open(encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except FileNotFoundError as error:
        raise RegistryError(f"Required configuration file is missing: {REGISTRY_FILENAME}.") from error
    except yaml.YAMLError as error:
        raise RegistryError(f"Configuration file {REGISTRY_FILENAME} is not valid YAML.") from error

    if not isinstance(document, dict) or document.get("schema_version") != "0.1":
        raise RegistryError(f"{REGISTRY_FILENAME}: schema_version must be '0.1'.")
    entries = document.get("sources")
    if not isinstance(entries, list):
        raise RegistryError(f"{REGISTRY_FILENAME}: sources must be a list.")

    source_classes_path = config_dir / "source_classes.yaml"
    try:
        with source_classes_path.open(encoding="utf-8") as stream:
            classes_document = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as error:
        raise RegistryError("Unable to load source_classes.yaml.") from error
    classes = classes_document.get("source_classes") if isinstance(classes_document, dict) else None
    if not isinstance(classes, list):
        raise RegistryError("source_classes.yaml: source_classes must be a list.")
    allowed_classes = {
        item.get("id")
        for item in classes
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if not allowed_classes:
        raise RegistryError("source_classes.yaml: no valid source classes are configured.")

    registrations: list[SourceRegistration] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RegistryError(f"{REGISTRY_FILENAME}: each source must be a mapping.")
        source_id = entry.get("id")
        if not isinstance(source_id, str) or SOURCE_ID_PATTERN.fullmatch(source_id) is None:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: each source id must be 2-64 uppercase "
                "letters, digits, dots, underscores or hyphens."
            )
        if source_id in seen_ids:
            raise RegistryError(f"{REGISTRY_FILENAME}: duplicate source id {source_id}.")
        seen_ids.add(source_id)

        publisher = _required_text(entry, "publisher", source_id)
        source_class = _required_text(entry, "source_class", source_id)
        if source_class not in allowed_classes:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: unknown source_class {source_class!r}."
            )
        country = _required_text(entry, "country", source_id)
        if country not in MVP_COUNTRIES:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: country is outside the MVP catalog."
            )
        language = _required_text(entry, "language", source_id)
        if language not in MVP_LANGUAGE_CODES or language not in MVP_COUNTRIES[country][1]:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: language is not supported "
                f"for country {country}."
            )
        access_method = _required_text(entry, "access_method", source_id)
        if access_method not in MVP_SOURCE_TYPES:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: access_method must be RSS or URL."
            )
        url = _required_text(entry, "url", source_id)
        _validate_public_url(url, source_id)
        event_relevance = _required_text(entry, "event_relevance", source_id)

        review = entry.get("review")
        if not isinstance(review, dict):
            raise RegistryError(f"{REGISTRY_FILENAME}: source {source_id}: review must be a mapping.")
        review_status = review.get("status")
        if not isinstance(review_status, str) or review_status not in {
            "pending",
            "approved",
            "rejected",
        }:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: review.status must be "
                "pending, approved or rejected."
            )
        reviewer, reviewed_at = review.get("reviewer"), review.get("reviewed_at")
        if review_status == "approved":
            if not isinstance(reviewer, str) or not reviewer.strip():
                raise RegistryError(
                    f"{REGISTRY_FILENAME}: approved source {source_id} needs a reviewer."
                )
            if not isinstance(reviewed_at, str):
                raise RegistryError(
                    f"{REGISTRY_FILENAME}: approved source {source_id} needs reviewed_at."
                )
            if RFC3339_TIMESTAMP_PATTERN.fullmatch(reviewed_at) is None:
                raise RegistryError(
                    f"{REGISTRY_FILENAME}: source {source_id}: reviewed_at must be RFC3339."
                )
            try:
                timestamp = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
            except ValueError as error:
                raise RegistryError(
                    f"{REGISTRY_FILENAME}: source {source_id}: reviewed_at must be RFC3339."
                ) from error
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise RegistryError(
                    f"{REGISTRY_FILENAME}: source {source_id}: reviewed_at must include a timezone."
                )
        elif reviewer is not None or reviewed_at is not None:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: unapproved source {source_id} cannot claim a reviewer."
            )

        enabled = entry.get("enabled")
        if not isinstance(enabled, bool):
            raise RegistryError(f"{REGISTRY_FILENAME}: source {source_id}: enabled must be boolean.")
        if enabled and review_status != "approved":
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id} must be approved before it is enabled."
            )

        constraints_doc = entry.get("constraints")
        if not isinstance(constraints_doc, dict):
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: constraints must be a mapping."
            )
        max_bytes = constraints_doc.get("max_bytes")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_SOURCE_BYTES:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: max_bytes must be 1.."
                f"{MAX_SOURCE_BYTES}."
            )
        timeout_seconds = constraints_doc.get("timeout_seconds")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: timeout_seconds must be 1.."
                f"{MAX_TIMEOUT_SECONDS}."
            )
        content_types = constraints_doc.get("allowed_content_types")
        if (
            not isinstance(content_types, list)
            or not content_types
            or any(
                not isinstance(value, str)
                or value.count("/") != 1
                or "*" in value
                or value != value.strip().lower()
                or any(character.isspace() for character in value)
                for value in content_types
            )
            or len(set(content_types)) != len(content_types)
        ):
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: allowed_content_types "
                "must be unique lowercase media types."
            )
        if access_method == "RSS" and not (
            set(content_types)
            & {
                "application/atom+xml",
                "application/rss+xml",
                "application/xml",
                "text/rss+xml",
                "text/xml",
            }
        ):
            raise RegistryError(
                f"{REGISTRY_FILENAME}: RSS source {source_id} must allow an RSS/Atom "
                "or XML media type."
            )
        archive_content = constraints_doc.get("archive_content")
        if not isinstance(archive_content, bool):
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: archive_content must be boolean."
            )
        retention_basis = constraints_doc.get("retention_basis")
        if archive_content and (
            not isinstance(retention_basis, str) or not retention_basis.strip()
        ):
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: archiving needs a documented "
                "retention_basis."
            )
        if not archive_content and retention_basis is not None:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: retention_basis must be null "
                "when content is not archived."
            )
        if constraints_doc.get("respect_robots_txt") is not True:
            raise RegistryError(
                f"{REGISTRY_FILENAME}: source {source_id}: respect_robots_txt must be true."
            )

        registrations.append(
            SourceRegistration(
                source_id=source_id,
                publisher=publisher,
                source_class=source_class,
                country=country,
                language=language,
                access_method=access_method,
                url=url,
                event_relevance=event_relevance,
                review_status=review_status,
                reviewer=reviewer.strip() if isinstance(reviewer, str) else None,
                reviewed_at=reviewed_at,
                enabled=enabled,
                constraints=CollectionConstraints(
                    max_bytes=max_bytes,
                    allowed_content_types=tuple(content_types),
                    archive_content=archive_content,
                    retention_basis=retention_basis,
                    respect_robots_txt=True,
                    timeout_seconds=timeout_seconds,
                ),
            )
        )
    return registrations
