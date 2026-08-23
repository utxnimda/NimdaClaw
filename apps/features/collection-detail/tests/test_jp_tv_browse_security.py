from __future__ import annotations

import unittest

from work_catalog_yaml.jp_tv.browse_security import (
    is_loopback_hostname,
    mutation_source_is_allowed,
    origin_matches_request,
)


class JpTvBrowseSecurityTest(unittest.TestCase):
    def test_loopback_hostnames_are_allowed(self) -> None:
        for hostname in ("localhost", "127.0.0.1", "::1", "[::1]"):
            with self.subTest(hostname=hostname):
                self.assertTrue(is_loopback_hostname(hostname))

    def test_non_loopback_hostnames_are_rejected(self) -> None:
        for hostname in ("0.0.0.0", "192.168.1.20", "example.test", ""):
            with self.subTest(hostname=hostname):
                self.assertFalse(is_loopback_hostname(hostname))

    def test_origin_must_match_scheme_and_host(self) -> None:
        self.assertTrue(
            origin_matches_request(
                "http://127.0.0.1:8765",
                request_scheme="http",
                request_host="127.0.0.1:8765",
            )
        )

    def test_equivalent_loopback_origins_are_allowed_on_the_same_port(self) -> None:
        self.assertTrue(
            origin_matches_request(
                "http://localhost:8765",
                request_scheme="http",
                request_host="127.0.0.1:8765",
            )
        )
        self.assertTrue(
            origin_matches_request(
                "http://[::1]:8765",
                request_scheme="http",
                request_host="localhost:8765",
            )
        )

    def test_origin_rejects_a_different_port_or_non_origin_url(self) -> None:
        self.assertFalse(
            origin_matches_request(
                "http://localhost:8766",
                request_scheme="http",
                request_host="127.0.0.1:8765",
            )
        )
        self.assertFalse(
            origin_matches_request(
                "http://127.0.0.1:8765/untrusted",
                request_scheme="http",
                request_host="127.0.0.1:8765",
            )
        )
        self.assertFalse(
            origin_matches_request(
                "https://127.0.0.1:8765",
                request_scheme="http",
                request_host="127.0.0.1:8765",
            )
        )
        self.assertFalse(
            origin_matches_request(
                "https://example.test",
                request_scheme="http",
                request_host="127.0.0.1:8765",
            )
        )

    def test_fetch_metadata_same_origin_is_authoritative(self) -> None:
        self.assertTrue(
            mutation_source_is_allowed(
                fetch_site="same-origin",
                origin="http://desktop-shell.invalid",
                request_scheme="http",
                request_host="127.0.0.1:8765",
            )
        )

    def test_fetch_metadata_cross_site_is_always_rejected(self) -> None:
        self.assertFalse(
            mutation_source_is_allowed(
                fetch_site="cross-site",
                origin="http://127.0.0.1:8765",
                request_scheme="http",
                request_host="127.0.0.1:8765",
            )
        )

    def test_origin_remains_the_fallback_without_fetch_metadata(self) -> None:
        self.assertTrue(
            mutation_source_is_allowed(
                fetch_site="",
                origin="http://localhost:8765",
                request_scheme="http",
                request_host="127.0.0.1:8765",
            )
        )
        self.assertFalse(
            mutation_source_is_allowed(
                fetch_site="same-site",
                origin="http://localhost:8766",
                request_scheme="http",
                request_host="127.0.0.1:8765",
            )
        )


if __name__ == "__main__":
    unittest.main()
