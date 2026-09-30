#!/usr/bin/env python3
"""Integration check for sandbox service endpoints (needs a live orchestrator).

Starts a small HTTP server inside a real sandbox, exposes it, and drives it
from outside the cluster:

1. create a sandbox
2. write and launch a server on a port inside it
3. expose that port and wait for it to answer
4. GET through the proxy
5. confirm a large response survives streaming intact
6. confirm revocation takes effect immediately
7. confirm the in-pod agent port can never be exposed

Usage::

    export SANDBOX_BASE_URL="http://your-orchestrator-host"
    export SANDBOX_API_KEY="your-api-key"
    python tests/integration/service_endpoint.py

Requires ``ENABLE_SERVICE_ENDPOINTS=true``, ``SERVICE_PUBLIC_BASE_URL``, and
``SERVICE_TOKEN_SECRET`` on the orchestrator. WebSocket behavior is covered by
the route-level test suite against a real upstream server. Deliberately not
named ``test_*.py``: importing it fires real network calls.
"""

import argparse
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from orchard_env import SandboxClient  # noqa: E402

# Written into the sandbox and launched there. Kept dependency-free so it runs
# in any image with a Python interpreter.
SERVER_SOURCE = """
import http.server, json, socketserver, sys

PORT = int(sys.argv[1])

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "healthy"}).encode()
        elif self.path == "/big":
            body = ("x" * 1024 * 512).encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
    httpd.serve_forever()
"""

SERVICE_PORT = 8000


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="python:3.11-slim")
    parser.add_argument("--keep", action="store_true", help="do not delete the sandbox")
    args = parser.parse_args()

    if not os.environ.get("SANDBOX_BASE_URL"):
        print("SANDBOX_BASE_URL is not set", file=sys.stderr)
        return 2

    with SandboxClient() as client:
        print(f"Creating sandbox from {args.image} ...")
        sandbox = client.create_sandbox(args.image)
        try:
            print(f"Sandbox {sandbox.sandbox_id} ready")

            # 1. Put a server inside the sandbox and start it in the background.
            sandbox.upload_content(SERVER_SOURCE.encode(), "/tmp/service.py")
            sandbox.exec(
                f"nohup python /tmp/service.py {SERVICE_PORT} "
                "> /tmp/service.log 2>&1 & echo started",
                timeout=30,
            )

            # 2. Expose it. wait_ready matters: sandbox readiness only covers
            #    the in-pod agent, so the server may still be binding.
            endpoint = sandbox.expose_service(
                SERVICE_PORT, wait_ready=True, health_path="/health", ready_timeout=60
            )
            check("expose_service returned a URL", bool(endpoint.url))
            check("port matches", endpoint.port == SERVICE_PORT)
            check("expiry is in the future", endpoint.expires_at > time.time())

            # 3. The URL authenticates itself: no X-API-Key header here.
            response = requests.get(f"{endpoint.url}/health", timeout=30)
            check("GET /health through the proxy", response.status_code == 200)
            check("body is intact", response.json() == {"status": "healthy"})

            # 4. A large body must survive streaming without truncation.
            big = requests.get(f"{endpoint.url}/big", timeout=60)
            check("large response status", big.status_code == 200)
            check("large response length", len(big.content) == 1024 * 512)

            # 5. The listing reflects what is exposed.
            check("service is listed", SERVICE_PORT in sandbox.list_services())

            # 6. Revocation is immediate, even though the URL has not expired.
            sandbox.revoke_service(SERVICE_PORT)
            revoked = requests.get(f"{endpoint.url}/health", timeout=30)
            check("revoked URL is refused", revoked.status_code == 403)
            check(
                "service no longer listed", SERVICE_PORT not in sandbox.list_services()
            )

            # 7. The agent port must never be exposable.
            try:
                sandbox.expose_service(9090)
                check("agent port refused", False)
            except Exception:
                check("agent port refused", True)

            print("\nAll service endpoint checks passed.")
            return 0
        finally:
            if args.keep:
                print(f"Keeping sandbox {sandbox.sandbox_id}")
            else:
                sandbox.delete()


if __name__ == "__main__":
    raise SystemExit(main())
