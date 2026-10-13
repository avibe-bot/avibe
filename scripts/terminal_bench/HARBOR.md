# Optional Harbor bridge

`harbor_agent.FullAvibeAgent` is a source-only `BaseInstalledAgent` for the full
runtime wrapper in [README.md](README.md). It declares **`atif=False`**. It does
not claim leaderboard submission or ATIF support; a local official verifier
reward is possible without them.

## Host setup

Use a **separate** evaluation-host environment. Harbor's dependencies must not
change the product `pyproject.toml`, `uv.lock`, or wheel:

```sh
# From this checkout, in a dedicated venv:
uv pip install --python /path/to/eval-venv/bin/python \
  -r scripts/terminal_bench/requirements-harbor.txt
export PYTHONPATH="$(pwd)"  # Evaluation HOST only; never passed into the task.
```

Example Harbor job file (replace paths, SHA and profile values explicitly):

```json
{
  "job_name": "avibe-pilot-one",
  "jobs_dir": "/path/to/private-job-artifacts",
  "n_concurrent_trials": 1,
  "n_attempts": 1,
  "agents": [{
    "import_path": "scripts.terminal_bench.harbor_agent:FullAvibeAgent",
    "model_name": "deepseek-v4-pro",
    "override_timeout_sec": 1800,
    "kwargs": {
      "wheel_path": "/path/to/avibe_os-<pinned-version>-py3-none-any.whl",
      "wheel_sha256": "<verified-64-character-lowercase-sha256>",
      "profile": {
        "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com/v1",
        "credential_env": "DEEPSEEK_API_KEY",
        "protocol": "openai_chat",
        "reasoning_effort": "high",
        "context_window": 128000,
        "max_output_tokens": 32000,
        "wall_seconds": 1800,
        "setup_seconds": 600,
        "cleanup_seconds": 30,
        "poll_seconds": 1,
        "offline": false
      }
    }
  }],
  "tasks": [{"path": "/path/to/pinned-official-task"}],
  "environment": {"type": "docker", "delete": true}
}
```

Run with the named credential injected into the evaluation HOST by an authorized
secret injector. Do not put its value in job JSON or argv. Harbor 0.24's Docker
`exec(env=...)` itself expands to `docker compose exec -e KEY=value` on the host,
so this adapter deliberately does **not** pass credential values through exec
env. It reads the named host environment variable via Harbor's `_get_env` and
uses the narrow ephemeral-file transport below instead.

```sh
/path/to/eval-venv/bin/harbor run --config pilot.json --max-retries 0
```

The adapter requires Harbor 0.24.0, a trusted local **ordinary Avibe wheel**, its
explicit SHA256, a Profile, and a matching explicit model. It hashes an immutable
copy **before uploading anything**. It installs the wheel into a private venv,
not editable source. Only six tracked wrapper files are copied beside it;
product packages, tests, oracle solutions and user home are never source-uploaded.
Wheel version/SHA, actual Harbor version, wrapper commit/hash/dirty state and
execution metadata are recorded. Real paired runs should use a clean committed
checkout and an independently verified pinned wheel.

## Ephemeral API-key transport

This is temporary transport to the ordinary Model Hub Source-create API, **not**
a Vault/key-custody replacement or a generic secret-export feature.

- First pilot supports **root task users only**; nonroot images fail explicitly
  before installation rather than changing task ownership silently.
- Claim a unique trial root and its credential subdirectory mode0700. Create one
  host mode0600 `provider.key` under a mode0700 tempfile directory; transfer only
  with Harbor `upload_file`, never a shell literal, echo, heredoc, env value, or
  JSON configuration. Inspect remote regular-file/type/owner/mode/size metadata
  without printing or comparing secret content in logs.
- The wrapper opens without following links, rejects nonregular/multiply-linked,
  oversized or improperly protected files, reads and unlinks the input **before
  starting the service**, then uses the existing private-stdin bootstrap. Provider
  keys never enter wrapper/service/tool exec env or argv in this Harbor mode.
- A cancelled upload is joined before cleanup, so late copy completion cannot
  recreate a deleted secret. Upload/read/metadata failures, cancellation and
  timeout clear that exact owned remote file and host tempfile. Unknown remote
  cleanup blocks handoff and requires container destruction. No recursive
  deletion or another trial's path is used.
- Direct wrapper invocation may retain its named-environment mode, but the Docker
  exec bridge always supplies `--credential-file` containing only a private path.

## Task and lifetime boundary

- POSIX/Linux task containers with Python >=3.10 (Harbor may bootstrap Python and
  venv) are the first supported target. Windows is not claimed.
- Setup installs the normal wheel/dependencies once. Model Hub/bootstrap work
  inside `run()` counts against Harbor's outer task deadline: equal wall limits
  are **not** a claim of equal model-active time between Agent implementations.
- Resolve the actual task cwd with `pwd -P`; do not assume `/app` or `/root`.
  The original task HOME/toolchains are retained. A private directory containing
  only the installed `vibe` executable is prefixed to PATH, not the Python venv.
- Source/venv/state live in a unique `/installed-agent/avibe-full-*` directory;
  exported reports are under `/logs/agent/full-runtime-*`. Reject overlapping
  task roots rather than silently contaminating them.
- Model requests still use the real product Model Hub. The adapter does not
  override the official task's network/resources or secretly forbid legitimate
  task Internet use. Supply the provider endpoint as an agent-phase allowance
  in the normal Harbor job/network policy where needed; installer egress belongs
  to setup. Do not change the official task's verifier/artifact declaration.
- Killing a `docker exec` client is insufficient. A small in-container launcher
  records the exact wrapper PID/creation identity/nonce under a locked start
  fence. Cancellation first fences a late start, then signals the exact wrapper,
  waits for it to exit and requires its inner owned-process-cleanup receipt.
  The adapter joins this path before downloading reports or letting Harbor grade.
- A missing/mismatched/unconfirmed shutdown receipt is an infrastructure error,
  **not a task failure score**. The outer owner must destroy that sandbox before
  any grading; forced wrapper death alone cannot prove a detached engine/job
  stopped. Use an init/reaper in the container. A cancelled-before-start fence
  does not invent an execution report.

`execution.json` is projected to Harbor AgentContext metadata and token counts
only when the product reports tokens. Cost stays null (unknown provider bill),
accounting completeness stays explicit, and verifier reward stays separate.
No model text is passed to Harbor's regex-based CLI error classifier. Reports,
not the runner's exit status, identify completion/failure/time limit.

## Focused optional tests

Normal product tests skip the Harbor-only file when Harbor is absent. Run it
explicitly in the isolated optional environment after installing product dev
test prerequisites and `requirements-harbor.txt`:

```sh
~/.local/bin/hd1-test /path/to/eval-venv/bin/python -m pytest -q \
  tests/test_terminal_bench_harbor.py tests/test_terminal_bench_launch.py \
  tests/test_terminal_bench_credential.py
```

The tests use Harbor's actual class/model interfaces, a recording Environment
transport, and owned local processes. They verify wheel mismatch refusal before
mutation, restricted source transfer, explicit model/profile binding, unknown
cost, cancellation join before artifact reads, failed cleanup blocking handoff,
late-start fencing and exact-owned TERM/receipt behavior. Credential cases prove
sentinel absence from all exec argv/env/config and logs, consume-before-service,
stdin-only bootstrap, rejected input shapes and cleanup on failed/cancelled
uploads. They do **not** prove
a Docker install or real engine execution. Those remain the parent's one-task
container smoke gate before a paid paired trial.
