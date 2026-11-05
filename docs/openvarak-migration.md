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

## Initial implementation verification

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

## Initial live-verification limits

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

## Post-merge review fixes

- Centralized memory construction so CLI, SDK, builder, and server entry points
  honor the configured SQLite path and optional sync settings, including after
  registry resets. Optional vector stores still load only when selected.
- Restored inference trace events when telemetry is disabled and an adapter does
  not explicitly declare that it publishes its own events.
- Kept archived consumer profiles available only through explicit legacy CLI
  selection; the primary `varak init` default remains the desktop profile.
- Wired daemon scanners, capability policy, and rate limiter into the active
  agent/executor. Added capability requirements for persistent browsing, skill
  synthesis, synthesized execution, and automatic code repair.
- Prevented startup from waiting on inference when a recovered backlog exceeds a
  newly reduced queue size. Corrupt job payloads no longer kill the worker, and
  cleanup continues after an individual resource fails. Repeated start/close calls
  preserve one worker and do not reuse closed resources.
- Restored connector registry entries from cached classes without reloading their
  modules and discarding configured/injected adapters.
- Mocked search/RSS unit-test network dependencies and Sendblue webhook replies.
  Reply mocks stay attached to test channel instances, including background tasks.
  Added pytest-socket to development dependencies and the Linux CI test lane to
  block non-loopback Python sockets during unit tests.
- Extended packaging verification to require the desktop CLI module and bundled
  desktop profile in both source and wheel distributions.

Review status: PR #1 had no inline findings; Codex reported none and CodeRabbit
skipped its review. No GitHub Actions runs were recorded for the merged commit.
The fixes above came from direct source review and expanded local testing.
The follow-up Codex P2 finding about knowledge/knowledge_graph paths is addressed
by forwarding db_path to any backend constructor that explicitly accepts it.

Validation after these fixes:

- The renamed Rust extension built successfully with a locked dependency graph
  and imports correctly (rustc 1.99.1; no Rust source changes in this follow-up).
- **9,440 passed, 103 skipped** in the broad pytest lane, with live/cloud/hub/
  external-runtime/Modal tests excluded and external Python sockets blocked.
  Seven process/PID integration cases were explicitly deselected because this
  execution host's `/proc` does not expose its reported process IDs. Those tests
  remain enabled in CI and were not weakened in source.
- 29 new desktop review regression cases exercise memory configuration across
  entry points (including knowledge and knowledge_graph), registered persistent
  plugins, persistent queue recovery, corrupt jobs, cleanup, idempotent
  startup, connector adapter preservation, and policy enforcement during both
  synthesis and automatic repair.
- Ruff lint/format checks pass; packaging smoke checks pass; the daemon returns
  HTTP 200 at `/health` and shuts down cleanly.

The excluded host cases are the three `TestPidLiveness` cases concerning current
PID/registered PID/reused PID, both parameterizations of real descendant cleanup,
and the current-process liveness and real-process termination cases in
`tests/core/test_utils.py`. Live GPU, browser, Docker, and Supabase acceptance
checks still require the intended desktop/services.

## Initial GitHub publication attempt

Target: `77svene/openvarak`, base `324c9c3ac61bff07a0d99b715a7511dcd78600c4`.
The connected integration returned HTTP 403 (`Resource not accessible by
integration`) when creating `refactor/openvarak-desktop`. No remote branch, commit,
or pull request was created at the time of the initial handoff. Subsequent publication
can supersede that status. The local branch and deliverable contain the changes.
