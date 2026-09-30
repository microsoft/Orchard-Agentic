"""Proxy helpers for services running inside sandboxes.

The orchestrator already reaches pods directly for exec and file I/O. This
module extends that fast path to user services while preserving the destination
boundary: every connection remains pinned to the resolved pod IP and exposed
port, including across redirects.
"""

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterable, Iterable, Mapping
from typing import Any
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from orchard_env.orchestrator.settings import settings

logger = logging.getLogger(__name__)

HeaderPairs = list[tuple[str, str]]

# RFC 9110 section 7.6.1. Fields named by Connection are removed dynamically in
# addition to this fixed set.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# Never forward ambient management/browser credentials into hostile sandbox
# code. The capability URL already authenticates access to the service.
_REQUEST_DROP_HEADERS = frozenset(
    {
        "authorization",
        "content-length",
        "cookie",
        "host",
        "x-api-key",
    }
)

# A sandbox service must not write cookies for the shared service origin or
# broaden a service worker beyond its token-scoped path.
_RESPONSE_DROP_HEADERS = frozenset(
    {
        "service-worker-allowed",
        "set-cookie",
        "set-cookie2",
    }
)

# WebSocket metadata that is useful to the service and safe to forward.
_WEBSOCKET_HEADER_ALLOWLIST = frozenset(
    {
        "accept-language",
        "user-agent",
        "x-request-id",
    }
)


class ServicePortError(ValueError):
    """Raised when a requested port may not be exposed."""


class ServiceDestinationError(aiohttp.ClientError):
    """Raised when a redirect attempts to leave the pinned pod and port."""


class ServiceRequestTooLargeError(Exception):
    """Raised while streaming a request body beyond the configured limit."""


def validate_port(port: int) -> int:
    """Return *port* if it may be exposed, otherwise raise."""
    if not isinstance(port, int) or isinstance(port, bool):
        raise ServicePortError("Port must be an integer")
    if not 1 <= port <= 65535:
        raise ServicePortError(f"Port {port} is outside the valid range 1-65535")
    if port == settings.agent_port:
        raise ServicePortError(
            f"Port {port} is reserved for the in-pod sandbox agent and cannot "
            "be exposed"
        )
    if port in _reserved_ports():
        raise ServicePortError(f"Port {port} is reserved and cannot be exposed")
    return port


def _reserved_ports() -> set[int]:
    """Parse the operator-configured reserved port list."""
    raw = settings.service_reserved_ports
    if not raw:
        return set()
    ports: set[int] = set()
    for chunk in str(raw).replace(",", " ").split():
        try:
            ports.add(int(chunk))
        except ValueError:
            logger.warning(
                "Ignoring non-integer entry in SERVICE_RESERVED_PORTS: %r", chunk
            )
    return ports


def _normalise_header_pairs(headers: Any) -> HeaderPairs:
    """Return headers as a duplicate-preserving list of text pairs."""

    def text(value: Any) -> str:
        return value.decode("latin-1") if isinstance(value, bytes) else str(value)

    raw = getattr(headers, "raw", None)
    if raw is not None:
        return [(text(key), text(value)) for key, value in raw]
    if isinstance(headers, Mapping):
        return [(text(key), text(value)) for key, value in headers.items()]
    return [(text(key), text(value)) for key, value in headers]


def _connection_named_headers(pairs: Iterable[tuple[str, str]]) -> set[str]:
    """Return field names listed by any Connection header."""
    named: set[str] = set()
    for key, value in pairs:
        if key.lower() == "connection":
            named.update(
                part.strip().lower() for part in value.split(",") if part.strip()
            )
    return named


def filter_request_headers(headers: Any) -> HeaderPairs:
    """Filter client headers before they reach hostile sandbox code.

    Preserves duplicate ordinary headers, strips hop-by-hop fields (including
    fields named dynamically by ``Connection``), and removes ambient
    management/browser credentials.
    """
    pairs = _normalise_header_pairs(headers)
    dropped = HOP_BY_HOP_HEADERS | _REQUEST_DROP_HEADERS
    dropped = dropped | _connection_named_headers(pairs)
    return [(key, value) for key, value in pairs if key.lower() not in dropped]


def filter_response_headers(headers: Any) -> HeaderPairs:
    """Filter upstream headers while preserving duplicates and content metadata.

    The body is relayed byte-for-byte with decompression disabled, so
    ``Content-Encoding`` and ``Content-Length`` remain accurate.
    """
    pairs = _normalise_header_pairs(headers)
    dropped = HOP_BY_HOP_HEADERS | _RESPONSE_DROP_HEADERS
    dropped = dropped | _connection_named_headers(pairs)
    return [(key, value) for key, value in pairs if key.lower() not in dropped]


def filter_websocket_headers(headers: Any) -> HeaderPairs:
    """Return the safe subset of client WebSocket handshake headers."""
    pairs = _normalise_header_pairs(headers)
    return [
        (key, value)
        for key, value in pairs
        if key.lower() in _WEBSOCKET_HEADER_ALLOWLIST
    ]


def build_upstream_url(
    pod_ip: str,
    port: int,
    raw_path: str,
    raw_query_string: str = "",
    scheme: str = "http",
) -> URL:
    """Build an encoded URL without reinterpreting escaped path delimiters."""
    path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
    return URL.build(
        scheme=scheme,
        host=pod_ip,
        port=port,
        path=path or "/",
        query_string=raw_query_string,
        encoded=True,
    )


def build_service_url(base_url: str, token: str) -> str:
    """Build a per-capability origin from a wildcard URL template."""
    subdomain = hashlib.sha256(token.encode()).hexdigest()[:32]
    origin = base_url.replace("{subdomain}", subdomain).rstrip("/")
    return f"{origin}/s/{token}"


class _PinnedTCPConnector(aiohttp.TCPConnector):
    """Reject redirects that attempt to leave one pod IP and port."""

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pinned_host = host
        self._pinned_port = port

    async def _create_connection(self, req, *args, **kwargs):
        request_port = req.url.port or (
            443 if req.url.scheme in {"https", "wss"} else 80
        )
        if req.url.host != self._pinned_host or request_port != self._pinned_port:
            raise ServiceDestinationError(
                "WebSocket redirect attempted to leave the exposed sandbox service"
            )
        return await super()._create_connection(req, *args, **kwargs)


class ServiceWebSocket:
    """A WebSocket response that also owns its one-connection session."""

    def __init__(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        session: aiohttp.ClientSession,
    ) -> None:
        self._websocket = websocket
        self._session = session

    @property
    def protocol(self) -> str | None:
        return self._websocket.protocol

    @property
    def close_code(self) -> int | None:
        return self._websocket.close_code

    def __aiter__(self):
        return self._websocket.__aiter__()

    async def send_str(self, value: str) -> None:
        await self._websocket.send_str(value)

    async def send_bytes(self, value: bytes) -> None:
        await self._websocket.send_bytes(value)

    async def close(self) -> None:
        try:
            await self._websocket.close()
        finally:
            await self._session.close()


class ServiceProxyClient:
    """Forwards HTTP and WebSocket traffic to sandbox services."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=settings.service_proxy_pool_size,
                limit_per_host=settings.service_proxy_pool_limit_per_host,
                ttl_dns_cache=0,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(
                    total=None, connect=settings.service_proxy_connect_timeout
                ),
                auto_decompress=False,
            )
        return self._session

    async def request(
        self,
        method: str,
        pod_ip: str,
        port: int,
        raw_path: str,
        raw_query_string: str = "",
        headers: HeaderPairs | None = None,
        body: AsyncIterable[bytes] | bytes | None = None,
        timeout: float | None = None,
    ) -> aiohttp.ClientResponse:
        """Send an HTTP request without following redirects."""
        session = await self._get_session()
        url = build_upstream_url(
            pod_ip, port, raw_path, raw_query_string=raw_query_string
        )
        request_timeout = aiohttp.ClientTimeout(
            total=(
                timeout
                if timeout is not None
                else settings.service_proxy_timeout_seconds
            ),
            connect=settings.service_proxy_connect_timeout,
        )
        return await session.request(
            method,
            url,
            headers=headers or (),
            data=body,
            timeout=request_timeout,
            allow_redirects=False,
        )

    async def open_websocket(
        self,
        pod_ip: str,
        port: int,
        raw_path: str,
        raw_query_string: str = "",
        headers: HeaderPairs | None = None,
        protocols: tuple[str, ...] = (),
        origin: str | None = None,
    ) -> ServiceWebSocket:
        """Open a destination-pinned WebSocket.

        aiohttp follows WebSocket redirects by default. A connector dedicated to
        this socket rejects any redirected host or port before a connection is
        made, while still permitting a same-service path redirect.
        """
        connector = _PinnedTCPConnector(
            pod_ip,
            port,
            limit=1,
            ttl_dns_cache=0,
        )
        session = aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(
                total=None, connect=settings.service_proxy_connect_timeout
            ),
        )
        url = build_upstream_url(
            pod_ip,
            port,
            raw_path,
            raw_query_string=raw_query_string,
            scheme="ws",
        )
        try:
            websocket = await asyncio.wait_for(
                session.ws_connect(
                    url,
                    headers=headers or (),
                    protocols=protocols,
                    origin=origin,
                    max_msg_size=settings.service_proxy_max_message_bytes,
                    autoping=True,
                ),
                timeout=settings.service_proxy_handshake_timeout_seconds,
            )
        except BaseException:
            await session.close()
            raise
        return ServiceWebSocket(websocket, session)

    async def probe(
        self, pod_ip: str, port: int, path: str, timeout: float = 5.0
    ) -> bool:
        """Return True only for a direct 2xx response; never follow redirects."""
        session = await self._get_session()
        url = build_upstream_url(pod_ip, port, path)
        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout, connect=timeout),
                allow_redirects=False,
            ) as response:
                return 200 <= response.status < 300
        except Exception:
            return False

    async def close(self) -> None:
        """Close the pooled HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()


def encode_query(params: dict) -> str:
    """Encode a mapping as a query string (helper for tests and callers)."""
    return urlencode(params, doseq=True)
