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

## Local GPU and cloud escalation

Select `ollama`, `vllm`, `sglang`, or `llamacpp` in `[engine]`; configure its nested
host and the exact installed model ID in `[intelligence]`. Defaults point at
loopback, and environment HTTP proxies are bypassed for desktop inference.

Routine tool calls, parsing, RAG, and state checks stay local. A turn escalates
only when its full estimated context plus tool schemas and output reservation
exceeds `desktop.local_context_tokens`, its retry count reaches two, or it carries
an explicit high-depth reasoning hint. Adjust the context budget for your model,
quantization, VRAM, and KV cache. Token estimates are approximate; no fixed local
percentage or model fit is guaranteed for every workload.

Set `OPENROUTER_API_KEY` to enable eligible escalations. `desktop.cloud_model`
uses an OpenRouter model ID (`openrouter/auto` by default). The router forwards the
same messages and tool schemas, including tool-call IDs and reasoning metadata.
No key is needed for routine local turns. Missing credentials produce a clear
error when escalation is required; traces and jobs remain local.
Set `desktop.cloud_enabled = false` for fully offline operation.

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
serially to avoid competing GPU sessions. Pending/interrupted jobs replay after
restart with at-least-once semantics: make side effects idempotent. Results and
failed traces persist locally. `desktop.agent_type` accepts `operative`,
`monitor_operative`, `native_openhands`, or `rlm`.

## Persistent memory

The primary memory backend uses Python `sqlite3`, FTS5/BM25, WAL, transactional
source replacement, checkpoints, and a durable ordered outbox. No native extension
or FAISS/ColBERT index is required for desktop memory. State defaults to
`~/.openvarak/memory.db`; `OPENVARAK_HOME` relocates runtime state.
Existing `~/.openjarvis` state is not automatically moved.

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

The daemon applies configured scanners, capability policies, and rate limits.
Browser calls require `network:fetch`; skill synthesis and automatic code repair
require `code:execute` plus `file:write`. Existing synthesized tools require
`code:execute` to run. Enabling the native-backed capability policy requires the
Rust extension below; failed policy initialization stops startup.

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
