# Security Policy

## Reporting a Vulnerability
- Please open a private issue or email the maintainer (add contact) with details.
- Do not disclose publicly until we confirm a fix or mitigation.

## Secrets
- For personal remote access, see `docs/plans/personal-installation-claims-runtime.md`: a grant is not consent at this machine. The Runtime alone holds the claim verifier, and only the existing protected pairing journal/configuration may persist redeemed credentials.
- Never commit secrets. Store them in `~/.avibe/config/config.json` or a secret manager. Existing installs may still expose the legacy `~/.vibe_remote/` symlink for compatibility.
- Tokens required: Slack tokens, and the Claude/Anthropic credentials used by `claude-code-sdk` (e.g., `ANTHROPIC_API_KEY`).
