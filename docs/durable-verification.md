# Durable execution verification — 25 November 2026

Base revision: `c9d045973ddeec29fde4d58eab868ff57964348b`.

The original runtime passed 34 desktop tests but controlled experiments showed
repeated fixture effects after task retry and process death, separate jobs for
the same supplied event ID, and completion based on a model claim without tool
receipts. The extension adds integrated regression cases for these conditions.

| Controlled condition | Original observation | Extended runtime |
|---|---|---|
| Effect followed by task failure | Effect repeated on task retry | One effect; no blind whole-task retry |
| Process death after external fixture commit | Effect repeated on recovery | Quarantine, authoritative receipt check, explicit resume; one total effect |
| Same incoming source/event/payload | Two distinct jobs | Same job returned; changed payload rejected |
| Answer claiming completion without evidence | Completed | Unverified by default |
| Late receipt after tool timeout | No generic action receipt boundary | Replay blocked; original worker receipt resolves action |
| Concurrent reservation requests | No generic budget store | Fifty attempts admit exactly ten reservations at the tested limit, on both databases |
| Scoped, expiring evidence | General FTS document retrieval only | Scope/freshness applied before ranking, persistent across restart |

These are deterministic fixture results. They establish execution-contract
improvements, not a measured improvement in LLM reasoning, GPU throughput,
business revenue, or arbitrary browser accuracy.

## Final supported regression run

```bash
python -m pytest tests/desktop tests/core/test_config.py \
  tests/core/test_config_key_validation.py tests/tools/test_tool_timeout.py \
  tests/tools/test_storage_tools.py tests/agents/test_monitor_operative.py \
  tests/agents/test_executor_tools.py -q --disable-warnings
```

**205 passed**, zero failures/errors/skips, three warnings, approximately
12.76 seconds in this runner. This includes **68 desktop tests**: the existing
34 with updated completion/retry expectations, plus 34 new extension cases.

Real optional-service coverage:

- Native PostgreSQL **16.2**, with isolated temporary schemas and TCP connections.
- Temporal SDK **1.37.0** running real workflows against its local test server.
- Pydantic AI **2.74.0**, with controlled model responses and a real loopback
  HTTP provider fixture using OpenAI SDK **3.59.0**.
- Psycopg **3.3.9** for the PostgreSQL journal and ledger tests.
- Actual child-process termination and receipt reconciliation through the
  integrated desktop ToolExecutor, followed by a resumed job.

The loopback HTTP fixture is not an actual LLM. No GPU server, live financial
transaction, customer payment, or authenticated third-party browser workflow was
used. The Compose template specifies PostgreSQL 17 and was structurally checked;
Docker deployment was not exercised in this runner.

## Other checks and limits

- `ruff check src/ tests/`: passed.
- `ruff format --check src/ tests/`: 1,434 files already formatted.
- `uv lock --check`: passed; the lock resolves 470 packages across optional profiles.
- Locked export of the `durable` profile: passed.
- Default/bundled profiles match; optional profile loads; Compose structure and
  `git diff --check` pass.

An earlier broader attempt including `tests/security/test_capabilities.py`
produced **207 passes and 27 failures**. Those 27 failures require the absent
compiled `openvarak_rust` module; no Rust toolchain is installed here. That native
capability suite is unverified, not reported as passing. Separate new tests
exercise the existing capability and confirmation gates with controlled policies
and prove that denied calls never reach the journal or tool.

The `durable` extra requires Python 3.11+ and is incompatible with the existing
pinned `inference-litellm` extra's OpenAI SDK 2 requirement. Both profiles remain
separately resolvable; the LiteLLM security pin is retained. Full instructions,
migration behavior, and API/receipt limitations are in
[durable desktop execution](durable-desktop.md).
