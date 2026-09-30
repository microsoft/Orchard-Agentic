"""Shared authenticated Redis client construction and safe logging."""

from urllib.parse import urlsplit

import redis.asyncio as redis

from orchard_env.orchestrator.settings import settings


def create_redis_client(redis_url: str) -> redis.Redis:
    """Create a decoded-text Redis client, failing closed on missing auth."""
    password = settings.redis_password
    password_in_url = urlsplit(redis_url).password
    if settings.redis_require_auth and not password and not password_in_url:
        raise RuntimeError(
            "Redis authentication is required. Set REDIS_PASSWORD or include "
            "a password in REDIS_URL."
        )

    kwargs = {"encoding": "utf-8", "decode_responses": True}
    if password:
        kwargs["password"] = password
    return redis.from_url(redis_url, **kwargs)


def redis_log_target(redis_url: str) -> str:
    """Return a credential-free endpoint description for logs."""
    parsed = urlsplit(redis_url)
    host = parsed.hostname or "<unknown>"
    port = parsed.port or (6380 if parsed.scheme == "rediss" else 6379)
    database = parsed.path or "/0"
    return f"{parsed.scheme or 'redis'}://{host}:{port}{database}"
