"""Signed capabilities for sandbox service endpoints.

A service URL is handed to clients that may not support custom authentication
headers, including an OpenEnv ``EnvClient`` opening a raw WebSocket. The URL
therefore carries a short-lived capability token.

The HMAC binds four values:

* sandbox ID
* port
* exposure generation
* expiration time

The generation changes whenever a revoked port is exposed again. This prevents
an old, unexpired URL from becoming valid after re-exposure.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from orchard_env.orchestrator.settings import settings

_SEPARATOR = "."
_TOKEN_VERSION = 1


class ServiceTokenError(Exception):
    """Raised when a token is malformed, forged, or expired."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _signing_secret() -> bytes:
    """Return the configured HMAC key, failing closed when it is absent."""
    configured = settings.service_token_secret
    if not configured:
        raise ServiceTokenError(
            "SERVICE_TOKEN_SECRET is required when service endpoints are enabled"
        )
    return configured.encode("utf-8")


def _encode_payload(
    sandbox_id: str, port: int, generation: str, expires_at: int
) -> bytes:
    payload: dict[str, Any] = {
        "v": _TOKEN_VERSION,
        "s": sandbox_id,
        "p": port,
        "g": generation,
        "e": expires_at,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def mint_token(
    sandbox_id: str,
    port: int,
    generation: str,
    ttl_seconds: int | None = None,
) -> tuple[str, float]:
    """Mint a capability for one exposure generation.

    Args:
        sandbox_id: Sandbox the token grants access to.
        port: Sandbox port the token grants access to.
        generation: Opaque nonce stored with the active exposure.
        ttl_seconds: Lifetime in seconds. Defaults to
            ``settings.service_token_ttl_seconds``.

    Returns:
        A ``(token, expires_at)`` pair, where ``expires_at`` is a Unix timestamp.
    """
    if not sandbox_id:
        raise ValueError("sandbox_id must not be empty")
    if not generation:
        raise ValueError("generation must not be empty")
    if ttl_seconds is None:
        ttl_seconds = settings.service_token_ttl_seconds
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")

    expires_at = int(time.time()) + int(ttl_seconds)
    payload = _encode_payload(sandbox_id, port, generation, expires_at)
    signature = hmac.new(_signing_secret(), payload, hashlib.sha256).digest()
    token = f"{_b64encode(payload)}{_SEPARATOR}{_b64encode(signature)}"
    return token, float(expires_at)


def verify_token(token: str) -> tuple[str, int, str, int]:
    """Verify and return ``(sandbox_id, port, generation, expires_at)``.

    Raises:
        ServiceTokenError: If the token is malformed, forged, expired, or uses
            an unsupported version.
    """
    if not token or _SEPARATOR not in token:
        raise ServiceTokenError("Malformed service token")

    encoded_payload, _, encoded_signature = token.partition(_SEPARATOR)
    try:
        payload = _b64decode(encoded_payload)
        signature = _b64decode(encoded_signature)
    except Exception as exc:
        raise ServiceTokenError("Malformed service token") from exc

    expected = hmac.new(_signing_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise ServiceTokenError("Invalid service token signature")

    try:
        decoded = json.loads(payload)
        version = decoded["v"]
        sandbox_id = decoded["s"]
        port = decoded["p"]
        generation = decoded["g"]
        expires_at = decoded["e"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ServiceTokenError("Malformed service token payload") from exc

    if version != _TOKEN_VERSION:
        raise ServiceTokenError("Unsupported service token version")
    if not isinstance(sandbox_id, str) or not sandbox_id:
        raise ServiceTokenError("Malformed service token payload")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ServiceTokenError("Malformed service token payload")
    if not isinstance(generation, str) or not generation:
        raise ServiceTokenError("Malformed service token payload")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise ServiceTokenError("Malformed service token payload")
    if time.time() > expires_at:
        raise ServiceTokenError("Service token has expired")

    return sandbox_id, port, generation, expires_at
