# API Client

## Calling the Web UI API

Mutating API calls require:

- same-origin `Origin` or `Referer` header
- CSRF cookie named `vibe_csrf_token`
- matching `X-Vibe-CSRF-Token` header

Use this local curl pattern:

```bash
BASE="http://127.0.0.1:5123"
COOKIE_JAR="$(mktemp)"
CSRF="$(
  curl -fsS -c "$COOKIE_JAR" "$BASE/api/csrf-token" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])'
)"

curl -fsS -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
  -H "Origin: $BASE" \
  -H "X-Vibe-CSRF-Token: $CSRF" \
  -H "Content-Type: application/json" \
  -X POST "$BASE/doctor" \
  --data '{}'
```

For `DELETE`, use the same cookie jar, `Origin`, and CSRF header.

When the Web UI is served through Avibe Cloud, the same calls require an authenticated OIDC session cookie issued by `/auth/callback`. Prefer hitting `127.0.0.1:5123` directly from the local machine for maintenance work.

Do not log full request bodies when they contain tokens or secrets.

### Reusable local API helper

For multi-step maintenance, use the bundled helper at `scripts/vibe_api.py` instead of hand-writing curl commands. The helper handles CSRF, same-origin headers, cookies, JSON encoding, and readable error output.

Resolve paths relative to this skill directory. If the skill is installed at `skills/use-avibe`, run:

Usage examples:

```bash
export VIBE_UI_BASE="http://127.0.0.1:5123"

python3 skills/use-avibe/scripts/vibe_api.py GET /health
python3 skills/use-avibe/scripts/vibe_api.py GET '/settings?platform=slack'
python3 skills/use-avibe/scripts/vibe_api.py POST /doctor '{}'
python3 skills/use-avibe/scripts/vibe_api.py POST /config '{"show_duration":true}'
python3 skills/use-avibe/scripts/vibe_api.py DELETE '/api/users/U123?platform=slack'
```

Payload can be passed as inline JSON, as `@payload.json`, or as `-` to read JSON from stdin.

For scope updates, still fetch and merge first:

```bash
API_HELPER="skills/use-avibe/scripts/vibe_api.py"

python3 "$API_HELPER" GET '/settings?platform=slack' > /tmp/slack_settings.json
python3 - <<'PY'
import json
from pathlib import Path

settings = json.loads(Path("/tmp/slack_settings.json").read_text())
channels = settings.get("channels") or {}
channels["C123"] = {
    **channels.get("C123", {}),
    "enabled": True,
    "show_message_types": channels.get("C123", {}).get("show_message_types") or ["assistant"],
    "custom_cwd": channels.get("C123", {}).get("custom_cwd"),
    "require_mention": channels.get("C123", {}).get("require_mention"),
    "routing": {
        **(channels.get("C123", {}).get("routing") or {}),
        "agent_name": "codex",
        "model": "gpt-5.4",
        "reasoning_effort": "high",
        "codex_model": "gpt-5.4",
        "codex_reasoning_effort": "high",
    },
}
Path("/tmp/slack_payload.json").write_text(json.dumps({"platform": "slack", "channels": channels}))
PY
python3 "$API_HELPER" POST /settings @/tmp/slack_payload.json
python3 "$API_HELPER" GET '/settings?platform=slack'
```
