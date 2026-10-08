import hashlib
import html
import ipaddress
import logging
import multiprocessing
import posixpath
import re
import time
import xml.etree.ElementTree as ElementTree
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from multiprocessing.connection import Connection
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from cnvs.configuration import MVP_LANGUAGE_CODES
from cnvs.models import (
    DuplicateRelationship,
    JsonValue,
    NormalizedDocument,
    TranslationRecord,
)
from cnvs.registry import SENSITIVE_QUERY_KEYS
from cnvs.storage import Database, StorageError


TRACKING_QUERY_KEYS = frozenset(
    {"fbclid", "gclid", "mc_cid", "mc_eid", "ref"}
)
SIMILARITY_THRESHOLD = 0.82
MIN_SIMILARITY_TEXT_LENGTH = 120
MAX_TITLE_CHARS = 512
MAX_NORMALIZATION_BYTES = 10 * 1024 * 1024
MAX_NORMALIZATION_ITEMS = 5000
PARSER_TIMEOUT_SECONDS = 15
FORBIDDEN_XML_DECLARATIONS = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
WORD_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
LOGGER = logging.getLogger("cnvs.normalization")


class NormalizationError(ValueError):
    """Raised when stored source content cannot be normalized safely."""

    def __init__(self, message: str, *, code: str = "normalization_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _ParsedItem:
    index: int
    identifier: str
    title: str
    text: str
    url: str | None
    published_at: str | None
    declared_language: str | None


@dataclass(frozen=True)
class _ParserRequest:
    collection_id: str
    source_id: str
    source_metadata: dict[str, JsonValue]
    source_url: str
    final_url: str | None
    content_type: str | None
    content: bytes
    created_at: str
    operation: str = "normalize"


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self.metadata: dict[str, str] = {}
        self.links: dict[str, str] = {}
        self.html_language: str | None = None
        self._ignored_depth = 0
        self._title_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value for key, value in attrs}
        tag = tag.lower()
        if tag == "html":
            self.html_language = attributes.get("lang")
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self._ignored_depth += 1
        if tag == "title":
            self._title_depth += 1
        if tag == "meta":
            name = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content")
            if name and content:
                self.metadata[name] = content
        if tag == "link":
            rel = (attributes.get("rel") or "").lower().split()
            href = attributes.get("href")
            if href and "canonical" in rel:
                self.links["canonical"] = href
        if tag in {"br", "p", "div", "li", "article", "section", "h1", "h2", "h3"}:
            if self._ignored_depth == 0:
                self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag in {"script", "style", "noscript", "svg", "template"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag in {"p", "div", "li", "article", "section", "h1", "h2", "h3"}:
            if self._ignored_depth == 0:
                self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self.text_parts.append(data)
        if self._title_depth:
            self.title_parts.append(data)


def normalize_text(value: str) -> str:
    """Apply conservative Unicode and whitespace normalization, not translation."""
    normalized = unicodedata.normalize("NFC", value).replace("\u00a0", " ")
    return " ".join(normalized.split())


def normalize_publisher(value: str) -> str:
    return normalize_text(value).casefold()


def normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    hostname = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None:
        hostname = address.compressed
        if address.version == 6:
            hostname = f"[{hostname}]"
    if port is not None and port != (443 if parsed.scheme.lower() == "https" else 80):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname
    path = parsed.path or "/"
    trailing_slash = path.endswith("/")
    path = posixpath.normpath(path)
    if not path.startswith("/"):
        path = "/" + path
    if trailing_slash and path != "/":
        path += "/"
    query = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in TRACKING_QUERY_KEYS
        and key.lower() not in SENSITIVE_QUERY_KEYS
    ]
    query.sort()
    return urlunsplit(
        (parsed.scheme.lower(), netloc, path, urlencode(query, doseq=True), "")
    )


def _timestamp(value: str | None) -> tuple[str | None, bool]:
    if not value or not value.strip():
        return None, False
    candidate = value.strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(candidate)
        except (TypeError, ValueError, OverflowError):
            return None, False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.isoformat(timespec="seconds"), False
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        True,
    )


def _safe_xml_root(content: bytes) -> ElementTree.Element:
    for encoding in ("utf-8-sig", "utf-16", "utf-32"):
        try:
            text = content.decode(encoding)
        except UnicodeError:
            continue
        if FORBIDDEN_XML_DECLARATIONS.search(text):
            raise NormalizationError(
                "RSS/Atom content contains a forbidden XML declaration.",
                code="unsafe_feed_xml",
            )
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise NormalizationError(
            "Archived RSS/Atom content is not valid XML.", code="invalid_feed"
        ) from error


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_values(item: ElementTree.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for child in item:
        name = _local_name(child.tag)
        if name == "link":
            relation = child.attrib.get("rel", "alternate").lower()
            if relation not in {"", "alternate"}:
                continue
            value = child.attrib.get("href") or child.text
        else:
            value = "".join(child.itertext())
        if value and name not in values:
            values[name] = html.unescape(value)
        elif value and name in {"content", "encoded", "description", "summary"}:
            values[name] += "\n" + html.unescape(value)
    return values


def _extract_html_fragment(value: str) -> str:
    extractor = _HTMLTextExtractor()
    extractor.feed(value)
    extractor.close()
    return " ".join(extractor.text_parts)


def _parse_feed(content: bytes, feed_url: str) -> list[_ParsedItem]:
    root = _safe_xml_root(content)
    root_name = _local_name(root.tag)
    if root_name not in {"rss", "rdf", "feed"}:
        raise NormalizationError(
            "Archived collection is not an RSS or Atom feed.",
            code="unsupported_feed_format",
        )
    feed_language = root.attrib.get("{http://www.w3.org/XML/1998/namespace}lang")
    channel = next(
        (child for child in root if _local_name(child.tag) == "channel"), root
    )
    channel_values = _child_values(channel)
    feed_language = feed_language or channel_values.get("language")
    elements: list[ElementTree.Element] = []
    for element in root.iter():
        if _local_name(element.tag) in {"item", "entry"}:
            elements.append(element)
            if len(elements) > MAX_NORMALIZATION_ITEMS:
                raise NormalizationError(
                    "Feed exceeds the "
                    f"{MAX_NORMALIZATION_ITEMS:,}-item normalization limit.",
                    code="too_many_items",
                )
    parsed_items: list[_ParsedItem] = []
    for index, item in enumerate(elements):
        values = _child_values(item)
        title = values.get("title", "")
        text_parts = [
            values[key]
            for key in ("description", "summary", "content", "encoded")
            if values.get(key)
        ]
        content_text = _extract_html_fragment("\n".join(text_parts))
        link = values.get("link")
        if link:
            link = urljoin(feed_url, link)
        identifier = (
            values.get("guid")
            or values.get("id")
            or link
            or hashlib.sha256(f"{index}\0{title}\0{content_text}".encode()).hexdigest()
        )
        parsed_items.append(
            _ParsedItem(
                index=index,
                identifier=identifier,
                title=title,
                text=content_text,
                url=link,
                published_at=(
                    values.get("pubdate")
                    or values.get("published")
                    or values.get("updated")
                    or values.get("date")
                ),
                declared_language=(
                    item.attrib.get("{http://www.w3.org/XML/1998/namespace}lang")
                    or feed_language
                ),
            )
        )
    if not elements:
        raise NormalizationError(
            "RSS/Atom feed contains no item or entry elements.", code="empty_feed"
        )
    return parsed_items


def _parse_html(content: bytes, source_url: str, final_url: str | None) -> list[_ParsedItem]:
    charset_match = re.search(
        rb"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
        content[:8192],
        re.IGNORECASE,
    )
    charset = charset_match.group(1).decode("ascii") if charset_match else "utf-8"
    try:
        text = content.decode(charset)
    except (LookupError, UnicodeError) as error:
        raise NormalizationError(
            "HTML page has an unsupported or invalid declared character encoding."
        ) from error
    extractor = _HTMLTextExtractor()
    extractor.feed(text)
    extractor.close()
    metadata = extractor.metadata
    title = " ".join(extractor.title_parts)
    if not title:
        title = metadata.get("og:title") or metadata.get("twitter:title") or ""
    article_text = " ".join(extractor.text_parts).strip()
    declared_language = extractor.html_language or metadata.get("og:locale")
    if declared_language:
        declared_language = declared_language.replace("_", "-")
    canonical_url = extractor.links.get("canonical")
    page_url = source_url
    if final_url:
        page_url = final_url
    if canonical_url:
        page_url = urljoin(page_url, canonical_url)
    if not title and not article_text:
        raise NormalizationError("Archived HTML page contains no extractable title or text.")
    return [
        _ParsedItem(
            index=0,
            identifier=page_url,
            title=title,
            text=article_text,
            url=page_url,
            published_at=(
                metadata.get("article:published_time")
                or metadata.get("datepublished")
                or metadata.get("date")
                or metadata.get("pubdate")
            ),
            declared_language=declared_language,
        )
    ]


def _language(
    declared_language: str | None, registry_language: str | None
) -> tuple[str, str, str]:
    def known(value: str | None) -> str | None:
        if not value or LANGUAGE_TAG.fullmatch(value) is None:
            return None
        primary = value.split("-", 1)[0].lower()
        return primary if primary in MVP_LANGUAGE_CODES else None

    declared = known(declared_language)
    registered = known(registry_language)
    if declared is not None:
        conflict = registered is not None and declared != registered
        return (
            declared,
            "DOCUMENT_DECLARED",
            "REVIEW_REQUIRED" if conflict else "RECORDED",
        )
    if registered is not None:
        review = "REVIEW_REQUIRED" if declared_language else "RECORDED"
        return registered, "REGISTRY", review
    review_status = (
        "REVIEW_REQUIRED"
        if declared_language is not None or registry_language is not None
        else "UNKNOWN"
    )
    return "unknown", "UNKNOWN", review_status


def _parse_collection_documents_in_process(
    request: _ParserRequest,
) -> list[NormalizedDocument]:
    if request.content_type in {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml", "text/rss+xml"}:
        items = _parse_feed(request.content, request.final_url or request.source_url)
    elif request.content_type in {"text/html", "application/xhtml+xml"}:
        items = _parse_html(request.content, request.source_url, request.final_url)
    else:
        raise NormalizationError(
            f"Content type {request.content_type!r} is not supported for normalization."
        )

    registry_language = request.source_metadata.get("language")
    registry_language = registry_language if isinstance(registry_language, str) else None
    publisher = request.source_metadata.get("publisher")
    publisher_original = publisher.strip() if isinstance(publisher, str) else ""
    documents: list[NormalizedDocument] = []
    for item in items:
        original_text = item.text
        normalized = normalize_text(original_text)
        original_title = item.title
        normalized_title = normalize_text(original_title)
        language, language_source, language_review_status = _language(
            item.declared_language, registry_language
        )
        normalized_published_at, timezone_known = _timestamp(item.published_at)
        origin_identifier = (
            item.identifier or item.url or f"{request.collection_id}:{item.index}"
        )
        identity = f"{request.collection_id}\0{item.index}\0{origin_identifier}"
        documents.append(
            NormalizedDocument(
                document_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                collection_id=request.collection_id,
                source_id=request.source_id,
                item_index=item.index,
                origin_identifier=origin_identifier,
                original_title=original_title,
                normalized_title=normalized_title,
                original_text=original_text,
                normalized_text=normalized,
                original_url=item.url,
                normalized_url=normalize_url(item.url),
                publisher_original=publisher_original,
                publisher_normalized=normalize_publisher(publisher_original),
                language=language,
                language_source=language_source,
                language_review_status=language_review_status,
                registry_language=registry_language,
                declared_language=item.declared_language,
                original_published_at=item.published_at,
                normalized_published_at=normalized_published_at,
                publication_timezone_known=timezone_known,
                text_sha256=hashlib.sha256(original_text.encode("utf-8")).hexdigest(),
                normalized_text_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                created_at=request.created_at,
            )
        )
    return documents


def _parse_in_worker(connection: Connection, request: _ParserRequest) -> None:
    try:
        if request.operation == "feed_origin_identifiers":
            items = _parse_feed(
                request.content, request.final_url or request.source_url
            )
            identifiers = tuple(
                dict.fromkeys(
                    identifier
                    for item in items
                    for identifier in (item.identifier, item.url)
                    if identifier
                )
            )
            connection.send((True, identifiers, None))
        else:
            documents = _parse_collection_documents_in_process(request)
            connection.send((True, documents, None))
    except NormalizationError as error:
        connection.send((False, error.code, str(error)))
    except Exception as error:
        connection.send((False, "parser_error", type(error).__name__))
    finally:
        connection.close()


def _run_isolated_parser(request: _ParserRequest) -> object:
    content = request.content
    if len(content) > MAX_NORMALIZATION_BYTES:
        raise NormalizationError(
            f"Archived content exceeds the {MAX_NORMALIZATION_BYTES}-byte "
            "normalization limit.",
            code="input_too_large",
        )
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_parse_in_worker, args=(sender, request), daemon=True
    )
    started = False
    try:
        process.start()
        started = True
        sender.close()
        if not receiver.poll(PARSER_TIMEOUT_SECONDS):
            raise NormalizationError(
                f"Document parsing exceeded {PARSER_TIMEOUT_SECONDS} seconds.",
                code="parser_timeout",
            )
        try:
            succeeded, payload, detail = receiver.recv()
        except EOFError as error:
            raise NormalizationError(
                "The isolated document parser exited without a result.",
                code="parser_process_failed",
            ) from error
        process.join(timeout=1)
        if succeeded:
            return payload
        if payload == "parser_error":
            raise NormalizationError(
                f"The isolated document parser failed ({detail}).",
                code="parser_error",
            )
        raise NormalizationError(str(detail), code=str(payload))
    finally:
        receiver.close()
        sender.close()
        if started:
            if process.is_alive():
                process.terminate()
            process.join()


def parse_collection_documents(
    *,
    collection_id: str,
    source_id: str,
    source_metadata: dict[str, JsonValue],
    source_url: str,
    final_url: str | None,
    content_type: str | None,
    content: bytes,
    created_at: str,
) -> list[NormalizedDocument]:
    payload = _run_isolated_parser(
        _ParserRequest(
            collection_id=collection_id,
            source_id=source_id,
            source_metadata=source_metadata,
            source_url=source_url,
            final_url=final_url,
            content_type=content_type,
            content=content,
            created_at=created_at,
        )
    )
    if isinstance(payload, list) and all(
        isinstance(document, NormalizedDocument) for document in payload
    ):
        return payload
    raise NormalizationError(
        "The isolated document parser returned an invalid result.",
        code="parser_process_failed",
    )


def parse_feed_origin_identifiers(
    content: bytes, feed_url: str
) -> tuple[str, ...]:
    payload = _run_isolated_parser(
        _ParserRequest(
            collection_id="collection",
            source_id="source",
            source_metadata={},
            source_url=feed_url,
            final_url=feed_url,
            content_type="application/rss+xml",
            content=content,
            created_at="",
            operation="feed_origin_identifiers",
        )
    )
    if isinstance(payload, tuple) and all(
        isinstance(identifier, str) for identifier in payload
    ):
        return payload
    raise NormalizationError(
        "The isolated document parser returned invalid feed identifiers.",
        code="parser_process_failed",
    )


def _shingles(text: str) -> set[tuple[str, ...]]:
    tokens = tuple(token.casefold() for token in WORD_PATTERN.findall(text))
    if len(tokens) < 5:
        return set()
    return set(zip(tokens, tokens[1:], tokens[2:], tokens[3:], tokens[4:]))


def _text_similarity(left: str, right: str) -> float:
    left_shingles = _shingles(left)
    right_shingles = _shingles(right)
    if not left_shingles or not right_shingles:
        return 0.0
    union = left_shingles | right_shingles
    return len(left_shingles & right_shingles) / len(union)


def detect_relationship(
    document: NormalizedDocument, candidate: NormalizedDocument
) -> tuple[str, float] | None:
    if document.document_id == candidate.document_id:
        return None
    if (
        document.normalized_text
        and document.normalized_text_sha256 == candidate.normalized_text_sha256
    ):
        return "EXACT_TEXT_MATCH", 1.0
    same_origin = (
        document.origin_identifier == candidate.origin_identifier
        and not document.origin_identifier.startswith(("http://", "https://"))
    )
    same_url = (
        document.normalized_url is not None
        and document.normalized_url == candidate.normalized_url
    )
    similarity = _text_similarity(document.normalized_text, candidate.normalized_text)
    if same_origin or same_url:
        return "POSSIBLE_SYNDICATION", max(similarity, 0.95 if same_origin else 0.9)
    if (
        min(len(document.normalized_text), len(candidate.normalized_text))
        < MIN_SIMILARITY_TEXT_LENGTH
    ):
        return None
    left_title = document.normalized_title[:MAX_TITLE_CHARS].casefold()
    right_title = candidate.normalized_title[:MAX_TITLE_CHARS].casefold()
    title_similarity = (
        SequenceMatcher(None, left_title, right_title, autojunk=False).ratio()
        if left_title and right_title
        else 0.0
    )
    if similarity >= SIMILARITY_THRESHOLD or (
        similarity >= 0.65 and title_similarity >= 0.9
    ):
        return "POSSIBLE_SYNDICATION", similarity
    return None


def normalize_collection(
    database: Database, collection_id: str
) -> tuple[list[NormalizedDocument], list[DuplicateRelationship]]:
    result = database.get_collection_result(collection_id)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    started_monotonic = time.perf_counter()
    try:
        if result.status != "SUCCEEDED":
            raise NormalizationError(
                f"Collection {collection_id} has status {result.status}; only successful "
                "collections can be normalized."
            )
        if result.archived_content_ref is None:
            raise NormalizationError(
                f"Collection {collection_id} has no retained content available to normalize."
            )
        snapshot = database.get_raw_snapshot(result.archived_content_ref)
        documents = parse_collection_documents(
            collection_id=result.collection_id,
            source_id=result.source_id,
            source_metadata=result.source_metadata,
            source_url=result.source_url,
            final_url=result.final_url,
            content_type=result.content_type,
            content=snapshot.content,
            created_at=result.completed_at,
        )
        normalized = database.save_normalized_documents(
            documents, _verified_snapshot_sha256=snapshot.content_sha256
        )
    except NormalizationError as error:
        database.record_processing_attempt(
            collection_id=collection_id,
            status="FAILED",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            error_code=error.code,
        )
        LOGGER.error(
            "Collection normalization failed.",
            extra={
                "event": "normalization_attempt",
                "collection_id": collection_id,
                "stage": "NORMALIZATION",
                "status": "FAILED",
                "error_code": error.code,
                "duration_ms": round((time.perf_counter() - started_monotonic) * 1000),
            },
        )
        raise
    except StorageError:
        database.record_processing_attempt(
            collection_id=collection_id,
            status="FAILED",
            started_at=started_at,
            completed_at=datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            error_code="storage_error",
        )
        LOGGER.error(
            "Collection normalization could not be persisted.",
            extra={
                "event": "normalization_attempt",
                "collection_id": collection_id,
                "stage": "NORMALIZATION",
                "status": "FAILED",
                "error_code": "storage_error",
                "duration_ms": round((time.perf_counter() - started_monotonic) * 1000),
            },
        )
        raise
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    database.record_processing_attempt(
        collection_id=collection_id,
        status="SUCCEEDED",
        started_at=started_at,
        completed_at=completed_at,
    )
    LOGGER.info(
        "Collection normalization succeeded.",
        extra={
            "event": "normalization_attempt",
            "collection_id": collection_id,
            "stage": "NORMALIZATION",
            "status": "SUCCEEDED",
            "document_count": len(normalized[0]),
            "duplicate_candidate_count": len(normalized[1]),
            "duration_ms": round((time.perf_counter() - started_monotonic) * 1000),
        },
    )
    return normalized


def add_translation(
    database: Database,
    *,
    document_id: str,
    translated_text: str,
    target_language: str,
    translation_method: str,
    translator: str,
    translated_at: str,
) -> TranslationRecord:
    return database.add_translation(
        document_id=document_id,
        translated_text=translated_text,
        target_language=target_language,
        translation_method=translation_method,
        translator=translator,
        translated_at=translated_at,
    )
