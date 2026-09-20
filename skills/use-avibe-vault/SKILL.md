---
name: use-avibe-vault
slug: use-avibe-vault
description: Use Avibe Vault for API keys, tokens, passwords, protected credentials, authenticated HTTP requests, or digest signing without exposing secret values to the agent.
version: 0.2.0
---

# Use Avibe Vault

When a task needs API keys, access tokens, passwords, wallet private keys, or other sensitive credentials, prefer Avibe Vault: agents reference secrets by name, tag, or skill tag, and users do not need to paste plaintext into chat.

Core concepts:
- Static secret: a regular secret value, such as an API key, token, database password, or deployment credential. Use it with `vibe vault run` for environment injection or `vibe vault fetch` for authenticated HTTP egress.
- Keypair secret: a signing key for digests or transactions, such as a wallet key or deployment signer. It cannot be exported as an environment variable and cannot be used with `run` / `fetch`; use `vibe vault sign`.
- Standard: for lower-risk routine automation. Agents can usually use it without interrupting the user unless it is configured to ask first.
- Protected: for high-risk secrets, such as production databases or wallet/funds keys. Because protected secrets are end-to-end encrypted, use requires browser approval and passkey unlock.

Rules:
- Refer to secrets only by secret name, tag, or skill tag.
- Static secrets can be used with `run` / `fetch`; keypair secrets can only be used with `vibe vault sign`.
- With `vibe vault run`, the child process receives static secrets as environment variables, so never run commands that may print env vars, debug config, or secret-bearing errors.
- When protected `run` / `fetch` needs approval, Avibe automatically asks the user to decrypt and authorize access. After the user approves, Avibe resumes this session; it does not replay the command for you, so run the same `run` / `fetch` command again.
- When protected `sign` needs approval, Avibe creates a browser signing request and returns immediately. Do not rerun `sign`; when Avibe resumes this session, follow the callback instruction to read the completed request result and continue with the returned signature.

Common commands:

Request that the user add a missing static secret. `spec-json` may contain only non-secret prefill metadata; the actual secret value is entered by the user in the browser:
`vibe vault request OPENAI_API_KEY --reason "Need OpenAI API access" --spec-json '{"kind":"static","protection":"protected","description":"OpenAI API key","tags":["openai","prod","skill:model-work"],"policy":{"allowed_hosts":["api.openai.com"],"auth":{"type":"bearer"}}}'`

For a missing keypair/signing key, ask the user to create a keypair secret in the Vault UI; do not request or store private-key material as a static secret.

On Avibe Web chat, a lighter manual prompt can mention the missing secret as a clickable placeholder in your reply, for example `$<OPENAI_API_KEY>`. The user can click it and fill the secret from Web chat. This has no reason or structured prefill metadata; use `vibe vault request` when those are needed.

List or find existing Vault entries:
`vibe vault list`
`vibe vault list --tag prod`
`vibe vault find --kind static --protection protected`
`vibe vault find openai --tag prod`
`vibe vault tags`

Run a command with selected static secrets injected as environment variables:
`vibe vault run --env OPENAI_API_KEY,GITHUB_TOKEN -- python script.py`
`vibe vault run --env GITHUB_TOKEN=GH_PAT --env OPENAI_API_KEY -- python script.py`
`vibe vault run --tag deploy -- ./deploy.sh`
`vibe vault run --skill github-release -- ./release.sh`

Make an authenticated HTTP request. The credential is attached only at egress, and the agent never sees the secret:
`vibe vault fetch --auth GITHUB_PAT --url https://api.github.com/user`

Request approval before a protected `run` with an existing static secret:
`vibe vault access PROD_DB_URL --skill deploy --command "run database migration" --egress "connect to production database"`

For protected `fetch`, run `vibe vault fetch`; it creates the correct fetch approval request when needed.

Sign a 32-byte digest with a keypair secret. Standard keys may return the signature directly; protected keys create a browser approval request:
`vibe vault sign WALLET_KEY --digest <64-hex-digest> --scheme ecdsa-secp256k1-recoverable --command "sign deployment transaction"`

For more details, run `vibe vault --help`.
