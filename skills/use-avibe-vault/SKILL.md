---
name: use-avibe-vault
slug: use-avibe-vault
description: Use Avibe Vault for API keys, tokens, passwords, protected credentials, authenticated HTTP requests, or digest signing without exposing secret values to the agent.
version: 0.5.0
---

# Use Avibe Vault

When a task needs API keys, access tokens, passwords, wallet private keys, or other sensitive credentials, prefer Avibe Vault: agents reference secrets by name, tag, or skill tag, and users do not need to paste plaintext into chat.

Core concepts:
- Static secret: a regular secret value, such as an API key, token, database password, or deployment credential. Use it with `vibe vault run` for environment injection or `vibe vault fetch` for authenticated HTTP egress.
- A static secret can contain up to 32 named fields. `NAME` selects its default field (existing secrets call it `value`); `NAME.field` selects that field, including an explicit default label such as `GITHUB_LOGIN.password`. Field names match `^[a-z][a-z0-9_]{0,63}$`. The default label may be renamed; extra labels and the choice of default cannot change after creation.
- Keypair secret: a signing key for digests or transactions, such as a wallet key or deployment signer. It cannot be exported as an environment variable and cannot be used with `run` / `fetch`; use `vibe vault sign`.
- Standard: for lower-risk routine automation. Agents can usually use it without interrupting the user unless it is configured to ask first.
- Protected: for high-risk secrets, such as production databases or wallet/funds keys. Because protected secrets are end-to-end encrypted, use requires browser approval and passkey unlock.

Rules:
- Refer to secrets only by secret name, record ref (`NAME.field`), tag, or skill tag.
- Static secrets can be used with `run` / `fetch`; keypair secrets can only be used with `vibe vault sign`. Keypairs do not accept field refs.
- Authorization is per secret: approving a protected secret unlocks **all its fields** for the grant's lifetime. The CLI cannot create, replace, or delete fields; the user does that in the browser.
- `--env NAME.field` delivers one value to `NAME_FIELD` (only the field is uppercased); `--env LOCAL=NAME.field` chooses the variable. `--tag` and `--skill` deliver every field of each selected secret, using `NAME` for the default and `NAME_FIELD` for extras. Conflicting variable mappings fail before delivery.
- `list` and `find` print the same value-free row: a static secret lists its `fields`, and a multi-field secret also names its `default_field`, the field `NAME` selects. If a ref fails with `field_not_found`, use the listed field names rather than guessing.
- `export` remains deprecated and has no delivery. Prefer `run`; advanced `inject` writes a restricted-permission file, so use it only for tools that require file-based configuration.
- With `vibe vault run`, the child process receives static secrets as environment variables, so never run commands that may print env vars, debug config, or secret-bearing errors.
- When protected `run` / `fetch` needs approval, Avibe automatically asks the user to decrypt and authorize access, and the command waits for that approval (540 seconds by default; `--approval-wait SECONDS` or `--no-approval-wait` to change it). If the command then succeeds, it ran once: do not run it again. If it returns `approval_required`, `approval_wait_timeout`, or `approval_handed_off`, or your call to it is stopped before it gets the approval, the command did not run; Avibe resumes this session once the request is decided, and after an approval you run the same `run` / `fetch` command once.
- When protected `sign` needs approval, Avibe creates a browser signing request and returns immediately. Do not rerun `sign`; when Avibe resumes this session, follow the callback instruction to read the completed request result and continue with the returned signature.

Common commands:

Request that the user add a missing static secret. `spec-json` may contain only non-secret prefill metadata; the actual secret value is entered by the user in the browser:
`vibe vault request OPENAI_API_KEY --reason "Need OpenAI API access" --spec-json '{"kind":"static","protection":"protected","description":"OpenAI API key","tags":["openai","prod","skill:model-work"],"policy":{"allowed_hosts":["api.openai.com"],"auth":{"type":"bearer"}}}'`

Request a multi-field login item with a password default. These are field names only, never values:
`vibe vault request GITHUB_LOGIN --reason "Log in to GitHub" --spec-json '{"protection":"protected","fields":["username","password","totp"],"default_field":"password"}'`

A `totp` field is ordinary stored secret material in this version, not a generated one-time code.

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
`vibe vault run --env GITHUB_LOGIN.username -- python script.py`
`vibe vault run --env PASSWORD=GITHUB_LOGIN.password -- python script.py`
`vibe vault run --tag deploy -- ./deploy.sh`
`vibe vault run --skill github-release -- ./release.sh`

Make an authenticated HTTP request. The credential is attached only at egress, and the agent never sees the secret:
`vibe vault fetch --auth GITHUB_API.token --url https://api.github.com/user`

The secret's allowed hosts and auth scheme apply to every field; this attaches one field, not a username/password pair.

For tools that require a file, `inject` uses the same selectors and explicit output keys:
`vibe vault inject --env USERNAME=GITHUB_LOGIN.username --env PASSWORD=GITHUB_LOGIN.password --out login.env --format dotenv`
`vibe vault inject --tag deploy --out deploy.json --format json`

Request approval before a protected `run` with an existing static secret:
`vibe vault access GITHUB_LOGIN.password --skill deploy --command "Log in to GitHub" --egress "github.com"`

For protected `fetch`, run `vibe vault fetch`; it creates the correct fetch approval request when needed.

Sign a 32-byte digest with a keypair secret. Standard keys may return the signature directly; protected keys create a browser approval request:
`vibe vault sign WALLET_KEY --digest 0000000000000000000000000000000000000000000000000000000000000001 --scheme ecdsa-secp256k1-recoverable --command "sign deployment transaction"`

Ask the user to delete a Vault secret. Every agent-initiated delete needs one-click user approval, including standard, protected, static, and keypair secrets:
`vibe vault rm OLD_TOKEN --reason "Rotated credential"`
This returns a pending request ID without deleting anything. With the normal callback, Avibe resumes this Session after the user decides; do not rerun `rm`. With `--no-callback`, or when the request has no resolvable Session, check explicitly with `vibe vault await <request_id>`; do not rerun `rm`. If a repeated `rm` reuses a pending request created by another Session, use the returned request ID with `vibe vault await <request_id>` rather than expecting a callback for the current Session.

`vibe vault rm` always requests whole-secret deletion. It rejects `NAME.field`; remove a single field from the Vaults page instead.

For more details, run `vibe vault --help`.
