# Contributing

Thanks for your interest in contributing!

## Getting Started

- Fork the repo and create a feature branch
- Install the CLI with `uv tool install vibe`
- Run `vibe` to complete the setup UI

## Development

- Run locally: `python main.py`
- Build an installable local wheel: build `ui/`, run
  `python scripts/prepare_local_show_runtime_manifest.py`, then run
  `uv build --wheel`. The preparation step inherits the latest official
  release's SHA256-verified Show Runtime manifest; packaging fails closed when
  the manifest is missing or invalid.
- Lint before PR: ruff is configured with a minimal safety rule set (E9,F63,F7,F82) and ignores E501. Install hooks with `pip install pre-commit` then `pre-commit install`. Run manually with `pre-commit run --all-files`.
- Write clear commit messages
- Agent-specific changes: install the relevant CLI (recommended: OpenCode `opencode`; also supported: Codex), then route one Slack channel via Slack **Agent Settings** to manually test the backend (`opencode` or `codex`).

## Pull Requests

- One logical change per PR
- Include description, screenshots/logs if UX/behavior changes
- Update docs/README if config or behavior changes

## Code of Conduct

By participating, you agree to the CODE_OF_CONDUCT.md
