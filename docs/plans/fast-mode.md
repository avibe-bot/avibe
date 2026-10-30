# Fast mode

Status: owner-approved design, 2026-10-30. The menu uses option B: a Fast
toggle beside the effort heading, with a hover explanation. The trigger uses
mark ②: a slim light-amber bolt beside the chevron.

## Goal

OpenAI and Anthropic each sell a faster processing tier for some models: the
same model, more output tokens per second, a higher price. A user can turn it
on for a Session from the Web model picker and turn it off at any time. The
closed picker shows a small mark while the Session runs fast. A new Web Session
starts with the choice the user made last.

- OpenAI calls it Fast mode, formerly priority processing. A Responses or Chat
  Completions request sets `service_tier: "priority"`. ChatGPT plans offer it to
  Codex as the "Fast" service tier, which uses more of the plan's quota.
- Anthropic calls it fast mode: a Messages request sets `speed: "fast"` on
  Claude Opus 5.5, and on 5 and 4.8 with a beta header. Claude subscriptions pay
  for it from usage credits only.

## Contract

### Intent

- A Session stores its processing tier in its row's `metadata.speed`: `"fast"`,
  or absent for the standard tier. The Session payload exposes it as top-level
  `speed` (`"fast"` or `null`). `POST /api/sessions` accepts `speed`, and so does
  `PATCH /api/sessions/<id>`, including on the fixed main Session. A fork copies
  the source Session's metadata, so it inherits the tier with the model and effort.
- The remembered default is a global Workbench preference,
  `state_meta["workbench.fast_mode_default"]`, read and written through
  `/api/workbench/prefs` as `fast_mode_default`. The Web picker writes it on
  every toggle, and the Web new-session composer starts from it. Sessions
  created by IM, Tasks, Watches and `vibe agent run` start at the standard tier:
  those surfaces show no mark, so they never pay more unseen.
- Dispatch copies the Session row onto the turn's context. `resolve_model_hub_launch`
  reads the tier from there and passes it to the Hub router, which records it on
  the gateway turn the launch prepares (`TurnTrace.speed`). The turn keeps the
  first value it is given, like its hosted tools, so a change applies from the
  next turn.

### Where the tier applies

The gateway reads the turn's tier for each request it can attribute to a turn
and carries it on `ModelHubRequest.speed`. Each attempt adds the vendor's field
only where its exact `(Source, model)` offers the tier
(`core/handlers/model_hub/fast_mode.py`):

- the model's first-party vendor, by `vibe/data/model_vendors.json` family
  prefix, is OpenAI or Anthropic, and the Source is that vendor's own upstream:
  an API key on its official endpoint, or its subscription held by the Model Hub
  (not a `native_cli` Source, whose requests never reach the gateway);
- the request speaks the Source's protocol, so the tier is never reached through
  a translation;
- the vendor's models.dev entry for that model names the field under
  `experimental.modes.fast.provider.body`. The entry's body must be exactly the
  vendor's one field (`service_tier` for OpenAI, `speed` for Anthropic); any
  other body is not applied. The engine adds the beta header the Anthropic
  model needs. The catalog is the cached copy, read off the controller loop and
  rebuilt only when that copy changes. Reading it never fetches.

A fallback to a hop without the tier runs at the standard tier. OpenCode's
shared server names no turn on its requests, so its turns never carry a
Session's tier. Direct-mode Agents never reach the gateway. For both, the
picker reports fast mode as not offered.

### Refusal

A vendor refuses the fast tier without a code of its own. For example,
Anthropic answers an account without usage credits with 429
`rate_limit_error`. So a fast attempt that fails with 400, 403 or 429 before any
output is retried once on the same hop at the standard tier. Only that retry's
outcome is classified, so a refused tier never cools down a healthy Source. A
real rate limit costs one extra immediate request before it is classified as
usual.

### Picker

- The Hub Agent read states, on each catalog model, whether a turn on it would
  reach the fast tier on the hop that normally serves it (`fast_mode`). The model
  menu remembers these per backend, beside the model labels
  (`avibe.fast-models.v1`).
- The compact picker's Fast toggle sits beside the effort heading. Its hover and
  focus tooltip says what it does. While fast mode is on, a touch screen shows
  the same line under the efforts. On a model without the tier, the toggle keeps
  the Session's choice but is disabled, and the trigger shows no mark.
- The trigger's mark, a slim bolt in light amber, sits beside the chevron,
  outside the parts a narrow row drops. It shows only while the Session asks for
  the fast tier and its model would get it.

## Not in this change

- Per-turn cost estimates. The tooltip says the price is higher; the
  multiplier differs by vendor and plan.
- Fast mode for Direct-mode Agents, OpenCode, and the IM settings surfaces.
- Recording the served tier in usage records. The response's `service_tier` or
  `usage.speed` says which tier ran.

## Validation

- Scenario tests: `MH-FAST-001` (a fast Session's turn reaches the hop that
  offers the tier, and only that hop), `MH-FAST-002` (a refused tier is served
  at the standard tier, and its Source stays routable), and `MH-FAST-003` (the
  menu marks the models that would run fast).
- Units: the hop rule (`tests/test_model_hub_fast_mode.py`), the Session field
  and its dispatch read, the preference, the picker, and the new-session flow.
- Browser: the mark stays visible on a crowded 320px phone row.
