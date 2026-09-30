"""Tests for per-sandbox service generations in SandboxManager."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchard_env.orchestrator.sandbox_manager import Sandbox, SandboxManager


def _make_sandbox() -> Sandbox:
    return Sandbox(
        sandbox_id="s1",
        namespace="sandbox-pods",
        image="python:3.11-slim",
        pod_name="sandbox-s1",
        block_network=True,
        cpu="1",
        memory="1Gi",
        ready=True,
    )


@pytest.fixture
def memory_manager():
    with patch("orchard_env.orchestrator.sandbox_manager.settings.use_redis", False):
        manager = SandboxManager(k8s_client=None)
        manager._sandboxes["s1"] = _make_sandbox()
        yield manager


class TestExposeAndRevoke:
    @pytest.mark.asyncio
    async def test_expose_creates_a_generation(self, memory_manager):
        generation, created = await memory_manager.expose_service("s1", 8000)
        assert generation
        assert created is True
        assert await memory_manager.get_service_generation("s1", 8000) == generation

    @pytest.mark.asyncio
    async def test_expose_is_idempotent_while_active(self, memory_manager):
        first = await memory_manager.expose_service("s1", 8000)
        second = await memory_manager.expose_service("s1", 8000)
        assert second == (first[0], False)

    @pytest.mark.asyncio
    async def test_reexpose_after_revoke_gets_a_new_generation(self, memory_manager):
        first, _ = await memory_manager.expose_service("s1", 8000)
        assert await memory_manager.revoke_service("s1", 8000) is True
        second, created = await memory_manager.expose_service("s1", 8000)
        assert created is True
        assert second != first

    @pytest.mark.asyncio
    async def test_list_services(self, memory_manager):
        await memory_manager.expose_service("s1", 8001)
        await memory_manager.expose_service("s1", 8000)
        assert await memory_manager.list_services("s1") == [8000, 8001]

    @pytest.mark.asyncio
    async def test_unknown_sandbox_returns_none(self, memory_manager):
        assert await memory_manager.expose_service("nope", 8000) is None
        assert await memory_manager.list_services("nope") is None

    @pytest.mark.asyncio
    async def test_limit_is_enforced(self, memory_manager):
        with patch(
            "orchard_env.orchestrator.sandbox_manager.settings."
            "max_services_per_sandbox",
            2,
        ):
            await memory_manager.expose_service("s1", 8000)
            await memory_manager.expose_service("s1", 8001)
            with pytest.raises(ValueError, match="MAX_SERVICES_PER_SANDBOX"):
                await memory_manager.expose_service("s1", 8002)

    @pytest.mark.asyncio
    async def test_concurrent_exposes_do_not_lose_ports(self, memory_manager):
        ports = list(range(8000, 8010))
        with patch(
            "orchard_env.orchestrator.sandbox_manager.settings."
            "max_services_per_sandbox",
            len(ports),
        ):
            await asyncio.gather(
                *(memory_manager.expose_service("s1", port) for port in ports)
            )
        assert await memory_manager.list_services("s1") == ports

    @pytest.mark.asyncio
    async def test_delete_removes_service_state(self, memory_manager):
        await memory_manager.expose_service("s1", 8000)
        await memory_manager._delete_sandbox_record("s1")
        assert "s1" not in memory_manager._services

    @pytest.mark.asyncio
    async def test_delete_clears_local_pod_watcher_cache(self, memory_manager):
        watcher = MagicMock()
        memory_manager._pod_watcher = watcher
        await memory_manager._delete_sandbox_record("s1")
        watcher.remove_sandbox.assert_not_called()
        # Public deletion owns watcher cleanup because it also tears down K8s.
        memory_manager._sandboxes["s1"] = _make_sandbox()
        memory_manager._background_delete_k8s_resources = AsyncMock()
        await memory_manager.delete_sandbox("s1")
        watcher.remove_sandbox.assert_called_once_with("s1")
