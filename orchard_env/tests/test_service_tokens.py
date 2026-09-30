"""Unit tests for sandbox service capability tokens."""

import time
from unittest.mock import patch

import pytest

from orchard_env.orchestrator import service_tokens
from orchard_env.orchestrator.service_tokens import (
    ServiceTokenError,
    mint_token,
    verify_token,
)

GENERATION = "generation-1"


@pytest.fixture(autouse=True)
def fixed_secret():
    with patch.object(
        service_tokens.settings, "service_token_secret", "test-secret-key"
    ):
        yield


class TestRoundTrip:
    def test_mint_and_verify_returns_bound_values(self):
        token, expires_at = mint_token("sandbox-abc", 8000, GENERATION)
        assert verify_token(token) == (
            "sandbox-abc",
            8000,
            GENERATION,
            int(expires_at),
        )
        assert expires_at > time.time()

    def test_token_has_two_base64url_segments(self):
        token, _ = mint_token("sandbox-abc", 8000, GENERATION)
        payload, separator, signature = token.partition(".")
        assert separator == "."
        assert payload and signature
        assert all(c.isalnum() or c in "-_" for c in payload + signature)

    def test_arbitrary_sandbox_id_round_trips(self):
        token, _ = mint_token("weird:id/with spaces", 8000, GENERATION)
        sandbox_id, port, generation, _expires_at = verify_token(token)
        assert (sandbox_id, port, generation) == (
            "weird:id/with spaces",
            8000,
            GENERATION,
        )

    def test_explicit_ttl_is_honoured(self):
        _token, expires_at = mint_token(
            "sandbox-abc", 8000, GENERATION, ttl_seconds=120
        )
        assert 110 < expires_at - time.time() <= 120

    @pytest.mark.parametrize(
        ("sandbox_id", "generation"),
        [("", GENERATION), ("sandbox-abc", "")],
    )
    def test_empty_bound_value_rejected(self, sandbox_id, generation):
        with pytest.raises(ValueError):
            mint_token(sandbox_id, 8000, generation)

    def test_non_positive_ttl_rejected(self):
        with pytest.raises(ValueError):
            mint_token("sandbox-abc", 8000, GENERATION, ttl_seconds=0)


class TestRejection:
    def test_expired_token_rejected(self):
        token, _ = mint_token("sandbox-abc", 8000, GENERATION, ttl_seconds=1)
        future = time.time() + 3600
        with patch(
            "orchard_env.orchestrator.service_tokens.time.time", return_value=future
        ):
            with pytest.raises(ServiceTokenError, match="expired"):
                verify_token(token)

    def test_tampered_payload_rejected(self):
        token, _ = mint_token("sandbox-abc", 8000, GENERATION)
        _, _, signature = token.partition(".")
        forged_payload, _ = mint_token("sandbox-victim", 9999, "other")
        tampered = f"{forged_payload.partition('.')[0]}.{signature}"
        with pytest.raises(ServiceTokenError, match="signature"):
            verify_token(tampered)

    def test_token_signed_with_another_secret_rejected(self):
        with patch.object(service_tokens.settings, "service_token_secret", "other-key"):
            token, _ = mint_token("sandbox-abc", 8000, GENERATION)
        with pytest.raises(ServiceTokenError, match="signature"):
            verify_token(token)

    @pytest.mark.parametrize(
        "bad", ["", "no-separator", ".", "abc.", ".abc", "!!!.???"]
    )
    def test_malformed_tokens_rejected(self, bad):
        with pytest.raises(ServiceTokenError):
            verify_token(bad)

    def test_missing_secret_fails_closed(self):
        with patch.object(service_tokens.settings, "service_token_secret", None):
            with pytest.raises(ServiceTokenError, match="required"):
                mint_token("sandbox-abc", 8000, GENERATION)
