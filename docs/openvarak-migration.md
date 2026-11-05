# OpenVarak migration and verification

## Implemented

- `src/openjarvis` → `src/openvarak`; Python imports, Rust crates/PyO3 bindings,
  package metadata, Tauri references, deployment files, examples, and tests updated.
  Upstream Apache licensing and historical changelog remain intact.
- `varak` is the desktop CLI. `jarvis` remains the deprecated legacy CLI.
  Public SDK classes such as `JarvisConfig`, `Jarvis`, and `JarvisSystem` retain
  their names for compatibility under the new package.
- Desktop profile is both source-controlled and bundled in the wheel. Consumer
  presets and the upstream profile are archived under `configs/archive`.
- Academic sensors, telemetry wrappers, and benchmark dependencies are excluded
  from the primary desktop path. Legacy modules remain available as opt-ins.
- Lazy OpenRouter escalation uses context budget, tool schemas/output reservation,
  retry count, and explicit depth hints. Routine loops remain local, including
  streaming. The same agent/message objects survive a routing decision.
- Loopback endpoint enforcement bypasses environment HTTP proxies. Explicit LAN
  allowlisting provides an opt-in endpoint hook, without discovery or balancing.
- Python SQLite FTS5/WAL memory, transactional source replacement, durable sync
  outbox, checkpoints, and traces. Optional background Ollama embeddings and
  Supabase pgvector sync; SQL migration and semantic-search RPC included.
- Durable bounded webhook queue, event status API, restart recovery, one active
  desktop agent, persistent Playwright profile with dedicated thread ownership.
- Agent Skills manifests and Python tool source validated in isolated Docker
  containers before persistence and live registration. Every execution stays in
  a container. Saved skills are revalidated on startup.
- Failed tool traces trigger bounded online repair; synthesized patches must pass
  regression validation before installation. Built-in descriptions can be revised.
  Repeated task failures receive one optimizer-assisted recovery attempt.
- Operative and monitor persistence now support the document-memory interface as
  well as legacy key/value stores.

## Verification completed in this environment

- `uv lock --check` passes.
- `uv run varak --help` and `uv run varak check` pass.
- Bare `uv run varak` starts and responds HTTP 200 at `/health` without a local
  GPU server or cloud credentials. Explicit daemon startup and graceful shutdown
  were also exercised.
- `ruff check src/ tests/` passes.
- `ruff format --check src/ tests/` passes (1,428 Python files).
- Focused compatibility lane: **469 passed, 5 live tests deselected**:

  ```bash
  NO_PROXY='*' uv run --extra dev pytest \
    tests/desktop tests/agents/test_monitor_operative.py \
    tests/operators/test_operators.py tests/engine/test_discovery.py \
    tests/core/test_config.py tests/skills \
    -q -m 'not live and not cloud' --disable-warnings
  ```

  This includes 34 desktop tests covering imports/entrypoint, local/cloud
  escalation and streaming, exact message identity and tool-call state, cloud
  laziness, endpoint allowlisting, persistent memory/offline operation, outbox
  failure/replay, manifest validation, live registration/repair, saved skill
  reload, webhook auth/dispatch/restart recovery/backpressure, bounded task
  optimization, browser thread ownership, and container isolation arguments.

- Expanded run including all `tests/core`: **684 passed, 5 failed, 5 deselected**.
  Three failures require the unavailable compiled `openvarak_rust` extension;
  two process-liveness tests cannot resolve child/current PIDs in this runner.
- An earlier broad agents/engines/tools run stopped at **659 passed, 32 failed,
  25 skipped, 1 collection error**. Failures included missing native Rust bindings
  and runner SOCKS-proxy support. The full upstream suite is not certified passing.

## Not live-verified here

There is no Docker executable, Rust toolchain, local GPU inference server,
Playwright browser installation, or Supabase credentials in this environment.
Container validation, browser persistence, Supabase replay, and provider HTTP
contracts are tested through controlled substitutes; real Docker/browser/GPU/
Supabase acceptance tests remain necessary on the desktop. Optional upstream
native-backed security and research features still need the renamed Rust build.

Token capacity is configurable and estimated, not automatically derived from
VRAM. Routing percentages depend on workload. LAN support is an explicit endpoint
hook. Cloud memory is local-authoritative synchronization, not distributed state
reconciliation. Restarted jobs are at-least-once and can repeat side effects.

## Initial GitHub publication attempt

Target: `77svene/openvarak`, base `324c9c3ac61bff07a0d99b715a7511dcd78600c4`.
The connected integration returned HTTP 403 (`Resource not accessible by
integration`) when creating `refactor/openvarak-desktop`. No remote branch, commit,
or pull request was created at the time of the initial handoff. Subsequent publication
can supersede that status. The local branch and deliverable contain the changes.
