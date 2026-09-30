"""Unit tests for sandbox service proxy helpers."""

from unittest.mock import patch

import pytest

from orchard_env.orchestrator import service_proxy
from orchard_env.orchestrator.service_proxy import (
    ServicePortError,
    build_service_url,
    build_upstream_url,
    filter_request_headers,
    filter_response_headers,
    filter_websocket_headers,
    validate_port,
)


class TestValidatePort:
    def test_ordinary_port_allowed(self):
        assert validate_port(8000) == 8000

    @pytest.mark.parametrize("port", [1, 65535])
    def test_boundaries_allowed(self, port):
        assert validate_port(port) == port

    @pytest.mark.parametrize("port", [0, -1, 65536, 99999])
    def test_out_of_range_rejected(self, port):
        with pytest.raises(ServicePortError, match="valid range"):
            validate_port(port)

    def test_agent_port_always_rejected(self):
        with pytest.raises(ServicePortError, match="reserved for the in-pod"):
            validate_port(service_proxy.settings.agent_port)

    def test_agent_port_rejected_even_when_reconfigured(self):
        with patch.object(service_proxy.settings, "agent_port", 7777):
            with pytest.raises(ServicePortError, match="reserved for the in-pod"):
                validate_port(7777)

    def test_operator_reserved_ports_rejected(self):
        with patch.object(service_proxy.settings, "service_reserved_ports", "22, 6379"):
            with pytest.raises(ServicePortError, match="reserved"):
                validate_port(6379)
            assert validate_port(8000) == 8000

    @pytest.mark.parametrize("port", ["8000", None, 80.5, True])
    def test_non_integer_rejected(self, port):
        with pytest.raises(ServicePortError):
            validate_port(port)


class TestHeaderFiltering:
    def test_hop_by_hop_request_headers_dropped(self):
        filtered = filter_request_headers(
            {
                "Connection": "keep-alive",
                "Upgrade": "websocket",
                "Transfer-Encoding": "chunked",
                "Content-Type": "application/json",
            }
        )
        assert filtered == [("Content-Type", "application/json")]

    def test_host_and_content_length_dropped(self):
        filtered = filter_request_headers(
            {"Host": "orchestrator", "Content-Length": "12", "X-Trace": "abc"}
        )
        assert filtered == [("X-Trace", "abc")]

    def test_management_credentials_are_never_forwarded(self):
        filtered = filter_request_headers(
            {
                "Authorization": "Bearer secret",
                "Cookie": "session=secret",
                "X-API-Key": "management-key",
                "X-Custom": "safe",
            }
        )
        assert filtered == [("X-Custom", "safe")]

    def test_fields_named_by_connection_are_dropped(self):
        filtered = filter_request_headers(
            [
                ("Connection", "X-Remove, keep-alive"),
                ("X-Remove", "secret"),
                ("X-Keep", "value"),
            ]
        )
        assert filtered == [("X-Keep", "value")]

    def test_duplicate_request_headers_are_preserved(self):
        filtered = filter_request_headers([("X-Value", "one"), ("X-Value", "two")])
        assert filtered == [("X-Value", "one"), ("X-Value", "two")]

    def test_response_hop_by_hop_headers_dropped(self):
        filtered = filter_response_headers(
            {
                "Transfer-Encoding": "chunked",
                "Connection": "close",
                "Content-Type": "text/html",
            }
        )
        assert filtered == [("Content-Type", "text/html")]

    def test_content_headers_survive(self):
        filtered = filter_response_headers(
            {"Content-Length": "100", "Content-Encoding": "gzip"}
        )
        assert filtered == [
            ("Content-Length", "100"),
            ("Content-Encoding", "gzip"),
        ]

    def test_unsafe_browser_state_headers_are_dropped(self):
        filtered = filter_response_headers(
            [
                ("Set-Cookie", "session=hostile; Domain=.example.com"),
                ("Service-Worker-Allowed", "/"),
                ("X-Custom", "1"),
            ]
        )
        assert filtered == [("X-Custom", "1")]

    def test_duplicate_response_headers_are_preserved(self):
        filtered = filter_response_headers([("Link", "</one>"), ("Link", "</two>")])
        assert filtered == [("Link", "</one>"), ("Link", "</two>")]

    def test_websocket_headers_use_a_safe_allowlist(self):
        filtered = filter_websocket_headers(
            {
                "User-Agent": "browser",
                "Authorization": "Bearer secret",
                "Cookie": "secret=1",
                "X-Request-ID": "r1",
            }
        )
        assert filtered == [("User-Agent", "browser"), ("X-Request-ID", "r1")]


class TestUrlBuilding:
    def test_path_without_leading_slash_is_normalised(self):
        assert str(build_upstream_url("10.0.0.1", 8000, "health")) == (
            "http://10.0.0.1:8000/health"
        )

    def test_leading_slash_preserved(self):
        assert str(build_upstream_url("10.0.0.1", 8000, "/health")) == (
            "http://10.0.0.1:8000/health"
        )

    def test_empty_path_becomes_root(self):
        assert str(build_upstream_url("10.0.0.1", 8000, "")) == "http://10.0.0.1:8000/"

    def test_query_string_passed_through_verbatim(self):
        url = build_upstream_url(
            "10.0.0.1", 8000, "/search", raw_query_string="q=a+b&n=1"
        )
        assert str(url) == "http://10.0.0.1:8000/search?q=a+b&n=1"

    def test_ws_scheme_supported(self):
        url = build_upstream_url("10.0.0.1", 8000, "/ws", scheme="ws")
        assert str(url) == "ws://10.0.0.1:8000/ws"

    def test_encoded_delimiters_remain_encoded(self):
        url = build_upstream_url(
            "10.0.0.1",
            8000,
            "/objects/a%2Fb%3Fc%23d",
            raw_query_string="sig=a%2Fb",
        )
        assert str(url) == ("http://10.0.0.1:8000/objects/a%2Fb%3Fc%23d?sig=a%2Fb")

    def test_service_url_puts_token_in_path(self):
        url = build_service_url("https://{subdomain}.services.example.net", "tok123")
        assert url.endswith(".services.example.net/s/tok123")
        assert url.startswith("https://")
        assert f"{url}/ws".endswith("/s/tok123/ws")

    def test_service_url_tolerates_trailing_slash(self):
        url = build_service_url("https://{subdomain}.example.net/", "tok")
        assert url.endswith(".example.net/s/tok")


def test_orchestrator_access_log_is_disabled():
    """Generic access logs would disclose /s/<capability>/... request paths."""
    from orchard_env.orchestrator import main

    with patch.object(main.uvicorn, "run") as run:
        main.main()
    assert run.call_args.kwargs["access_log"] is False
    assert run.call_args.kwargs["log_level"] == "warning"
