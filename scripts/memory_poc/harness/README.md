# EverOS Memory POC Harness

This project is intentionally separate from the Avibe application environment.
It installs the pinned provider into the POC-owned runtime environment only.

Bootstrap the exact locked environment from the worktree root:

```sh
UV_PROJECT_ENVIRONMENT="$PWD/.runtime/memory-poc/env" \
UV_CACHE_DIR="$PWD/.runtime/memory-poc/uv-cache" \
uv sync --locked --project scripts/memory_poc/harness --python 3.12
```

Run the frozen contract CLI with that environment's Python:

```sh
.runtime/memory-poc/env/bin/python -m memory_poc run --stage sanity --run-id stage1-sanity
.runtime/memory-poc/env/bin/python -m memory_poc report --run-id stage1-sanity
```

The harness discovers `.runtime/memory-poc/.env.poc` in this worktree first,
then the primary checkout path frozen in `../CONTRACT.md`. It never copies that
file, prints its values, or writes its values to reports or logs.

Stage 1 readiness uses only the production-style public hybrid `/search` call
with profiles included. Episode and atomic-fact retrieval are the scored gate;
the initial run continues profile observation for up to 600 seconds after a
successful flush. A missing profile is recorded as a known-absent warning in
the redacted `summary.md`, not a retrieval failure. The summary records each
first observed time and the maximum observed cascade lag without fixture
content.
