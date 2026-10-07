import ipaddress
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from cnvs.models import CollectionResult, JsonValue
from cnvs.registry import SENSITIVE_QUERY_KEYS, SourceRegistration
from cnvs.storage import Database


USER_AGENT = "CNVS/0.1 (public source collection)"
REDIRECT_CODES = {301, 302, 303, 307, 308}
ACCESS_RESTRICTED_CODES = {401, 403, 429, 451}
MAX_REDIRECTS = 5
MAX_ROBOTS_BYTES = 512 * 1024


@dataclass(frozen=True)
class _Response:
    status: int
    final_url: str
    headers: Message
    content: bytes

    @property
    def content_type(self) -> str | None:
        value = self.headers.get("Content-Type")
        return value.split(";", 1)[0].strip().lower() if value else None


class _CollectionFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        blocked: bool = False,
        response: _Response | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.blocked = blocked
        self.response = response


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _check_public_host(url: str) -> None:
    if any(character.isspace() or ord(character) < 32 for character in url):
        raise _CollectionFailure(
            "invalid_url", "Collection URLs cannot contain whitespace or control characters."
        )
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise _CollectionFailure("invalid_url", "URL contains an invalid port.") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "%" in parsed.hostname
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise _CollectionFailure(
            "invalid_url",
            "Collection destinations must be HTTP(S), public, and contain no credentials.",
        )
    if any(
        key.lower() in SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query)
    ):
        raise _CollectionFailure(
            "credentials_in_url", "Credentials and tokens must not be embedded in URLs.",
            blocked=True,
        )
    hostname = parsed.hostname
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname.lower().rstrip(".") == "localhost" or hostname.lower().endswith(
            (".localhost", ".local", ".internal")
        ):
            raise _CollectionFailure("non_public_address", "Local hostnames are not allowed.")
        try:
            addresses = {
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(
                    hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except OSError as error:
            raise _CollectionFailure("dns_error", "Unable to resolve the source hostname.") from error
    else:
        addresses = {literal}
    if not addresses or any(not address.is_global for address in addresses):
        raise _CollectionFailure(
            "non_public_address",
            "Source or redirect resolves to a non-public IP address.",
            blocked=True,
        )


def _read_response(response, *, max_bytes: int, url: str) -> _Response:
    status = response.status if hasattr(response, "status") else response.code
    headers = response.headers
    length_header = headers.get("Content-Length")
    if length_header:
        try:
            if int(length_header) > max_bytes:
                raise _CollectionFailure(
                    "response_too_large",
                    f"Response exceeds the configured {max_bytes}-byte limit.",
                    response=_Response(
                        response.status if hasattr(response, "status") else response.code,
                        url,
                        headers,
                        b"",
                    ),
                )
        except ValueError:
            pass
    try:
        content = response.read(max_bytes + 1)
    except (OSError, TimeoutError) as error:
        raise _CollectionFailure(
            "response_read_error",
            "Unable to read the source response.",
            response=_Response(
                response.status if hasattr(response, "status") else response.code,
                url,
                headers,
                b"",
            ),
        ) from error
    if len(content) > max_bytes:
        raise _CollectionFailure(
            "response_too_large",
            f"Response exceeds the configured {max_bytes}-byte limit.",
            response=_Response(
                response.status if hasattr(response, "status") else response.code,
                url,
                headers,
                b"",
            ),
        )
    return _Response(status, url, headers, content)


def _fetch(
    url: str,
    *,
    max_bytes: int,
    timeout_seconds: int,
    authorize_url: Callable[[str], None] | None = None,
) -> _Response:
    current_url = url
    opener = urllib.request.build_opener(_NoRedirectHandler())
    for redirect_number in range(MAX_REDIRECTS + 1):
        _check_public_host(current_url)
        if authorize_url is not None:
            authorize_url(current_url)
        request = urllib.request.Request(
            current_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                "text/xml, text/html, application/xhtml+xml, */*;q=0.1",
            },
        )
        try:
            response = opener.open(request, timeout=timeout_seconds)
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise _CollectionFailure(
                "network_error", "Unable to retrieve the public source."
            ) from error

        try:
            status = response.code if hasattr(response, "code") else response.status
            if status in REDIRECT_CODES:
                location = response.headers.get("Location")
                redirect_response = _Response(
                    status, current_url, response.headers, b""
                )
                response.close()
                if not location:
                    raise _CollectionFailure(
                        "redirect_without_location",
                        "Source redirect omitted its destination.",
                        response=redirect_response,
                    )
                if redirect_number == MAX_REDIRECTS:
                    raise _CollectionFailure(
                        "too_many_redirects",
                        "Source exceeded the redirect limit.",
                        response=redirect_response,
                    )
                current_url = urljoin(current_url, location)
                redirected = urlsplit(current_url)
                if redirected.scheme not in {"http", "https"}:
                    raise _CollectionFailure(
                        "unsupported_redirect_scheme",
                        "Source redirected to a non-HTTP(S) destination.",
                        blocked=True,
                        response=redirect_response,
                    )
                try:
                    _check_public_host(current_url)
                except _CollectionFailure as error:
                    raise _CollectionFailure(
                        error.code,
                        str(error),
                        blocked=error.blocked,
                        response=_Response(
                            status, current_url, redirect_response.headers, b""
                        ),
                    ) from error
                continue
            if status in ACCESS_RESTRICTED_CODES:
                response.close()
                raise _CollectionFailure(
                    f"http_{status}",
                    f"Source access was restricted by HTTP status {status}.",
                    blocked=True,
                    response=_Response(status, current_url, response.headers, b""),
                )
            return _read_response(response, max_bytes=max_bytes, url=current_url)
        finally:
            response.close()
    raise _CollectionFailure("too_many_redirects", "Source exceeded the redirect limit.")


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower(),
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def _robots_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))


def _check_robots(url: str, entry: SourceRegistration) -> None:
    try:
        response = _fetch(
            _robots_url(url),
            max_bytes=min(entry.constraints.max_bytes, MAX_ROBOTS_BYTES),
            timeout_seconds=entry.constraints.timeout_seconds,
        )
    except _CollectionFailure as error:
        if error.blocked and error.code.startswith("http_"):
            raise _CollectionFailure(
                f"robots_{error.code}",
                f"robots.txt could not authorize collection: {error}.",
                blocked=True,
                response=error.response,
            ) from error
        raise
    if response.status == 404:
        return
    if response.status in ACCESS_RESTRICTED_CODES:
        raise _CollectionFailure(
            f"robots_http_{response.status}",
            f"robots.txt access was restricted by HTTP status {response.status}.",
            blocked=True,
            response=response,
        )
    if not 200 <= response.status < 300:
        raise _CollectionFailure(
            "robots_unavailable",
            f"robots.txt returned HTTP status {response.status}; collection was not attempted.",
            response=response,
        )
    if b"<!DOCTYPE" in response.content.upper() or b"<!ENTITY" in response.content.upper():
        raise _CollectionFailure(
            "invalid_robots_document", "robots.txt contained forbidden XML declarations."
        )
    parser = RobotFileParser()
    parser.set_url(_robots_url(url))
    parser.parse(response.content.decode("utf-8", errors="replace").splitlines())
    if not parser.can_fetch(USER_AGENT, url):
        raise _CollectionFailure(
            "robots_disallow",
            "robots.txt disallows collection of this URL.",
            blocked=True,
        )


def _contains_forbidden_xml_declaration(content: bytes) -> bool:
    for encoding in ("utf-8-sig", "utf-16", "utf-32"):
        try:
            text = content.decode(encoding)
        except UnicodeError:
            continue
        normalized = text.upper()
        if "<!DOCTYPE" in normalized or "<!ENTITY" in normalized:
            return True
    return False


def _rss_origin_identifiers(content: bytes) -> tuple[str, ...]:
    if _contains_forbidden_xml_declaration(content):
        raise _CollectionFailure(
            "unsafe_feed_xml", "RSS/Atom feed contains a forbidden XML declaration."
        )
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise _CollectionFailure("invalid_feed", "RSS/Atom feed is not valid XML.") from error
    root_name = root.tag.rsplit("}", 1)[-1].lower()
    if root_name not in {"feed", "rdf", "rss"}:
        raise _CollectionFailure(
            "unsupported_feed_format",
            "The source did not return an RSS or Atom feed document.",
        )

    identifiers: list[str] = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1].lower() not in {"item", "entry"}:
            continue
        for child in item:
            name = child.tag.rsplit("}", 1)[-1].lower()
            value = (
                child.attrib.get("href") or child.text
                if name == "link"
                else child.text
            )
            if name in {"guid", "id", "link"} and isinstance(value, str) and value.strip():
                normalized = value.strip()
                if normalized not in identifiers:
                    identifiers.append(normalized)
    return tuple(identifiers)


def _record(
    database: Database,
    entry: SourceRegistration,
    *,
    status: str,
    started_at: str,
    completed_at: str,
    retry_of: str | None,
    response: _Response | None = None,
    content: bytes | None = None,
    origin_identifiers: tuple[str, ...] = (),
    error_code: str | None = None,
    error_message: str | None = None,
) -> CollectionResult:
    source_metadata: dict[str, JsonValue] = {
        "publisher": entry.publisher,
        "source_class": entry.source_class,
        "country": entry.country,
        "language": entry.language,
        "access_method": entry.access_method,
        "url": entry.url,
        "event_relevance": entry.event_relevance,
        "review": {
            "status": entry.review_status,
            "reviewer": entry.reviewer,
            "reviewed_at": entry.reviewed_at,
        },
        "enabled": entry.enabled,
        "constraints": {
            "max_bytes": entry.constraints.max_bytes,
            "allowed_content_types": list(entry.constraints.allowed_content_types),
            "archive_content": entry.constraints.archive_content,
            "retention_basis": entry.constraints.retention_basis,
            "respect_robots_txt": entry.constraints.respect_robots_txt,
            "timeout_seconds": entry.constraints.timeout_seconds,
        },
    }
    return database.record_collection_result(
        source_id=entry.source_id,
        source_metadata=source_metadata,
        source_url=entry.url,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        retry_of=retry_of,
        final_url=response.final_url if response is not None else None,
        http_status=response.status if response is not None else None,
        content_type=response.content_type if response is not None else None,
        content=content,
        archive_content=entry.constraints.archive_content,
        origin_identifiers=origin_identifiers,
        error_code=error_code,
        error_message=error_message,
    )


def collect_source(
    database: Database,
    entry: SourceRegistration,
    *,
    retry_of: str | None = None,
) -> CollectionResult:
    started_at = _now()
    if not entry.approved:
        return _record(
            database,
            entry,
            status="BLOCKED",
            started_at=started_at,
            completed_at=_now(),
            retry_of=retry_of,
            error_code="source_not_approved",
            error_message="Source collection requires an enabled, analyst-approved registry entry.",
        )

    checked_origins: set[tuple[str, str, int]] = set()

    def authorize_url(url: str) -> None:
        origin = _origin(url)
        if origin not in checked_origins:
            _check_robots(url, entry)
            checked_origins.add(origin)

    response: _Response | None = None
    body: bytes | None = None
    origin_identifiers: tuple[str, ...] = ()
    try:
        response = _fetch(
            entry.url,
            max_bytes=entry.constraints.max_bytes,
            timeout_seconds=entry.constraints.timeout_seconds,
            authorize_url=authorize_url if entry.constraints.respect_robots_txt else None,
        )
        if not 200 <= response.status < 300:
            raise _CollectionFailure(
                f"http_{response.status}",
                f"Source returned HTTP status {response.status}.",
                response=response,
            )
        body = response.content
        if response.content_type not in entry.constraints.allowed_content_types:
            raise _CollectionFailure(
                "content_type_not_allowed",
                f"Source media type {response.content_type!r} is not allowed.",
                response=response,
            )
        if entry.access_method == "RSS":
            origin_identifiers = tuple(
                dict.fromkeys(
                    (entry.url, response.final_url, *_rss_origin_identifiers(body))
                )
            )
        else:
            origin_identifiers = tuple(
                dict.fromkeys((entry.url, response.final_url))
            )
        return _record(
            database,
            entry,
            status="SUCCEEDED",
            started_at=started_at,
            completed_at=_now(),
            retry_of=retry_of,
            response=response,
            content=body,
            origin_identifiers=origin_identifiers,
        )
    except _CollectionFailure as error:
        failed_response = error.response or response
        error_body = (
            failed_response.content
            if failed_response is not None and failed_response.content
            else body
        )
        status = "BLOCKED" if error.blocked else "FAILED"
        return _record(
            database,
            entry,
            status=status,
            started_at=started_at,
            completed_at=_now(),
            retry_of=retry_of,
            response=failed_response,
            content=error_body,
            origin_identifiers=origin_identifiers,
            error_code=error.code,
            error_message=str(error),
        )


def retry_collection(
    database: Database,
    entry: SourceRegistration,
    collection_id: str,
) -> CollectionResult:
    previous = database.get_collection_result(collection_id)
    if previous.source_id != entry.source_id:
        raise ValueError(
            f"Collection attempt {collection_id} does not belong to source {entry.source_id}."
        )
    if previous.status != "FAILED":
        raise ValueError("Only failed collection attempts can be retried.")
    return collect_source(database, entry, retry_of=collection_id)


def find_registration(
    config_dir: Path, source_id: str
) -> SourceRegistration:
    from cnvs.registry import load_source_registry

    for registration in load_source_registry(config_dir):
        if registration.source_id == source_id:
            return registration
    raise ValueError(f"Source {source_id} is not configured in the source registry.")
