---
name: use-avibe-vault
slug: use-avibe-vault
description: Use Avibe Vault for API keys, tokens, passwords, protected credentials, authenticated HTTP requests, or digest signing without exposing secret values to the agent.
version: 0.1.0
---

# Use Avibe Vault

Use this skill whenever a task needs credentials, authenticated egress, or a
signing key. Refer to secrets only by name, tag, or skill tag; users should not
paste secret values into chat.

## Secret Types

- A static secret is an API key, token, or password. Use it with
  `vibe vault run` for environment injection or `vibe vault fetch` for
  authenticated HTTP egress.
- A keypair secret signs digests or transactions. It cannot be exported as an
  environment variable and cannot be used with `run` or `fetch`; use
  `vibe vault sign`.
- Standard secrets support lower-risk routine automation.
- Protected secrets require browser approval and passkey unlock.

## Rules

- Never print, log, or ask the user to paste a secret value.
- Never use a keypair secret with `run` or `fetch`.
- With `vibe vault run`, avoid commands that print environment variables,
  debug configuration, or secret-bearing errors.
- Protected `run` and `fetch` may pause for approval. After approval resumes
  the Session, run the same command again because Avibe does not replay it.
- Protected `sign` returns immediately with a browser signing request. Do not
  rerun it; follow the callback instruction to read the completed result.

## Find Existing Secrets

```bash
vibe vault list
vibe vault list --tag prod
vibe vault find --kind static --protection protected
vibe vault find openai --tag prod
vibe vault tags
```

## Request A Missing Secret

Request a static secret with non-secret metadata only:

```bash
vibe vault request OPENAI_API_KEY \
  --reason "Need OpenAI API access" \
  --spec-json '{"kind":"static","protection":"protected","description":"OpenAI API key","tags":["openai","prod","skill:model-work"],"policy":{"allowed_hosts":["api.openai.com"],"auth":{"type":"bearer"}}}'
```

For a missing keypair, ask the user to create a keypair secret in the Vault UI.
Never request private-key material as a static secret.

On Avibe Web chat, a lighter manual request may mention a clickable placeholder
such as `$<OPENAI_API_KEY>`. Use `vibe vault request` when policy, reason, tags,
or other structured metadata matter.

## Run With Static Secrets

```bash
vibe vault run --env OPENAI_API_KEY,GITHUB_TOKEN -- python script.py
vibe vault run --env GITHUB_TOKEN=GH_PAT --env OPENAI_API_KEY -- python script.py
vibe vault run --tag deploy -- ./deploy.sh
vibe vault run --skill github-release -- ./release.sh
```

Use `vibe vault access` when an existing protected secret should be approved
before a command:

```bash
vibe vault access PROD_DB_URL \
  --skill deploy \
  --command "run database migration" \
  --egress "connect to production database"
```

## Authenticated HTTP Egress

The credential is attached only at egress and is not returned to the agent:

```bash
vibe vault fetch --auth GITHUB_PAT --url https://api.github.com/user
```

Run `vibe vault fetch` directly for protected fetches; it creates the required
approval request when needed.

## Sign A Digest

```bash
vibe vault sign WALLET_KEY \
  --digest <64-hex-digest> \
  --scheme ecdsa-secp256k1-recoverable \
  --command "sign deployment transaction"
```

Standard keys may return a signature directly. Protected keys create a browser
approval request. Run `vibe vault --help` when command behavior is uncertain.
