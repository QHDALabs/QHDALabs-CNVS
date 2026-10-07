import contextlib
import io
import sqlite3
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import yaml

from cnvs import collection
from cnvs.cli import main
from cnvs.registry import CollectionConstraints, SourceRegistration, load_source_registry
from cnvs.storage import Database


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
RSS_BODY = (
    b'<?xml version="1.0"?><rss><channel><item><guid>wire-123</guid>'
    b"<link>https://news.example/item-1</link></item></channel></rss>"
)


def make_registration(
    *,
    access_method: str = "RSS",
    archive_content: bool = True,
    review_status: str = "approved",
    enabled: bool = True,
) -> SourceRegistration:
    return SourceRegistration(
        source_id="TEST-SOURCE-001",
        publisher="Synthetic test publisher",
        source_class="secondary_reporting",
        country="PL",
        language="pl",
        access_method=access_method,
        url="https://news.example/feed.xml",
        event_relevance="Synthetic collection test fixture.",
        review_status=review_status,
        reviewer="test analyst" if review_status == "approved" else None,
        reviewed_at="2026-10-07T09:00:00Z" if review_status == "approved" else None,
        enabled=enabled,
        constraints=CollectionConstraints(
            max_bytes=1024 * 1024,
            allowed_content_types=("application/rss+xml", "text/html"),
            archive_content=archive_content,
            retention_basis="Synthetic fixture only." if archive_content else None,
            respect_robots_txt=True,
            timeout_seconds=5,
        ),
    )


def response(
    body: bytes = RSS_BODY,
    *,
    status: int = 200,
    content_type: str = "application/rss+xml",
    url: str = "https://news.example/feed.xml",
) -> collection._Response:
    headers = Message()
    headers["Content-Type"] = content_type
    return collection._Response(status, url, headers, body)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "cnvs.sqlite3"
        self.database = Database(self.database_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rss_collection_archives_content_and_records_origin_identifiers(self):
        registration = make_registration()
        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection._fetch", return_value=response()
        ):
            result = collection.collect_source(self.database, registration)

        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.http_status, 200)
        self.assertEqual(
            result.source_metadata["publisher"], "Synthetic test publisher"
        )
        self.assertEqual(
            result.origin_identifiers,
            (
                "https://news.example/feed.xml",
                "wire-123",
                "https://news.example/item-1",
            ),
        )
        assert result.archived_content_ref is not None
        self.assertTrue(result.archived_content_ref.startswith("sha256:"))
        snapshot = self.database.get_raw_snapshot(result.archived_content_ref)
        self.assertEqual(snapshot.content, RSS_BODY)
        self.assertEqual(self.database.collection_results(registration.source_id), [result])

    def test_public_url_collection_does_not_archive_when_not_permitted(self):
        registration = make_registration(access_method="URL", archive_content=False)
        html = b"<html><body>Synthetic page</body></html>"
        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection._fetch", return_value=response(
                html, content_type="text/html", url="https://news.example/article"
            )
        ):
            result = collection.collect_source(self.database, registration)

        self.assertEqual(result.status, "SUCCEEDED")
        self.assertIsNone(result.archived_content_ref)
        self.assertIsNotNone(result.content_sha256)
        self.assertEqual(result.final_url, "https://news.example/article")
        self.assertEqual(
            result.origin_identifiers,
            ("https://news.example/feed.xml", "https://news.example/article"),
        )

    def test_unapproved_registry_source_is_blocked_without_network_access(self):
        registration = make_registration(review_status="pending", enabled=False)
        with patch("cnvs.collection._fetch") as fetch:
            result = collection.collect_source(self.database, registration)

        fetch.assert_not_called()
        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.error_code, "source_not_approved")
        self.assertIsNone(result.content_sha256)

    def test_failed_attempt_is_visible_and_manual_retry_is_linked(self):
        registration = make_registration(access_method="URL")

        def fetch_then_succeed(*args, **kwargs):
            if fetch_then_succeed.calls == 0:
                fetch_then_succeed.calls += 1
                raise collection._CollectionFailure(
                    "network_error", "Synthetic connection failure."
                )
            return response(
                b"<html>recovered</html>",
                content_type="text/html",
                url="https://news.example/article",
            )

        fetch_then_succeed.calls = 0
        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection._fetch", side_effect=fetch_then_succeed
        ):
            failed = collection.collect_source(self.database, registration)
            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(failed.error_code, "network_error")
            succeeded = collection.retry_collection(
                self.database, registration, failed.collection_id
            )

        self.assertEqual(succeeded.status, "SUCCEEDED")
        self.assertEqual(succeeded.attempt_number, 2)
        self.assertEqual(succeeded.retry_of, failed.collection_id)
        self.assertEqual(
            [item.status for item in self.database.collection_results(registration.source_id)],
            ["SUCCEEDED", "FAILED"],
        )

    def test_http_access_restriction_is_blocked_and_not_retryable(self):
        registration = make_registration(access_method="URL")
        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection._fetch",
            side_effect=collection._CollectionFailure(
                "http_403", "Source access was restricted.", blocked=True
            ),
        ):
            blocked = collection.collect_source(self.database, registration)

        self.assertEqual(blocked.status, "BLOCKED")
        with self.assertRaisesRegex(ValueError, "Only failed"):
            collection.retry_collection(
                self.database, registration, blocked.collection_id
            )

    def test_unsafe_rss_xml_is_recorded_as_failure_not_success(self):
        registration = make_registration()
        unsafe_feed = b'<!DOCTYPE rss [<!ENTITY x "no">]><rss><channel/></rss>'
        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection._fetch", return_value=response(unsafe_feed)
        ):
            result = collection.collect_source(self.database, registration)

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error_code, "unsafe_feed_xml")
        self.assertIsNotNone(result.archived_content_ref)
        with self.assertRaisesRegex(collection._CollectionFailure, "forbidden XML"):
            collection._rss_origin_identifiers(
                '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "no">]>'
                "<rss><channel/></rss>".encode("utf-16")
            )

    def test_non_feed_xml_is_not_reported_as_a_successful_rss_collection(self):
        registration = make_registration()
        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection._fetch", return_value=response(b"<html><body/></html>")
        ):
            result = collection.collect_source(self.database, registration)

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error_code, "unsupported_feed_format")

    def test_robots_disallow_is_explicit_and_stops_source_request(self):
        registration = make_registration(access_method="URL")
        robots = response(
            b"User-agent: *\nDisallow: /feed.xml\n",
            content_type="text/plain",
            url="https://news.example/robots.txt",
        )
        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection._fetch", return_value=robots
        ):
            with self.assertRaisesRegex(collection._CollectionFailure, "disallows"):
                collection._check_robots(registration.url, registration)

    def test_local_and_private_addresses_are_rejected(self):
        for url in ("http://127.0.0.1/feed.xml", "http://[::1]/feed.xml"):
            with self.subTest(url=url), self.assertRaises(
                collection._CollectionFailure
            ) as raised:
                collection._check_public_host(url)
            self.assertEqual(raised.exception.code, "non_public_address")

    def test_redirect_to_private_address_is_rejected(self):
        redirect_headers = Message()
        redirect_headers["Location"] = "http://127.0.0.1/private"
        redirect = HTTPError(
            "https://public.example/feed", 302, "Found", redirect_headers, None
        )

        class RedirectOpener:
            def open(self, *args, **kwargs):
                return redirect

        check_public_host = collection._check_public_host
        with patch(
            "cnvs.collection._check_public_host",
            side_effect=lambda url: (
                None
                if url == "https://public.example/feed"
                else check_public_host(url)
            ),
        ), patch("cnvs.collection.urllib.request.build_opener", return_value=RedirectOpener()):
            with self.assertRaises(collection._CollectionFailure) as raised:
                collection._fetch(
                    "https://public.example/feed", max_bytes=1024, timeout_seconds=5
                )

        self.assertEqual(raised.exception.code, "non_public_address")

    def test_response_size_limit_is_checked_before_reading_body(self):
        headers = Message()
        headers["Content-Type"] = "text/html"
        headers["Content-Length"] = "2048"

        class OversizedResponse:
            status = 200

            def __init__(self):
                self.headers = headers
                self.read_called = False

            def read(self, count):
                self.read_called = True
                return b""

            def close(self):
                pass

        oversized = OversizedResponse()

        class OversizedOpener:
            def open(self, *args, **kwargs):
                return oversized

        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection.urllib.request.build_opener",
            return_value=OversizedOpener(),
        ):
            with self.assertRaises(collection._CollectionFailure) as raised:
                collection._fetch(
                    "https://public.example/page",
                    max_bytes=1024,
                    timeout_seconds=5,
                )

        self.assertEqual(raised.exception.code, "response_too_large")
        self.assertFalse(oversized.read_called)

    def test_oversized_response_is_not_misrepresented_as_an_empty_snapshot(self):
        registration = make_registration()
        too_large = response(
            b"",
            content_type="application/rss+xml",
        )
        with patch("cnvs.collection._check_public_host"), patch(
            "cnvs.collection._fetch",
            side_effect=collection._CollectionFailure(
                "response_too_large",
                "Response exceeds the configured limit.",
                response=too_large,
            ),
        ):
            result = collection.collect_source(self.database, registration)

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error_code, "response_too_large")
        self.assertIsNone(result.content_sha256)
        self.assertIsNone(result.archived_content_ref)

    def test_collection_attempts_are_immutable_in_sqlite(self):
        failed = self.database.record_collection_result(
            source_id="TEST-SOURCE-001",
            source_url="https://news.example/feed.xml",
            status="FAILED",
            started_at="2026-10-07T09:00:00Z",
            completed_at="2026-10-07T09:00:01Z",
            error_code="network_error",
            error_message="Synthetic failure.",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE collection_attempts SET error_message = 'hidden' "
                    "WHERE collection_id = ?",
                    (failed.collection_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM collection_attempts WHERE collection_id = ?",
                    (failed.collection_id,),
                )
        finally:
            connection.close()

    def test_cli_lists_sources_and_records_denied_collection(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["source", "list", "--config-dir", str(CONFIG_DIR)])
        self.assertEqual(result, 0)
        self.assertIn("SYNTHETIC-URL-PL-001 [pending/disabled]", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "source",
                    "collect",
                    "SYNTHETIC-URL-PL-001",
                    "--config-dir",
                    str(CONFIG_DIR),
                    "--database",
                    str(self.database_path),
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("BLOCKED", output.getvalue())
        self.assertIn("source_not_approved", output.getvalue())

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["source", "results", "--database", str(self.database_path)])
        self.assertEqual(result, 0)
        self.assertIn("source_not_approved", output.getvalue())

    def test_cli_result_query_works_without_valid_source_registry(self):
        failed = self.database.record_collection_result(
            source_id="TEST-SOURCE-001",
            source_url="https://news.example/feed.xml",
            status="FAILED",
            started_at="2026-10-07T09:00:00Z",
            completed_at="2026-10-07T09:00:01Z",
            error_code="network_error",
            error_message="Synthetic failure.",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "source",
                    "results",
                    "--source-id",
                    "TEST-SOURCE-001",
                    "--database",
                    str(self.database_path),
                ]
            )
        self.assertEqual(result, 0)
        self.assertIn(failed.collection_id, output.getvalue())


class RegistryTests(unittest.TestCase):
    def test_example_registry_is_synthetic_pending_and_disabled(self):
        registrations = load_source_registry(CONFIG_DIR)
        self.assertEqual(len(registrations), 1)
        entry = registrations[0]
        self.assertEqual(entry.source_id, "SYNTHETIC-URL-PL-001")
        self.assertEqual(entry.review_status, "pending")
        self.assertFalse(entry.enabled)
        self.assertFalse(entry.approved)
        self.assertFalse(entry.constraints.archive_content)

    def test_enabled_pending_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            for filename in (
                "countries.yaml",
                "languages.yaml",
                "event_types.yaml",
                "source_types.yaml",
                "source_classes.yaml",
                "source_registry.yaml",
            ):
                (config_dir / filename).write_bytes(
                    (CONFIG_DIR / filename).read_bytes()
                )
            registry_path = config_dir / "source_registry.yaml"
            document = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
            document["sources"][0]["enabled"] = True
            registry_path.write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "must be approved"):
                load_source_registry(config_dir)

    def test_reviewed_source_loads_with_explicit_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            for filename in (
                "countries.yaml",
                "languages.yaml",
                "event_types.yaml",
                "source_types.yaml",
                "source_classes.yaml",
                "source_registry.yaml",
            ):
                (config_dir / filename).write_bytes(
                    (CONFIG_DIR / filename).read_bytes()
                )
            registry_path = config_dir / "source_registry.yaml"
            document = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
            source = document["sources"][0]
            source["review"] = {
                "status": "approved",
                "reviewer": "analyst@example.test",
                "reviewed_at": "2026-10-07T09:00:00Z",
            }
            source["enabled"] = True
            registry_path.write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
            registration = load_source_registry(config_dir)[0]
            self.assertTrue(registration.approved)
            source["review"]["reviewed_at"] = "2026-10-07 09:00:00"
            registry_path.write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "must be RFC3339"):
                load_source_registry(config_dir)

        self.assertTrue(registration.approved)
        self.assertEqual(registration.reviewer, "analyst@example.test")

    def test_registry_rejects_credentials_and_non_public_addresses(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            for filename in (
                "countries.yaml",
                "languages.yaml",
                "event_types.yaml",
                "source_types.yaml",
                "source_classes.yaml",
                "source_registry.yaml",
            ):
                (config_dir / filename).write_bytes(
                    (CONFIG_DIR / filename).read_bytes()
                )
            registry_path = config_dir / "source_registry.yaml"
            document = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
            document["sources"][0]["url"] = "https://example.com/feed?token=secret"
            registry_path.write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "must not be embedded"):
                load_source_registry(config_dir)
            document["sources"][0]["url"] = "http://127.0.0.1/feed"
            registry_path.write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "address must be public"):
                load_source_registry(config_dir)


if __name__ == "__main__":
    unittest.main()
