"""Deployment-level security invariants used by service endpoints."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import requests
import yaml

from orchard_env.client.sandbox_client import _ScopedApiKeySession
from orchard_env.orchestrator.k8s_client import K8sClient
from orchard_env.orchestrator.redis_connection import redis_log_target
from orchard_env.orchestrator.redis_job_store import RedisJobStore
from orchard_env.orchestrator.redis_store import RedisSandboxStore
from orchard_env.orchestrator.utils import CapabilityPathRedactionFilter

ROOT = Path(__file__).resolve().parents[1]


def test_sdk_api_key_is_scoped_to_management_origin():
    session = _ScopedApiKeySession("https://api.example.com", "secret")
    management = session.prepare_request(
        requests.Request("GET", "https://api.example.com/health")
    )
    service = session.prepare_request(
        requests.Request("GET", "https://cap.sandboxes.example.net/s/token")
    )
    assert management.headers["X-API-Key"] == "secret"
    assert "X-API-Key" not in service.headers


def test_capability_path_is_redacted_from_log_message_and_arguments():
    import logging

    token = "payload.signature"
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        'WebSocket "%s"',
        (f"/s/{token}/ws?x=1",),
        None,
    )
    assert CapabilityPathRedactionFilter().filter(record)
    assert token not in record.getMessage()
    assert "/s/<redacted>/ws?x=1" in record.getMessage()


@pytest.mark.asyncio
async def test_sandbox_pod_overrides_image_agent_port():
    """An arbitrary image's AGENT_PORT cannot move the trusted agent."""
    captured = {}
    k8s = object.__new__(K8sClient)
    core = SimpleNamespace(create_namespaced_pod=object())
    k8s._get_core_v1_api = lambda: core

    async def k8s_call(_method, **kwargs):
        captured.update(kwargs)

    k8s._k8s_call = k8s_call
    with (
        patch(
            "orchard_env.orchestrator.k8s_client.settings.enable_sandbox_tools",
            False,
        ),
        patch(
            "orchard_env.orchestrator.k8s_client.settings.agent_port",
            9090,
        ),
    ):
        await k8s.create_pod(
            name="sandbox-s1",
            namespace="sandbox-pods",
            image="hostile-image:latest",
            sandbox_id="s1",
        )

    container = captured["body"].spec.containers[0]
    assert {item.name: item.value for item in container.env} == {"AGENT_PORT": "9090"}
    assert container.ports[0].container_port == 9090


def test_redis_network_policy_allows_only_orchestrator_pods():
    documents = list(yaml.safe_load_all((ROOT / "k8s/redis.yaml").read_text()))
    policy = next(item for item in documents if item["kind"] == "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app": "redis"}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    peer = policy["spec"]["ingress"][0]["from"][0]
    assert peer == {"podSelector": {"matchLabels": {"app": "sandbox-orchestrator"}}}
    assert policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 6379}]


@pytest.mark.asyncio
async def test_redis_authentication_fails_closed_when_missing():
    store = RedisSandboxStore("redis://redis.example:6379/0")
    with (
        patch("orchard_env.orchestrator.redis_store.settings.redis_require_auth", True),
        patch("orchard_env.orchestrator.redis_store.settings.redis_password", None),
    ):
        with pytest.raises(RuntimeError, match="authentication is required"):
            await store.connect()


@pytest.mark.asyncio
async def test_redis_password_is_passed_out_of_band():
    store = RedisSandboxStore("redis://redis.example:6379/0")
    client = AsyncMock()
    with (
        patch("orchard_env.orchestrator.redis_store.settings.redis_require_auth", True),
        patch("orchard_env.orchestrator.redis_store.settings.redis_password", "secret"),
        patch(
            "orchard_env.orchestrator.redis_connection.redis.from_url",
            return_value=client,
        ) as from_url,
    ):
        await store.connect()

    assert from_url.call_args.kwargs["password"] == "secret"
    client.ping.assert_awaited_once()


@pytest.mark.asyncio
async def test_password_embedded_in_redis_url_is_preserved():
    store = RedisSandboxStore("redis://:url-secret@redis.example:6379/0")
    client = AsyncMock()
    with (
        patch("orchard_env.orchestrator.redis_store.settings.redis_require_auth", True),
        patch("orchard_env.orchestrator.redis_store.settings.redis_password", None),
        patch(
            "orchard_env.orchestrator.redis_connection.redis.from_url",
            return_value=client,
        ) as from_url,
    ):
        await store.connect()

    assert "password" not in from_url.call_args.kwargs


@pytest.mark.asyncio
async def test_redis_job_store_uses_shared_authenticated_factory():
    store = RedisJobStore("redis://redis.example:6379/0")
    client = AsyncMock()
    with (
        patch(
            "orchard_env.orchestrator.redis_connection.settings.redis_require_auth",
            True,
        ),
        patch(
            "orchard_env.orchestrator.redis_connection.settings.redis_password",
            "secret",
        ),
        patch(
            "orchard_env.orchestrator.redis_connection.redis.from_url",
            return_value=client,
        ) as from_url,
    ):
        await store.connect()

    assert from_url.call_args.kwargs["password"] == "secret"
    client.ping.assert_awaited_once()


def test_redis_log_target_never_contains_credentials():
    target = redis_log_target("rediss://user:secret@redis.example:6380/2")
    assert target == "rediss://redis.example:6380/2"
    assert "user" not in target
    assert "secret" not in target
