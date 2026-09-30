# Tests

Two kinds of tests, kept deliberately separate.

## Unit tests — `tests/`

Offline, fast, fully mocked. No cluster, no orchestrator, no network.

```bash
python -m pytest              # everything (testpaths = tests)
python -m pytest tests/ -v
python -m pytest tests/test_exec_timeout.py::TestJobResult -v
```

| File | Covers |
| --- | --- |
| [`test_exec_timeout.py`](test_exec_timeout.py) | `JobResult` status semantics and the sync-exec polling fallback — in particular that a `"running"` status is **never** treated as complete |
| [`test_resources.py`](test_resources.py) | `GET /resources` and the CPU/memory quantity parsers |
| [`test_file_listing.py`](test_file_listing.py) | The file-listing response model — regression cover for the int-vs-str `size` bug that made `GET /files/list` return 500 for non-empty directories |
| [`test_service_tokens.py`](test_service_tokens.py) | Capability tokens — signature, expiry, generation binding, and fail-closed secret handling |
| [`test_service_proxy.py`](test_service_proxy.py) | Port validation, credential/header filtering, encoded URL construction, and access-log configuration |
| [`test_service_endpoints.py`](test_service_endpoints.py) | Real HTTP/WebSocket upstreams: origin isolation, revoke/expiry watchdogs, redirects, streaming limits, compressed bodies, raw paths, subprotocols, and handshake deadlines |
| [`test_service_allowlist.py`](test_service_allowlist.py) | In-memory generation rotation, limits, concurrency, and cache cleanup |
| [`test_redis_mutate.py`](test_redis_mutate.py) | Redis CAS, separate generation state, sandbox-incarnation binding, and pod-IP binding |
| [`test_service_security.py`](test_service_security.py) | Agent-port override, authenticated/network-isolated Redis, SDK key scoping, and capability log redaction |

## Integration scripts — `tests/integration/`

Manual scripts that need a **live orchestrator**. They are deliberately *not*
named `test_*.py` and are excluded from collection (`norecursedirs`), so
`pytest` never tries to import them — importing them would fire real network
calls at collection time.

```bash
export SANDBOX_BASE_URL="http://your-orchestrator-host"
export SANDBOX_API_KEY="your-api-key"
```

| Script | Purpose |
| --- | --- |
| [`soak.py`](integration/soak.py) | Long session with idle gaps — exercises HTTP keep-alive and connection-pool reuse. A regression shows up as a spurious `ConnectionError` after an idle period. |
| [`sandbox_tools.py`](integration/sandbox_tools.py) | 11 checks on the bundled agent harnesses: PATH ordering, read-only mount, login shell, `kubectl exec` reachability, and that `hermes` leaves the image's own `python` untouched |
| [`bench_concurrent.py`](integration/bench_concurrent.py) | Concurrency benchmark — P50/P90/P95/P99 latency for create/exec/delete |
| [`bench_concurrent_pty.py`](integration/bench_concurrent_pty.py) | The same, for PTY sessions |
| [`service_endpoint.py`](integration/service_endpoint.py) | Service endpoints against a live cluster: launch a server inside a sandbox, expose it, drive it over HTTP, confirm a large response is not truncated, and confirm revocation is immediate. Needs `ENABLE_SERVICE_ENDPOINTS=true`. |

```bash
python tests/integration/soak.py --mode both --rounds 50
python tests/integration/sandbox_tools.py
python tests/integration/bench_concurrent.py --help
python tests/integration/bench_concurrent_pty.py --help
```

Benchmarks write `timing_*.json` / `pty_timing_*.json` next to themselves; both
patterns are git-ignored.

## Known quirk: list-form commands

`sandbox.exec()` accepts `str | list[str]`, but the two paths disagree:

- **PTY** (`pty=True`) joins with `shlex.quote()` — quoting is preserved.
- **Normal exec** joins with a bare `" ".join()` in
  `orchestrator/exec_manager.py`, so any argument containing spaces or quotes is
  corrupted before it reaches `bash -c`.

So `exec(["python", "-c", 'print("hi")'])` works under `pty=True` but fails
without it. Pass a single pre-quoted string for non-PTY commands until this is
reconciled.

For everyday feature demos use [`../examples/`](../examples) instead — those are
written to be read, these are written to find regressions.
