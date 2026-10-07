import json
import re
from datetime import datetime
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from cnvs.models import RECORD_TYPES, CanonicalRecord, record_from_payload, record_to_payload


SCHEMA_VERSION = "0.1"
SCHEMA_FILES = {
    "event": "event.schema.json",
    "source": "source.schema.json",
    "claim": "claim.schema.json",
    "evidence": "evidence.schema.json",
    "assessment": "assessment.schema.json",
}
FORMAT_CHECKER = FormatChecker()
RFC3339_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:\d{2})$"
)


@FORMAT_CHECKER.checks("date-time", raises=(ValueError, OverflowError))
def _is_rfc3339_datetime(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if RFC3339_DATETIME.fullmatch(value) is None:
        return False
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


class RecordValidationError(ValueError):
    """Raised when a canonical record does not satisfy its versioned schema."""


def _load_schema(record_type: str) -> dict[str, Any]:
    filename = SCHEMA_FILES.get(record_type)
    if filename is None:
        raise RecordValidationError(f"Unsupported canonical record type: {record_type}.")
    schema_path = files("cnvs").joinpath("schemas", filename)
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecordValidationError(f"Unable to load schema for {record_type}.") from error
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise RecordValidationError(f"Bundled {record_type} schema is invalid.") from error
    return schema


def validate_payload(record_type: str, payload: dict[str, Any]) -> None:
    if record_type not in RECORD_TYPES:
        raise RecordValidationError(f"Unsupported canonical record type: {record_type}.")
    if not isinstance(payload, dict):
        raise RecordValidationError(f"{record_type} record must be a JSON object.")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RecordValidationError(
            f"{record_type} schema_version must be {SCHEMA_VERSION!r}."
        )
    validator = Draft202012Validator(
        _load_schema(record_type), format_checker=FORMAT_CHECKER
    )
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path)
        field = f" at {location}" if location else ""
        raise RecordValidationError(
            f"Invalid {record_type} record{field}: {error.message}"
        )


def validate_record(record: CanonicalRecord) -> dict[str, Any]:
    payload = record_to_payload(record)
    validate_payload(record.record_type, payload)
    return payload


def parse_record(record_type: str, payload: dict[str, Any]) -> CanonicalRecord:
    validate_payload(record_type, payload)
    return record_from_payload(record_type, payload)
