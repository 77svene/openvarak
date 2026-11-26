# Durable desktop execution

The existing SQLite desktop path now journals consequential tool calls,
deduplicates incoming events, and distinguishes an answer from a verified task.
An optional stack adds Temporal dispatch, a Pydantic AI agent, and a PostgreSQL
action/settlement ledger. SQLite remains the authoritative local memory and job
spool; enabling the optional stack does not migrate or delete existing memory.

## Install and run

The normal desktop installation still works without the extra services:

```bash
uv sync --extra browser
uv run varak
```

For the optional stack:

Use Python 3.11 or newer (tested here with 3.12). The ordinary desktop path
retains the project's Python 3.10 support; the durable dependencies are marked
for 3.11+ because current Pydantic AI requires it.

The `durable` extra and the legacy `inference-litellm` extra are separate
installation profiles: their current upstream OpenAI SDK requirements conflict.
The lockfile declares that conflict rather than weakening the existing LiteLLM
security pin. Do not combine those two extras in one environment.

```bash
uv sync --extra browser --extra durable
docker compose -f deploy/durable/compose.yml up -d
uv run varak --config configs/openvarak/durable-local.toml check
uv run varak --config configs/openvarak/durable-local.toml daemon
```

Before starting Compose, set `VARAK_DB_PASSWORD` to your own password. Set
`VARAK_POSTGRES_DSN` for the host worker, pointing to its local `openvarak`
database, user `openvarak`, and that password. Use a valid URI with escaped
credentials or a libpq connection string. Set `VARAK_WEBHOOK_TOKEN` for the
authenticated control API. Credentials are supplied through the environment,
not committed to this repository. Start a qualified tool-capable local model
server on port 8080 exposing the model alias `local-main`.

`check` validates local configuration and SQLite only. It does not claim that
Temporal, PostgreSQL, browser installation, or inference is ready. The health
endpoint reports the configured execution backends and `inference: not_checked`.
The Pydantic agent uses a loopback endpoint and no cloud fallback. The ordinary
agent also defaults to cloud escalation off; existing user profiles with
`cloud_enabled = true` retain their explicit setting.

The Compose file is an optional local deployment template, not a tested Omen
installation. Service integration tests use native PostgreSQL and a real local
Temporal test server. The Temporal container release is explicitly pinned;
pin the PostgreSQL image digest for reproducible deployment after qualification.
The supplied stack is a single-owner development deployment. Host/credential
isolation and operational backups remain deployment responsibilities.

## Jobs and acceptance

The trigger accepts an optional stable `event_id` and `source`. Repeating the
same event/payload returns its original job, including when the queue is full.
Reusing that event ID with a different payload is rejected. Events without an
ID remain independent jobs. An optional `expires_at` Unix timestamp prevents
starting expired jobs and dispatching new consequential tools after the deadline.

An ordinary conversational answer has status `unverified` by default. A job is
`completed` only after its selected acceptance contract passes. For example:

```json
{
  "prompt": "Write result.json in the configured workspace with {\"ok\":true}",
  "source": "owner",
  "event_id": "example-result-v1",
  "acceptance": {
    "kind": "json_equals",
    "path": "result.json",
    "expected": {"ok": true}
  }
}
```

Set `desktop.workspace_root` to the intended artifact directory, or use the
`workspace` directory beside the memory database. The agent must be told the
appropriate path. File contracts reject traversal and symlinks resolving outside
that workspace. Supported contracts are `json_equals`, `file_sha256`, and
`tool_receipts` (optionally specifying required tool names).

`tool_receipts` verifies successful tool results. It does not establish customer
acceptance, correct business outcomes, or payment. Choose a contract appropriate
to the task. Schema-validated Pydantic output also remains a claim until the
runtime's independent contract passes.

`require_verified_completion = false` is an explicit legacy compatibility mode.
`varak ask` prints conversational responses and labels their unverified status
instead of raising an unrelated missing-error exception.

## Action execution and recovery

The normal tool executor preserves its existing capability, confirmation, rate,
and taint checks, then claims an action before dispatch. The key includes durable
job identity, tool name, and canonical arguments; a new model call ID cannot
repeat the same recorded action. Within one job, identical consequential calls
are deliberately treated as the same action. A genuinely distinct repetition
requires a separately authorized job.

Unknown tools are treated as consequential. Trusted tool authors may set
`ToolSpec.metadata["side_effects"] = false` for read-only tools. Read tools are
not cached. Trusted adapters may declare integer `cost_minor`; a lifetime
reservation is enforced against `desktop.action_budget_minor` in an explicit
currency. Cost estimates do not infer the amount of an arbitrary browser
purchase. There are no live trading or payment adapters in this change.

Timeouts do not authorize replay. An original worker's late successful receipt
can resolve its own timed-out action. If the process dies, the OS releases the
runtime lease; startup changes interrupted jobs to `needs_reconciliation` and
marks their running actions unknown. Only pending, unstarted jobs are recovered
automatically. Whole-task retries are disabled by default. Legacy
`retry_tasks = true` is suitable only for known pure/retry-safe agents; even in
that mode, recording an action prevents a task-wide retry.

An adapter can implement `reconcile_action(**original_params) -> ToolResult`.
It must check authoritative external state and return the original tool name.
An empty lookup or a model's assertion does not establish success. The control
API provides:

- `GET /webhooks/events/{job_id}/actions` for receipt states;
- `POST /actions/{action_id}/reconcile` to run that trusted adapter check;
- `POST /webhooks/events/{job_id}/resume` for an explicit resume after all
  recorded actions are reconciled.

These endpoints use the existing webhook token/origin checks and cannot
arbitrarily mark an action successful. If an adapter has no authoritative probe,
the job stays quarantined. The process-crash test exercises the integrated
executor, reconciliation adapter, and resumed job, leaving exactly one fixture
effect.

## Temporal's role

Set `desktop.orchestrator = "temporal"` to dispatch the same persisted jobs as
Temporal workflows. Workflow IDs derive from durable job IDs and reject reuse;
repeated dispatch attaches to the existing execution. The local worker admits
one activity at a time. A whole-agent activity has one attempt, deliberately
avoiding unsafe automatic retry of arbitrary external effects. Temporal is not
an exactly-once payment mechanism.

Keep the same SQLite state volume and task queue for this single desktop worker.
This is not a distributed job-payload store or a multi-GPU scheduler. The local
spool is committed before acknowledgement; a failure to dispatch leaves that
job available for redispatch on restart. An interrupted activity is quarantined
rather than blindly restarted. Cancellation waits for the underlying Python
thread to stop before allowing it to release its activity slot.

## PostgreSQL and memory

Set `action_backend = "postgres"` and the configured DSN environment variable
to move action receipts, reservations, settlement records, and ledger entries
to PostgreSQL. Budget-row locks serialize reservations across database
connections. SQLite remains usable without PostgreSQL. No legacy action records
are copied automatically; choose the backend before enabling consequential work.

Trusted payment adapters can call `record_settlement` with a stable external
receipt ID, explicit currency, and actual settled amounts. Replaying the same
receipt is idempotent; changing its values is rejected. Ledger debit and credit
totals balance. `totals()` distinguishes reserved cost and settled contribution.
This is a foundation for accounting, not an integration with a payment provider;
refund workflows and tax accounting are not implemented.

SQLite memory adds `store_evidence` and `retrieve_evidence`. Facts carry a source
URI, observation/expiry times, a content hash, and scope. Freshness and scope
filters apply before FTS ranking/limits. Existing document/checkpoint retrieval
is unchanged, and evidence retrieval never waits for a cloud service. Scope is
a retrieval filter, not multi-user authorization, and a source URI alone does
not prove that stored text is true. These APIs are available to trusted tools
and collectors; a caller should preserve source provenance.

## Tests and limits

```bash
uv run --extra dev --extra durable pytest tests/desktop -q
```

For PostgreSQL integration tests, supply `VARAK_TEST_POSTGRES_DSN` for a dedicated
test database where temporary schemas may be created, or install `pgserver` in
the test environment to use bundled native binaries. Temporal tests start a
real local test server. Pydantic tests include both a controlled FunctionModel
and a loopback HTTP provider fixture. Neither is a live LLM quality benchmark.

Tests cover event collisions, claims without receipts, expiry, unknown outcomes,
late receipts, fifty concurrent reservation attempts, balanced/deduplicated
settlements, scoped/expired evidence, process death, reconciliation/resume,
Temporal dispatch, and the typed agent's existing-executor integration.
This does not establish improved model intelligence, GPU throughput, revenue,
or fully unattended recovery for actions without authoritative receipt adapters.

The durable-local profile enables `desktop.free_experiments` for the typed
agent. It requires a zero action budget and exposes configured local file tools,
the calculator, and keyless DuckDuckGo search. The model can write prototype
source files. Running generated programs requires a separately isolated runner.
The agent is instructed to find a free demand test or move to another idea.
Time, compute and electricity still count when comparing experiments.
