"""End-to-end route tests for sandbox service endpoints.

The orchestrator app and proxy are real. Only Kubernetes-backed sandbox lookup
is replaced with a small in-memory manager.
"""

import asyncio
import gzip
import socket
import threading
import time
from contextlib import ExitStack, asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, patch

import pytest
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import (
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchard_env.orchestrator import service_proxy, service_tokens
from orchard_env.orchestrator.sandbox_manager import Sandbox

SANDBOX_ID = "sandbox-test"
API_KEY = "test-api-key"
SERVICE_ORIGIN_TEMPLATE = "http://{subdomain}.services.testserver"
AUTH = {"X-API-Key": API_KEY}


def _build_upstream_app(redirect_target: str | None = None) -> FastAPI:
    app = FastAPI()
    app.state.hits = 0
    app.state.ws_hits = 0
    app.state.last_protocols = ""

    @app.get("/health")
    def health():
        app.state.hits += 1
        return {"status": "healthy"}

    @app.get("/echo")
    def echo(request_value: str = "none"):
        return {"value": request_value}

    @app.post("/body")
    async def body(request: Request):
        return Response(await request.body(), media_type="application/octet-stream")

    @app.get("/stream")
    def stream():
        return StreamingResponse(
            (f"chunk-{index};" for index in range(5)), media_type="text/plain"
        )

    @app.get("/slow-stream")
    async def slow_stream():
        async def chunks():
            yield "start;"
            await asyncio.sleep(0.12)
            yield "end;"

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.get("/")
    def root():
        return PlainTextResponse("root-ok")

    @app.get("/gzipped")
    def gzipped():
        raw = b"compressible-payload;" * 200
        compressed = gzip.compress(raw)
        return Response(
            content=compressed,
            media_type="text/plain",
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
            },
        )

    @app.get("/teapot")
    def teapot():
        return PlainTextResponse("short and stout", status_code=418)

    @app.get("/request-headers")
    def request_headers(request: Request):
        return {
            "authorization": request.headers.get("authorization"),
            "cookie": request.headers.get("cookie"),
            "x_api_key": request.headers.get("x-api-key"),
            "x_custom": request.headers.get("x-custom"),
        }

    @app.get("/response-headers")
    def response_headers():
        response = PlainTextResponse("headers")
        response.raw_headers.extend(
            [
                (b"x-repeat", b"one"),
                (b"x-repeat", b"two"),
                (b"set-cookie", b"hostile=1; Domain=.example.com"),
                (b"service-worker-allowed", b"/"),
            ]
        )
        return response

    @app.api_route("/raw/{rest:path}", methods=["GET", "POST"])
    async def raw_path(request: Request, rest: str):
        return {
            "raw_path": request.scope["raw_path"].decode("ascii"),
            "raw_query": request.scope["query_string"].decode("ascii"),
        }

    @app.get("/redirect-health")
    def redirect_health():
        return RedirectResponse(f"{redirect_target}/health")

    @app.get("/relative-redirect")
    def relative_redirect():
        return RedirectResponse("/health")

    @app.get("/ws-redirect")
    def ws_redirect():
        return RedirectResponse(f"{redirect_target.replace('http:', 'ws:')}/ws")

    @app.get("/ws-hang")
    async def ws_hang():
        await asyncio.sleep(1)
        return PlainTextResponse("too late")

    @app.websocket("/ws")
    async def websocket_echo(ws: WebSocket):
        app.state.ws_hits += 1
        offered = ws.headers.get("sec-websocket-protocol", "")
        app.state.last_protocols = offered
        subprotocol = "openenv-v1" if "openenv-v1" in offered else None
        await ws.accept(subprotocol=subprotocol)
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("text") is not None:
                    if message["text"] == "close-please":
                        await ws.close(code=4200)
                        return
                    if message["text"] == "wait":
                        await asyncio.sleep(0.12)
                    await ws.send_text(f"echo:{message['text']}")
                elif message.get("bytes") is not None:
                    await ws.send_bytes(b"bin:" + message["bytes"])
        except Exception:
            pass

    return app


def _ws_implementation() -> str:
    try:
        import websockets  # noqa: F401

        return "websockets"
    except ImportError:
        pass
    try:
        import wsproto  # noqa: F401

        return "wsproto"
    except ImportError:
        return "none"


WS_AVAILABLE = _ws_implementation() != "none"
requires_websockets = pytest.mark.skipif(
    not WS_AVAILABLE,
    reason="needs a uvicorn WebSocket implementation",
)


@contextmanager
def running_upstream(app: FastAPI | None = None):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    app = app or _build_upstream_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            ws=_ws_implementation(),
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("upstream server did not start")
    try:
        yield port, app
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@contextmanager
def orchestrator_client(upstream_port: int, enabled: bool = True):
    import orchard_env.orchestrator.api as api_module

    sandbox = Sandbox(
        sandbox_id=SANDBOX_ID,
        namespace="sandbox-pods",
        image="python:3.11-slim",
        pod_name=f"sandbox-{SANDBOX_ID}",
        block_network=True,
        cpu="1",
        memory="1Gi",
        ready=True,
    )
    services: dict[int, str] = {}
    generation_counter = 0

    async def fake_get_sandbox(sandbox_id):
        return sandbox if sandbox_id == SANDBOX_ID else None

    async def fake_expose(sandbox_id, port):
        nonlocal generation_counter
        if sandbox_id != SANDBOX_ID:
            return None
        if port in services:
            return services[port], False
        generation_counter += 1
        services[port] = f"generation-{generation_counter}"
        return services[port], True

    async def fake_revoke(sandbox_id, port):
        return (
            services.pop(port, None) is not None if sandbox_id == SANDBOX_ID else False
        )

    async def fake_list(sandbox_id):
        return sorted(services) if sandbox_id == SANDBOX_ID else None

    async def fake_generation(sandbox_id, port):
        return services.get(port) if sandbox_id == SANDBOX_ID else None

    manager = AsyncMock()
    manager.get_sandbox.side_effect = fake_get_sandbox
    manager.expose_service.side_effect = fake_expose
    manager.revoke_service.side_effect = fake_revoke
    manager.list_services.side_effect = fake_list
    manager.get_service_generation.side_effect = fake_generation
    manager.get_pod_ip.return_value = "127.0.0.1"
    manager.get_current_pod_ip.return_value = "127.0.0.1"
    manager.heartbeat.return_value = True

    proxy_client = service_proxy.ServiceProxyClient()

    @asynccontextmanager
    async def noop_lifespan(_app):
        yield

    previous_lifespan = api_module.app.router.lifespan_context
    previous_manager = getattr(api_module, "sandbox_manager", None)
    previous_proxy = getattr(api_module, "service_proxy_client", None)
    api_module.app.router.lifespan_context = noop_lifespan
    api_module.sandbox_manager = manager
    api_module.service_proxy_client = proxy_client

    patches = [
        patch.object(api_module.settings, "enable_service_endpoints", enabled),
        patch.object(api_module.settings, "require_api_key", True),
        patch.object(api_module.settings, "api_keys", API_KEY),
        patch.object(
            api_module.settings,
            "service_public_base_url",
            SERVICE_ORIGIN_TEMPLATE,
        ),
        patch.object(api_module.settings, "service_allow_insecure_http", True),
        patch.object(api_module.settings, "service_token_secret", "test-secret"),
        patch.object(
            api_module.settings, "service_active_heartbeat_interval_seconds", 0.02
        ),
        patch.object(service_proxy.settings, "agent_port", 9090),
    ]
    try:
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            assert upstream_port != 9090
            with TestClient(api_module.app) as client:
                try:
                    yield client, services, manager
                finally:
                    client.portal.call(proxy_client.close)
    finally:
        api_module.app.router.lifespan_context = previous_lifespan
        if previous_manager is not None:
            api_module.sandbox_manager = previous_manager
        if previous_proxy is not None:
            api_module.service_proxy_client = previous_proxy


@pytest.fixture
def upstream():
    with running_upstream() as value:
        yield value


def _expose(client: TestClient, port: int, **overrides) -> str:
    response = client.post(
        f"/sandboxes/{SANDBOX_ID}/services",
        json={"port": port, **overrides},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()["url"]


class TestServiceLifecycle:
    def test_expose_list_and_revoke(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, services, _manager):
            url = _expose(client, port)
            assert ".services.testserver/s/" in url
            assert client.get(f"/sandboxes/{SANDBOX_ID}/services", headers=AUTH).json()[
                "ports"
            ] == [port]
            assert (
                client.delete(
                    f"/sandboxes/{SANDBOX_ID}/services/{port}", headers=AUTH
                ).status_code
                == 200
            )
            assert services == {}

    def test_expose_requires_management_api_key(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            response = client.post(
                f"/sandboxes/{SANDBOX_ID}/services", json={"port": port}
            )
            assert response.status_code == 401

    def test_agent_port_is_rejected(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            response = client.post(
                f"/sandboxes/{SANDBOX_ID}/services",
                json={"port": 9090},
                headers=AUTH,
            )
            assert response.status_code == 400

    def test_readiness_failure_does_not_create_exposure(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, services, _manager):
            response = client.post(
                f"/sandboxes/{SANDBOX_ID}/services",
                json={
                    "port": port,
                    "wait_ready": True,
                    "health_path": "/missing",
                    "ready_timeout": 1,
                },
                headers=AUTH,
            )
            assert response.status_code == 408
            assert services == {}

    def test_missing_base_url_fails_closed(self, upstream):
        import orchard_env.orchestrator.api as api_module

        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            with patch.object(api_module.settings, "service_public_base_url", None):
                response = client.post(
                    f"/sandboxes/{SANDBOX_ID}/services",
                    json={"port": port},
                    headers=AUTH,
                )
            assert response.status_code == 503

    def test_insecure_public_url_requires_explicit_opt_in(self, upstream):
        import orchard_env.orchestrator.api as api_module

        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            with patch.object(
                api_module.settings, "service_allow_insecure_http", False
            ):
                response = client.post(
                    f"/sandboxes/{SANDBOX_ID}/services",
                    json={"port": port},
                    headers=AUTH,
                )
            assert response.status_code == 503


class TestOriginIsolation:
    def test_capabilities_receive_different_browser_origins(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            first = _expose(client, port).split("/s/", 1)[0]
            second = _expose(client, port + 1).split("/s/", 1)[0]
            assert first != second

    def test_proxy_is_refused_on_management_origin(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            url = _expose(client, port)
            path = "/" + url.split("/", 3)[3]
            assert client.get(path).status_code == 404

    def test_management_api_is_refused_on_service_origin(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            service_origin = _expose(client, port).split("/s/", 1)[0]
            response = client.get(f"{service_origin}/health")
            assert response.status_code == 404

    def test_same_hostname_on_another_port_is_not_a_management_origin(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            service_origin = _expose(client, port).split("/s/", 1)[0]
            hostname = service_origin.split("://", 1)[1]
            response = client.get(f"http://{hostname}:9999/health")
            assert response.status_code == 404


class TestHttpProxy:
    def test_status_query_body_stream_and_compression(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            base = _expose(client, port)
            assert client.get(f"{base}/health").json() == {"status": "healthy"}
            assert client.get(f"{base}/echo?request_value=hello").json() == {
                "value": "hello"
            }
            assert client.post(f"{base}/body", content=b"payload").content == b"payload"
            assert client.get(f"{base}/teapot").status_code == 418
            assert client.get(f"{base}/stream").text == (
                "chunk-0;chunk-1;chunk-2;chunk-3;chunk-4;"
            )
            root = client.get(base, follow_redirects=False)
            assert root.status_code == 200
            assert root.text == "root-ok"
            assert client.get(f"{base}/gzipped").text == ("compressible-payload;" * 200)

    def test_management_credentials_are_not_forwarded(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            base = _expose(client, port)
            response = client.get(
                f"{base}/request-headers",
                headers={
                    "Authorization": "Bearer secret",
                    "Cookie": "session=secret",
                    "X-API-Key": "management-key",
                    "X-Custom": "safe",
                },
            )
            assert response.json() == {
                "authorization": None,
                "cookie": None,
                "x_api_key": None,
                "x_custom": "safe",
            }

    def test_hostile_browser_state_headers_are_stripped(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            response = client.get(f"{_expose(client, port)}/response-headers")
            assert "set-cookie" not in response.headers
            assert "service-worker-allowed" not in response.headers
            assert response.headers.get("x-repeat") == "one, two"

    def test_encoded_path_and_query_are_preserved(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            base = _expose(client, port)
            response = client.get(f"{base}/raw/a%2Fb%3Fc%23d?sig=a%2Fb%3Fc%23d")
            assert response.json() == {
                "raw_path": "/raw/a%2Fb%3Fc%23d",
                "raw_query": "sig=a%2Fb%3Fc%23d",
            }

    def test_management_api_key_is_removed_from_query(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            base = _expose(client, port)
            response = client.get(f"{base}/raw/value?api_key={API_KEY}&sig=a%2Fb")
            assert response.json()["raw_query"] == "sig=a%2Fb"

    def test_request_size_limit(self, upstream):
        import orchard_env.orchestrator.api as api_module

        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            base = _expose(client, port)
            with patch.object(
                api_module.settings, "service_proxy_max_request_bytes", 4
            ):
                response = client.post(f"{base}/body", content=b"12345")
            assert response.status_code == 413

    def test_chunked_request_size_limit(self, upstream):
        import orchard_env.orchestrator.api as api_module

        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            base = _expose(client, port)
            with patch.object(
                api_module.settings, "service_proxy_max_request_bytes", 4
            ):
                response = client.post(f"{base}/body", content=iter([b"12", b"345"]))
            assert response.status_code == 413

    def test_active_stream_refreshes_heartbeat(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, manager):
            base = _expose(client, port)
            assert client.get(f"{base}/slow-stream").text == "start;end;"
            # One initial refresh plus at least one active-session refresh.
            assert manager.heartbeat.await_count >= 2

    def test_upstream_error_does_not_leak_pod_address(self, upstream):
        port, _app = upstream
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        with orchestrator_client(port) as (client, _services, _manager):
            response = client.get(f"{_expose(client, dead_port)}/health")
            assert response.status_code == 502
            assert response.json()["detail"] == "Service unreachable"
            assert str(dead_port) not in response.text


class TestCapabilities:
    def test_forged_and_unexposed_tokens_are_rejected(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            forged_url = service_proxy.build_service_url(
                SERVICE_ORIGIN_TEMPLATE, "not-a-real-token"
            )
            assert client.get(f"{forged_url}/health").status_code == 403
            token, _ = service_tokens.mint_token(SANDBOX_ID, port, "not-active")
            unexposed_url = service_proxy.build_service_url(
                SERVICE_ORIGIN_TEMPLATE, token
            )
            assert client.get(f"{unexposed_url}/health").status_code == 403

    def test_revoke_then_reexpose_does_not_revive_old_url(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            old_url = _expose(client, port)
            assert client.get(f"{old_url}/health").status_code == 200
            client.delete(f"/sandboxes/{SANDBOX_ID}/services/{port}", headers=AUTH)
            new_url = _expose(client, port)
            assert new_url != old_url
            assert client.get(f"{old_url}/health").status_code == 403
            assert client.get(f"{new_url}/health").status_code == 200


class TestSessionWatchdog:
    @pytest.mark.asyncio
    async def test_revocation_closes_an_active_session(self):
        import orchard_env.orchestrator.api as api_module

        manager = AsyncMock()
        manager.get_service_generation.return_value = None
        close = AsyncMock()
        with (
            patch.object(api_module, "sandbox_manager", manager),
            patch.object(
                api_module.settings, "service_active_heartbeat_interval_seconds", 0.01
            ),
        ):
            await asyncio.wait_for(
                api_module._maintain_service_session(
                    SANDBOX_ID,
                    8000,
                    "generation",
                    int(time.time()) + 60,
                    close_session=close,
                ),
                timeout=0.2,
            )
        close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_state_store_failure_closes_an_active_session(self):
        import orchard_env.orchestrator.api as api_module

        manager = AsyncMock()
        manager.get_service_generation.side_effect = RuntimeError("redis unavailable")
        close = AsyncMock()
        with (
            patch.object(api_module, "sandbox_manager", manager),
            patch.object(
                api_module.settings, "service_active_heartbeat_interval_seconds", 0.01
            ),
        ):
            await asyncio.wait_for(
                api_module._maintain_service_session(
                    SANDBOX_ID,
                    8000,
                    "generation",
                    int(time.time()) + 60,
                    close_session=close,
                ),
                timeout=0.2,
            )
        close.assert_awaited_once()


class TestRedirectPinning:
    def test_external_http_redirect_is_blocked(self):
        victim = _build_upstream_app()
        with running_upstream(victim) as (victim_port, victim_app):
            target = f"http://127.0.0.1:{victim_port}"
            primary = _build_upstream_app(target)
            with running_upstream(primary) as (primary_port, _primary_app):
                with orchestrator_client(primary_port) as (
                    client,
                    _services,
                    _manager,
                ):
                    response = client.get(
                        f"{_expose(client, primary_port)}/redirect-health"
                    )
                    assert response.status_code == 502
                    assert victim_app.state.hits == 0

    def test_relative_redirect_stays_beneath_capability_url(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            response = client.get(f"{_expose(client, port)}/relative-redirect")
            assert response.status_code == 200
            assert response.json() == {"status": "healthy"}

    def test_readiness_probe_does_not_follow_redirect(self):
        victim = _build_upstream_app()
        with running_upstream(victim) as (victim_port, victim_app):
            target = f"http://127.0.0.1:{victim_port}"
            primary = _build_upstream_app(target)
            with running_upstream(primary) as (primary_port, _primary_app):
                with orchestrator_client(primary_port) as (
                    client,
                    services,
                    _manager,
                ):
                    response = client.post(
                        f"/sandboxes/{SANDBOX_ID}/services",
                        json={
                            "port": primary_port,
                            "wait_ready": True,
                            "health_path": "/redirect-health",
                            "ready_timeout": 1,
                        },
                        headers=AUTH,
                    )
                    assert response.status_code == 408
                    assert victim_app.state.hits == 0
                    assert services == {}

    @requires_websockets
    def test_websocket_redirect_cannot_leave_pinned_port(self):
        victim = _build_upstream_app()
        with running_upstream(victim) as (victim_port, victim_app):
            target = f"http://127.0.0.1:{victim_port}"
            primary = _build_upstream_app(target)
            with running_upstream(primary) as (primary_port, _primary_app):
                with orchestrator_client(primary_port) as (
                    client,
                    _services,
                    _manager,
                ):
                    base = _expose(client, primary_port).replace("http:", "ws:")
                    with pytest.raises(WebSocketDisconnect) as excinfo:
                        with client.websocket_connect(f"{base}/ws-redirect"):
                            pass
                    assert excinfo.value.code == 4503
                    assert victim_app.state.ws_hits == 0


class TestWebSocketProxy:
    @requires_websockets
    def test_text_binary_subprotocol_close_and_heartbeat(self, upstream):
        port, app = upstream
        with orchestrator_client(port) as (client, _services, manager):
            base = _expose(client, port).replace("http:", "ws:")
            with client.websocket_connect(
                f"{base}/ws",
                subprotocols=[f"api_key.{API_KEY}", "openenv-v1"],
            ) as ws:
                assert ws.accepted_subprotocol == "openenv-v1"
                assert app.state.last_protocols == "openenv-v1"
                ws.send_text("hello")
                assert ws.receive_text() == "echo:hello"
                ws.send_bytes(b"\x00\x01")
                assert ws.receive_bytes() == b"bin:\x00\x01"
                ws.send_text("wait")
                assert ws.receive_text() == "echo:wait"
                assert manager.heartbeat.await_count >= 2
                ws.send_text("close-please")
                with pytest.raises(WebSocketDisconnect) as excinfo:
                    ws.receive_text()
                assert excinfo.value.code == 4200

    @requires_websockets
    def test_revocation_closes_an_established_socket(self, upstream):
        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            base = _expose(client, port).replace("http:", "ws:")
            with client.websocket_connect(f"{base}/ws") as ws:
                client.delete(f"/sandboxes/{SANDBOX_ID}/services/{port}", headers=AUTH)
                with pytest.raises(WebSocketDisconnect) as excinfo:
                    ws.receive_text()
                assert excinfo.value.code == 4403

    @requires_websockets
    def test_handshake_has_a_deadline(self, upstream):
        import orchard_env.orchestrator.api as api_module

        port, _app = upstream
        with orchestrator_client(port) as (client, _services, _manager):
            base = _expose(client, port).replace("http:", "ws:")
            with patch.object(
                api_module.settings,
                "service_proxy_handshake_timeout_seconds",
                0.05,
            ):
                with pytest.raises(WebSocketDisconnect) as excinfo:
                    with client.websocket_connect(f"{base}/ws-hang"):
                        pass
            assert excinfo.value.code == 4503
