# DeepSeek Search and Fetch tools

Status: **proposal**, 2026-10-19. Owner request: make DeepSeek's standalone
Search/Fetch APIs usable from any Vibey model without DeepSeek Harness or a
provider-native `web_search`.

## Why this is not a hosted tool

Model Hub's hosted `web_search` runs either natively on the model's provider or
through another vendor's model executor. Both paths need a model call, and the
executor path assumes the engine owns the credential and can send the vendor's
hosted declaration. DeepSeek's standalone API is a raw HTTP endpoint, not a
model call, so it does not fit that executor contract.

The tools therefore sit at the local tool layer, beside `read`, `write`, `edit`,
and `bash`. They work on every model Vibey serves, including a local model whose
provider exposes no search, and they cost one HTTP request instead of a model
call.

## Tools

| Tool | API | Arguments |
| --- | --- | --- |
| `deepseek_web_search` | `POST /v1/tools/search` | `query` (1-128), `count` (1-50, default 5), `need_content`, `ori_question` |
| `deepseek_fetch` | `POST /v1/tools/fetch` | `url` (HTTP/HTTPS, at most 8192) |

Search returns title, URL, snippet, summary, site, date, authority, and rerank
score. `need_content` adds page content; content is capped at 20,000 characters
per result and 100,000 characters in total. The rendered tool text is capped at
60,000 characters with an explicit `more result(s) omitted` marker. Fetch
returns title and Markdown content, capped at 20,000 characters. An empty Fetch
body is a failure, not a successful empty page.

## Credentials

The key is looked up in this order:

1. `DEEPSEEK_API_KEY` in the Turn environment, used for a direct HTTPS request.
2. Otherwise `vibe vault fetch --auth DEEPSEEK_API_KEY` attaches the Vault
   credential at egress. `DEEPSEEK_VAULT_SECRET` names a different secret.

The Vault path needs no key in any config or process argument. Direct mode
refuses HTTP redirects, so a cross-origin redirect cannot receive the bearer
credential. Error text is redacted for bearer values and `sk-` keys before it
reaches the model.

## Enablement

The tools are opt-in: set `AVIBE_DEEPSEEK_SEARCH=1` in the service environment.
Without it, Vibey's tool list and prompt are unchanged. A future config field
can expose the same switch in Settings; the environment variable keeps this PR
from adding a migration.

## Tests

`tests/agent_core/tools/test_deepseek.py` covers argument validation, result
shaping, content and output caps, empty Fetch, credential redaction, redirect
refusal, opt-in gating, and a lone surrogate. `tests/test_vibey_agent.py`
continues to pass with the tools off by default.

## Follow-ups

- A managed MCP/CLI surface for Claude Code, Codex, and OpenCode, if Avibe
  decides to own that integration rather than leave it to each client.
- A Model Hub direct-search executor once the engine can expose a Source's
  credential to a non-model HTTP call; today the engine owns that credential.
