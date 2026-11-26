# OpenVarak

A single-desktop autonomous agent runtime, forked from [OpenJarvis](https://github.com/open-jarvis/OpenJarvis).
Python package: `openvarak`. Primary CLI: `varak`. License: Apache-2.0; upstream contributor credits are retained.

## Start on your desktop

```bash
uv sync --extra browser
uv run playwright install chromium
uv run varak init
# Edit ~/.openvarak/config.toml: select your local engine and installed model.
uv run varak check
uv run varak
```

`varak` serves only `127.0.0.1:8765`. It starts without a running model server;
actual tasks require the selected inference server. No engine discovery, GPU
power polling, cron presets, or cloud client initialization occurs on this path.
`varak ask "your task"` executes a single task using the same runtime.
Use `varak --config path/to/profile.toml daemon` for an explicit profile.

The default profile is [configs/openvarak/desktop-autonomous.toml](configs/openvarak/desktop-autonomous.toml).
A copy ships in the Python package, so installed wheels do not depend on a source checkout.
The `jarvis` executable retains the legacy command suite during migration; it is deprecated.

## Durable execution and verified outcomes

The desktop executor now journals consequential actions, deduplicates webhook
event IDs, and separates model answers from independently verified outcomes.
Interrupted actions are quarantined for authoritative reconciliation rather
than blindly repeated. SQLite remains the default memory and execution store.

Optional Temporal workflows, a typed Pydantic AI agent, and a PostgreSQL action
and settlement ledger are available with `uv sync --extra durable`. See
[the setup, API contracts, and recovery procedure](docs/durable-desktop.md)
and [the optional local profile](configs/openvarak/durable-local.toml).

## Local GPU and optional cloud escalation

Select `ollama`, `vllm`, `sglang`, or `llamacpp` in `[engine]`; configure its nested
host and the exact installed model ID in `[intelligence]`. Defaults point at
loopback, and environment HTTP proxies are bypassed for desktop inference.

Cloud escalation is disabled by default. If explicitly enabled, a turn escalates
only when its full estimated context plus tool schemas and output reservation
exceeds `desktop.local_context_tokens`, its retry count reaches two, or it carries
an explicit high-depth reasoning hint. Adjust the context budget for your model,
quantization, VRAM, and KV cache. Token estimates are approximate; no fixed local
percentage or model fit is guaranteed for every workload.

Set `desktop.cloud_enabled = true` and `OPENROUTER_API_KEY` for eligible escalations. `desktop.cloud_model`
uses an OpenRouter model ID (`openrouter/auto` by default). The router forwards the
same messages and tool schemas, including tool-call IDs and reasoning metadata.
No key is needed for routine local turns. Missing credentials produce a clear
error when escalation is required; traces and jobs remain local.
Local inference does not prevent approved research/browser tools from using the internet.

`[cluster] enabled = false` rejects remote inference endpoints and performs no
LAN discovery. To opt into a manually configured LAN endpoint, enable the block,
list its exact URL in `remote_nodes`, and set the selected engine host to it.
This is an endpoint hook, not distributed scheduling or automatic load balancing.

## Trigger tasks immediately

```bash
curl http://127.0.0.1:8765/webhooks/trigger \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Inspect the project and store a progress summary"}'
# Returns HTTP 202 with an event id.
curl http://127.0.0.1:8765/webhooks/events/EVENT_ID
```

For explicit depth, include `"high_depth_reasoning": true`. Set
`VARAK_WEBHOOK_TOKEN` to require `Authorization: Bearer TOKEN`. Browser-origin
requests are rejected. n8n and other local scripts can call the endpoint directly.
A hosted Supabase webhook needs a separately configured relay/tunnel to reach a
loopback-only listener; OpenVarak does not expose the daemon publicly.

Jobs are committed before acknowledgement, bounded in memory, and executed
serially to avoid competing GPU sessions. Pending jobs recover after restart;
interrupted jobs require reconciliation. Whole-task retries are off by default.
Responses without acceptance contracts are `unverified`; file hashes, expected
JSON, or selected successful tool receipts can verify a contract. Results and
failed traces persist locally. `desktop.agent_type` accepts `operative`,
`monitor_operative`, `native_openhands`, `rlm`, or optional `pydantic`.

## Persistent memory

The primary memory backend uses Python `sqlite3`, FTS5/BM25, WAL, transactional
source replacement, checkpoints, and a durable ordered outbox. No native extension
or FAISS/ColBERT index is required for desktop memory. State defaults to
`~/.openvarak/memory.db`; `OPENVARAK_HOME` relocates runtime state.
Existing `~/.openjarvis` state is not automatically moved.

`store_evidence()` and `retrieve_evidence()` add sourced facts with content hashes,
access scopes, and expiry filtering before FTS ranking. Checkpoint/document
retrieval remains unchanged. PostgreSQL is optional for actions and accounting,
not required for local memory.

Supabase sync is optional and disabled by default. Apply
[deploy/supabase/memory.sql](deploy/supabase/memory.sql), set `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY`, pull `nomic-embed-text` into local Ollama, and set
`[tools.storage] supabase_enabled = true`. The worker embeds with Ollama's
`/api/embed` and syncs 768-dimensional vectors, documents, traces, and checkpoints.
Explicit `semantic_retrieve()` uses the SQL cosine-search RPC. Normal `retrieve()`
always uses local FTS and never waits for Supabase. Failed writes stay queued
across restarts. This is local-authoritative sync, not a multi-writer merge protocol.

## Persistent browser and live skills

`playwright_session` owns one persistent headless Chromium profile on a dedicated
thread. Tabs, DOM, cookies, and authentication survive agent turns; the profile
persists across daemon restarts. Playwright and Chromium are optional until used.

`synthesize_skill` accepts a name, description, Python source defining
`execute(**params)`, a JSON object parameter schema, test parameters, and an
optional expected result. It writes a valid Agent Skills `SKILL.md`,
`scripts/tool.py`, and tool manifest under `~/.openvarak/skills/synthesized/`.
Validated tools are registered in the current executor and `ToolRegistry` and
become visible on the next reasoning turn. Saved tools are revalidated on startup.

Docker must be running and the `python:3.12-slim` image must be available
(`docker pull python:3.12-slim`). Validation and every synthesized invocation run
in a fresh non-root container with no network, a read-only root filesystem,
restricted capabilities, resource limits, and a timeout. Generated Python is never
imported or executed on the daemon host. Skills currently use Python's standard
library; arbitrary dependency installation is not provided.

After three consecutive tool failures (configurable), the online optimizer uses
the failed trace to propose a repair. Synthesized code passes both the original
regression check and the failed invocation before replacing the live tool.
Built-in tools can receive revised prompt descriptions; their host code is not
automatically rewritten. Repeated task failures get one bounded optimizer-assisted
attempt. Policy denials terminate without recovery attempts.

## Verification and optional legacy features

```bash
uv sync --extra dev
uv run pytest tests/desktop -q
uv run varak --help
uv run varak check
```

The desktop tests cover routing/streaming, preserved tool-call state, SQLite
restart/offline behavior, outbox replay, webhook dispatch/recovery/backpressure,
hot skill registration and repair, browser ownership, and container arguments.
See [docs/openvarak-migration.md](docs/openvarak-migration.md) for compatibility
and verification limits.

Legacy research/benchmark modules are retained for reference but excluded from
the default CLI and dependency path. Install `--extra research` for datasets,
academic power instrumentation, or PostHog. Other upstream native-backed features
still require the renamed Rust extension:

```bash
uv run maturin develop -m rust/crates/openvarak-python/Cargo.toml
```
