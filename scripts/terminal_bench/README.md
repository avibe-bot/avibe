# Full Avibe Runtime trial wrapper

This source-only tool drives **normal Avibe**, not an extracted agent core:

1. a fresh `AVIBE_HOME`, ordinary V2 config and ordinary service main;
2. the real controller-owned Model Hub IPC for Source and route setup;
3. `vibe agent update`, followed by `vibe agent run` and stable Run polling;
4. exact-Run cancellation on deadline and bounded owned-process shutdown;
5. `execution.json`, separate from a later Harbor verifier reward.

The native scheduler, Watches, Session/history/data-query machinery and Vibey
context compaction stay intact. No external IM platform, browser, Cloud pairing,
tunnel or Web UI is started. The loopback/internal Model Hub and control IPC are
real product services. No `Controller`, `Agent`, Session row or provider loop is
reimplemented here. This tool is **not** a sandbox.

## Prerequisites

Use a dedicated interpreter with the ordinary, pinned Avibe wheel installed,
including its normal dependencies. Verify the wheel SHA externally and keep it
in the experiment receipt. The interpreter chosen to launch this wrapper is
also used for the service and CLI. Do not run `uv` in the product repository in a
way that replaces its lock with Harbor dependencies.

For example, inside a fresh task container (with an init/reaper such as Docker's
`--init`), after installing the verified wheel into `/opt/avibe`:

```sh
# /opt/eval contains only this source-side scripts package, NOT a dataset,
# oracle solution, the user's home, or another agent's trajectory.
cd /opt/eval
/opt/avibe/bin/python -m scripts.terminal_bench.runner \
  --workdir /app \
  --state /var/lib/avibe-eval/trial-001 \
  --output /logs/agent/full-runtime \
  --profile /opt/eval/profile.json \
  --instruction /opt/eval/instruction.txt \
  --sandbox-environment
```

`state` and `output` must **not exist** yet. They and `workdir` must be disjoint
(no aliases, nesting, or filesystem root). Do not mount user state or the host
Docker socket. Source-code/test helpers, task instructions, raw service logs,
and model keys must not be collected as task outputs. Review the task's declared
artifacts to ensure they cannot copy the private state directory.

Example profile (freeze before trials; these are pilot limits, not the official
Terminal-Bench protocol):

```json
{
  "model": "deepseek-v4-pro",
  "base_url": "https://api.deepseek.com/v1",
  "credential_env": "DEEPSEEK_API_KEY",
  "protocol": "openai_chat",
  "reasoning_effort": "high",
  "context_window": 128000,
  "max_output_tokens": 32000,
  "wall_seconds": 1200,
  "setup_seconds": 600,
  "cleanup_seconds": 30,
  "poll_seconds": 1,
  "offline": false
}
```

Supply the named credential through an authorized secret injector, not argv or a
profile value. The supervisor passes it only to the short-lived bootstrap helper
on stdin, and that helper uses the ordinary Model Hub API. The service and tool
subprocess environment do not inherit the provider key. Real custody is still
Model Hub's responsibility. Tool processes share the container user; this is not
a key-security boundary against a malicious root process.

`--sandbox-environment` is only for an already isolated container: preserve the
image's HOME, XDG, PATH, Python/Rust/Java/conda toolchains and other task variables,
while clearing inherited Avibe routing and credential variables. Without it, the
wrapper uses a private host-test HOME. It never modifies its parent's environment.
This prevents tests from touching the developer's account; the host-test mode is
not a leaderboard-equivalent HOME configuration.

`offline=true` prevents managed binary downloads and is used in local tests. Real
runs with it require already installed/cached dependencies in that fresh home.
`offline=false` allows normal product dependency setup; arrange installer egress
in the sandbox setup phase. This wrapper does not replace Harbor's network policy
or impose a model-provider-only rule on tasks that legitimately need the network.

## Result and lifecycle contract

- A ready service with no supplied credential is `blocked`; no model Run or score
  is invented.
- Configure the isolated Agent's model/effort with the existing update command.
  There are no unsupported model override flags on non-forked `agent run`.
- Creation is asynchronous to capture the Run/Session IDs **before** waiting.
  This avoids mistaking CLI `--wait-timeout` for cancellation. A terminal Run
  does not automatically finish a trial: wait for the fresh home's real Harness
  to quiesce (including Watches, scheduled work and delegated runs). Check twice,
  and refuse incomplete/truncated snapshots. A delayed Watch note is covered by
  the integration test; it must not be killed at the initial final reply.
  `outcome` and `final_text` identify the initial Run, not an invented aggregate
  verdict over all continuations; grading still belongs to the verifier.
- `time_limit` requests cancellation of that exact Run. A terminal Run may still
  have Watch work, so the service and its recorded descendants are stopped before
  the final report is written. TERM reaches normal Controller shutdown; forced
  cleanup checks recorded process identity, including detached descendants.
- A failed cleanup prevents a normal handoff. Keep the surrounding sandbox alive
  until all writers are gone; if the supervisor is killed outright, its container
  owner must stop/remove the entire task container before any separate grading.
- Wrapper failures retain a bounded symbolic `error_code` (for example,
  `cli_helper_timeout` or `harness_quiescence_unconfirmed`) in `execution.json`
  and the CLI stdout/stderr summaries. Only wrapper-owned codes are accepted;
  arbitrary upstream exception text and captured helper output are never echoed.
- `outcome` is the product Run state, `time_limit`, `blocked`, or a wrapper failure;
  `verifier_reward` remains null. Do not treat wrapper/Harbor exit status as a
  success rate.
- `usage` is the product's **reported fresh-home usage, including bootstrap**,
  not a provider invoice. `accounting_complete=false` deliberately avoids claiming
  knowledge of unreported/cancelled provider work. No inferred zero-dollar cost.
- Only the structured report is exported. Raw SQLite, job/scratch state, secrets,
  setup files and runtime logs remain private. Model text in the report is
  redacted against the supplied key; it is not an unredacted token-delta stream.
- The report identifies the imported installation and whether source-checkout
  test mode or installed-package mode was used. Do not conflate their revisions.

## Tests

```sh
uv sync --python 3.12 --group dev --no-install-project --frozen
~/.local/bin/hd1-test .venv/bin/python -m pytest -q tests/test_terminal_bench_full_runtime.py
ruff check scripts/terminal_bench tests/test_terminal_bench_full_runtime.py tests/fixtures/full_runtime_service.py
```

The process test runs the real service, Model Hub aggregate/gateway, product CLI,
Run/Session persistence, Vibey and its tools. It substitutes **only the external
engine adapter** with the repository's existing deterministic fixture. It proves
Unicode note and SQLite task writes, module-shadow isolation, actual deadline
cancellation, no writes after cleanup, secret-safe exports, and no task-workspace
pollution. These are local integration tests, **not** acceptance of the real CPA
engine or a benchmark score. The parent's installed-wheel + real-engine + local
HTTP upstream check is a separate evidence layer.

The subprocess fixture has a separate 120-second setup budget for fresh SQLite
migrations on cold/contended filesystems. Its Agent execution budgets remain
30 seconds (normal) and 2 seconds (deadline cancellation); a service that exits
before readiness is still reported immediately. This is not a retry policy.

The startup updater log may initially show compatibility defaults (60 minutes,
`auto_update=true`). The real `_do_check()` reloads the saved config before its
interval-zero early return; the intervening askill reconcile is explicitly
disabled by this wrapper. That log alone does not prove updater egress. No product
updater behavior is changed by this integration.

Still required: parent-run installed-wheel wrapper smoke in the real sandbox,
Harbor installed-agent wiring (declare `atif=False` until conversion is implemented),
pinned task verification, same-profile paired trials, network-policy/resource
equivalence, review/CI, and cost assessment before any larger paid batch. ATIF
conversion is a later submission feature, not a prerequisite to an honest local
pilot with official verifier rewards and structured/native execution evidence.
