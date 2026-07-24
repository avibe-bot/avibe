# Model Hub — Subscription-Reuse ToS / Billing / Ban-Risk Review (Spike S2)

Status: spike report v1 · 2026-07-23 · research-only, no code
Blocks: *defaults* for subscription flows (impl plan §4 product gates), not the build
Spec: `docs/plans/model-hub.md` (§2, §4.2, §8, §9, §10.2)
Impl plan: `docs/plans/model-hub-implementation.md` (§1 S2, §4)
All URLs accessed **2026-07-23** unless noted. Confidence tags: **[VERIFIED]** = primary
vendor doc / primary artifact (official terms, the actual PR/issue); **[DOC-IMPLIED]** =
reasoned inference from official docs; **[ANECDOTAL]** = community report / secondary blog.

---

## 0. TL;DR (one line per scenario)

| Scenario | Claude (Anthropic) | ChatGPT (OpenAI) |
| --- | --- | --- |
| **(a)** same-vendor, same-user, own agent **via the Hub proxy** | **Do-not-ship default-on.** This is the exact pattern Anthropic blocks server-side and bans for. Compliant Claude-subscription path is **Direct mode only**. Experimental-flag + explicit ban-risk consent if offered at all. | **Consent-gated experimental.** Currently works, "not officially supported," ToS-ambiguous, no crackdown yet. |
| **(b)** cross-client (sub consumed by a non-native client, e.g. OpenCode) | **Do-not-ship.** Explicitly prohibited; server-blocked; OpenCode removed it under Anthropic legal demand. | **Experimental-flag only**, consent-gated, ban-risk warning. Gray/unsanctioned (OpenClaw pattern). |
| **(c)** cross-vendor substitution | **Keep default-off experimental** (spec §9 already). ToS/ban risk *inherited* from whichever subscription it draws on + capability-equivalence risk. | Same. |

**The single most important finding:** the product promise in spec §2 ("subscriptions
consumed first, auto-failover **through the hub**") is in direct conflict with Anthropic's
live ToS **even for the default same-vendor same-user case (a)**, because the spec §7
credential model has the *engine hold the subscription OAuth token and re-originate the
request*, which is precisely "routing requests through Pro/Max credentials" that Anthropic
prohibits and now enforces. API-key sources carry **no such gate** and are the safe backbone.

---

## 1. Scope definitions (why the proxy, not just the vendor, decides the verdict)

The question is not only "which vendor" but "**does a credential-holding proxy sit in the
path**." Three request shapes matter:

- **Direct (official binary, no proxy):** the official `claude` / `codex` CLI talks straight
  to the vendor with its own OAuth token. This is "ordinary use of the native app."
- **Transparent passthrough (hypothetical):** the official binary keeps its own OAuth and a
  local relay forwards bytes unchanged. Legally grayer than Direct; **not** what the current
  spec builds (see §4).
- **Credential-holding hub (what spec §7 builds):** the engine holds the subscription OAuth
  token; backends receive only a local gateway token; the engine re-originates upstream calls
  with the subscription credential. **This is the pattern vendors prohibit / enforce against.**

Scenario mapping used throughout:
- **(a)** Claude sub → Claude Code, or ChatGPT sub → Codex — the user's own native agent, on
  the user's own machine, *through the Hub*.
- **(b)** a subscription consumed by a **non-native** client (Claude sub → OpenCode; ChatGPT
  sub → OpenCode / any non-Codex client).
- **(c)** cross-vendor substitution ("Claude quota gone → serve GPT"), spec §9 default-off.

---

## 2. Anthropic (Claude Pro / Max) — **[VERIFIED, high confidence]**

### 2.1 The operative ToS language (primary, verbatim)

Anthropic's Claude Code "Legal and compliance" page states, under **Authentication and
credential use** [VERIFIED, [code.claude.com/docs/en/legal-and-compliance](https://code.claude.com/docs/en/legal-and-compliance)]:

> "**OAuth authentication** is intended exclusively for purchasers of Claude Free, Pro, Max,
> Team, and Enterprise subscription plans and is designed to support ordinary use of Claude
> Code and other native Anthropic applications."
>
> "**Developers** building products or services that interact with Claude's capabilities,
> including those using the Agent SDK, should use API key authentication … **Anthropic does
> not permit third-party developers to offer Claude.ai login or to route requests through
> Free, Pro, or Max plan credentials on behalf of their users.**"
>
> "Anthropic reserves the right to take measures to enforce these restrictions and may do so
> without prior notice."

The same page ties quota to individual use: "Advertised usage limits for Pro and Max plans
assume ordinary, individual usage of Claude Code and the Agent SDK." Governing terms are the
[Consumer Terms of Service](https://www.anthropic.com/legal/consumer-terms) (Free/Pro/Max) and
the [Usage Policy](https://www.anthropic.com/legal/aup). The Register reports the underlying
"no third-party harness" clause (ToS §3.7) has existed since ~Feb 2024; February 2026 only made
it explicit [VERIFIED-secondary, [theregister.com](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/)].

### 2.2 Server-side enforcement (the decisive operational fact)

On **2026-01-09** Anthropic deployed a server-side check that rejects subscription-OAuth
requests not originating from the official client, returning: *"This credential is only
authorized for use with Claude Code and cannot be used for other API requests."* The check
keys on Claude-Code client identity (e.g. the `claude-code-20250219` beta header / native
system prompt / telemetry). Tools that spoofed those headers (OpenCode, Cline, RooCode) broke
overnight [ANECDOTAL/secondary but consistent across sources, [mindstudio.ai](https://www.mindstudio.ai/blog/anthropic-openclaw-ban-oauth-authentication); corroborated by [ridakaddir.com](https://ridakaddir.com/blog/post/did-anthropic-kill-opencode-claude-subscription-ban)].

Anthropic engineer Thariq Shihipar (quoted by The Register): "Third-party harnesses using
Claude subscriptions … are prohibited by our Terms of Service," and they "generate unusual
traffic patterns without any of the usual telemetry that the Claude Code harness provides"
[VERIFIED-secondary, [theregister.com](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/)].

**Implication for the Hub proxy (case a-Claude).** Two failure modes, both bad: (1) if the hub
does *not* perfectly reproduce Claude Code's client fingerprint, the server check rejects it;
(2) if it *does* spoof the fingerprint to pass, that is squarely the prohibited harness-spoofing
behavior — and the telemetry/traffic gap can still flag the account. There is no configuration
of a credential-holding proxy that is both functional and clearly compliant for Claude
subscriptions. **The only clean Claude-subscription path is Direct mode** (official binary,
direct connection) [DOC-IMPLIED, high confidence].

### 2.3 Quota / billing semantics

Dual-layer per plan: a **5-hour rolling window** (starts on first message) **and** a **weekly
cap** (fixed reset). Approx Pro ~45 msgs / 5h, Max 5x ~225, Max 20x ~900 (figures drift). Usage
across claude.ai, Claude Code and Claude Desktop **shares the same limit**
[ANECDOTAL/secondary, [truefoundry.com](https://www.truefoundry.com/blog/claude-code-limits-explained), [usecarly.com](https://www.usecarly.com/blog/claude-code-usage-limits/)].
Billing behavior when proxied: requests that pass the fingerprint draw from the normal
subscription windows; requests that fail are **rejected** (not silently re-billed as API) per
the Jan-9 error string. Anthropic's sanctioned programmatic path is **API keys** (metered,
per-token, on a separate Console account) — a consumer suspension does not affect Console API
access [DOC-IMPLIED].

---

## 3. OpenAI (ChatGPT Plus / Pro) via Codex — **[gray zone, medium confidence]**

### 3.1 What OpenAI actually says

- Official: signing into Codex with a ChatGPT account charges usage to the subscription; the
  ChatGPT **Terms of Use / Privacy Policy** apply; `codex login` uses a browser OAuth callback
  (default `localhost:1455`) with a **device-code** flow for headless use; token cached at
  `~/.codex/auth.json` [VERIFIED, [learn.chatgpt.com/docs/auth](https://learn.chatgpt.com/docs/auth) (redirect from developers.openai.com/codex/auth); [help.openai.com](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)].
- The auth docs **recommend API keys "for programmatic Codex CLI workflows, such as CI/CD
  jobs"** and even acknowledge LLM proxies for **API-key** access
  (`requires_openai_auth = true` "useful when you access OpenAI models through an LLM proxy
  server") — but say **nothing** blessing routing the *subscription* credential through a
  third-party proxy [VERIFIED, [learn.chatgpt.com/docs/auth](https://learn.chatgpt.com/docs/auth)].
- OpenAI Terms of Use prohibit using the Services "automatically or programmatically" to
  extract data/output and "circumventing any rate limits" — the same latent clause that makes
  subscription-proxying ambiguous [VERIFIED, [openai.com/policies/row-terms-of-use](https://openai.com/policies/row-terms-of-use/)].
- Asked directly whether a forked/modified Codex CLI using "Sign in with ChatGPT" is allowed,
  an OpenAI maintainer confirmed the Apache-2.0 **code** is free to fork but **declined the
  subscription-ToS question** ("I'm an engineer, not a lawyer"); the thread is marked
  Unanswered and the community notes "many developers … are waiting for clear guidance"
  [VERIFIED, [github.com/openai/codex/discussions/8338](https://github.com/openai/codex/discussions/8338)].

### 3.2 Practical status

Third-party subscription proxying (the "OpenClaw" pattern: localhost proxy mimicking the Codex
CLI system prompt so the ChatGPT subscription pays) **currently works** and is "**not officially
supported**"; secondary write-ups explicitly note Anthropic already killed the Claude equivalent
and Google made a similar Gemini-CLI change — i.e. precedent that vendors *do* eventually clamp
down [ANECDOTAL/secondary, [explainx.ai](https://explainx.ai/blog/login-with-chatgpt-codex-subscription-oauth-2026), [lumadock.com](https://lumadock.com/tutorials/openclaw-openai-codex-chatgpt-subscription)].
No documented mass-suspension wave for OpenAI subscription-proxying was found (contrast with
Anthropic). Third-party Codex OAuth 429s are reported but read as quota, not bans
[ANECDOTAL, [github.com/openai/openai-python#2951](https://github.com/openai/openai-python/issues/2951)].

### 3.3 Quota / billing semantics

Since **2026-04-02** Codex meters against **API-token-equivalent credits**, not per-message; a
**5-hour rolling window shared by local + cloud** plus a **weekly cap**. Plus ≈15–110 msgs/5h
(model-dependent bands), Pro 5x/20x multiply; Plus/Pro can buy top-up credits; API-key route
skips both caps and bills per token [ANECDOTAL/secondary, [morphllm.com/codex-pricing](https://www.morphllm.com/codex-pricing), [help.openai.com codex-rate-card](https://help.openai.com/en/articles/20001106-codex-rate-card)].

**Net:** OpenAI is *softer than Anthropic today* — no explicit prohibition, no enforcement wave
— but the direction of travel (docs steer automation to API keys; vendor precedent) means this
is a **latent, not absent, risk**. Treat as consent-gated, not default-on-silent.

---

## 4. The architectural crux vs spec §7

Spec §7 defines three credential rings: backends receive **only the local gateway token**;
**upstream subscription OAuth tokens are engine-held** and injected upstream by the engine. That
design *is* "a service routing requests through Pro/Max credentials on behalf of the user" —
the prohibited pattern (§2.1) — regardless of it being the user's own machine and own
credential. A narrower "transparent passthrough where the official client keeps its own OAuth
and the hub adds nothing" would be legally grayer-but-arguable, yet it (i) contradicts §7, (ii)
still shows the abnormal-telemetry signature Anthropic cites, and (iii) forfeits the hub's
failover value for subscriptions anyway (you cannot fail a subscription request over to another
source without the hub re-originating it). **Conclusion: there is no design that delivers
hub-mediated failover for a Claude subscription while staying clearly ToS-compliant.** This is a
product decision to escalate, not an implementation detail.

API-key sources are unaffected: both vendors explicitly bless API keys for programmatic/proxy
use, so **routing API-key sources through the Hub is fully compliant and should be the
default-on backbone** of Model Hub.

---

## 5. Community / practical ban-risk signal

- **[VERIFIED — primary artifacts]** OpenCode PR #18186 "anthropic legal requests" removed the
  branded system prompt, the `opencode-anthropic-auth` plugin, the `claude-code-20250219` beta
  header, and the Anthropic login hint — community reaction 4👍/40👎
  [[github.com/anomalyco/opencode/pull/18186](https://github.com/anomalyco/opencode/pull/18186)].
  This is hard evidence for the **(b)-Claude do-not-ship** verdict.
- **[VERIFIED — primary artifact]** CLIProxyAPI issue #2211: a user running Claude Code creds
  through the proxy with an `opencode` client reported *"Your account has been disabled after an
  automatic review of your recent activities,"* pointing at ToS + Usage Policy and a Trust &
  Safety appeal path [[github.com/router-for-me/CLIProxyAPI#2211](https://github.com/router-for-me/CLIProxyAPI/issues/2211); further reports in [discussion #2244](https://github.com/router-for-me/CLIProxyAPI/discussions/2244)].
- **[ANECDOTAL]** Reported ban *triggers*: plan upgrades, payment-method changes, and unusual
  usage spikes triggering automated review (one case: OAuth + Max 5x→20x upgrade → disabled in
  ~20 min). "Usage limit reached" ≠ suspension; distinguish throttle from ban
  [[autonomee.ai](https://autonomee.ai/blog/claude-code-account-suspended-banned-safe-usage/), [knightli.com](https://knightli.com/en/2026/05/09/claude-account-suspension-code-limit-guide/)].
- **[ANECDOTAL]** No comparable OpenAI subscription-proxying suspension wave was found; signal
  there is quota-429s, not disablement.

**Summary:** ban risk for Claude subscription proxying/cross-client is **verified and
demonstrated** (account disables + legal takedowns). For OpenAI it is **latent/anecdotal**
(works today, unsanctioned, no enforcement wave observed).

---

## 6. cn-region vendors (LOW priority, one paragraph) — **[ANECDOTAL/secondary]**

Zhipu (Z.AI) **GLM Coding Plan** and Moonshot **Kimi Coding Plan** sell flat-rate coding
subscriptions (~US$18–30/mo entry) *distinct* from pay-as-you-go API keys, and both are moving
to **first-party OAuth device-flow** onboarding. Critically, Zhipu's Coding-Plan keys are
documented as **usable only inside supported coding tools and cannot make standalone API
calls** — a tool-lock restriction that means routing a cn coding-plan credential through the
Avibe Hub could violate *their* terms much like Anthropic's, and would need the same
consent-gate rather than default-on. Treat cn subscription reuse as **experimental/consent-gated
pending per-vendor confirmation**; cn **API keys** (pay-as-you-go) are unrestricted and fine for
the Hub [ [jia.je coding_plan](https://jia.je/kb/en/software/coding_plan.html), [gsd-2#4642 (Kimi/GLM OAuth device-flow)](https://github.com/gsd-build/gsd-2/issues/4642), [aicoolies Kimi plan](https://aicoolies.com/tools/kimi-coding-plan) ].

---

## 7. Risk matrix

Recommendation legend: **DEFAULT-ON** / **CONSENT-GATED** (ship, but explicit one-time
informed opt-in per source) / **EXPERIMENTAL-FLAG** (advanced, default-off, visible marking) /
**DO-NOT-SHIP**.

| # | Scenario / source | ToS posture | Billing / quota behavior | Ban risk | Recommendation |
| --- | --- | --- | --- | --- | --- |
| a-Claude | Claude sub → Claude Code **via Hub** | **Prohibited pattern** (engine routes sub creds); server-blocked since 2026-01-09 [VERIFIED] | Draws sub 5h+weekly windows *iff* fingerprint passes; else rejected | **High, demonstrated** (disables) | **DO-NOT-SHIP default-on.** Claude sub = **Direct mode only**. If ever in Hub → EXPERIMENTAL-FLAG + ban-risk consent |
| a-Claude(Direct) | Claude sub → Claude Code, **no proxy** | "Ordinary use of the native app" — allowed [VERIFIED] | Normal sub windows | Low | **DEFAULT-ON** (this is the compliant subscription story) |
| a-OpenAI | ChatGPT sub → Codex **via Hub** | Unaddressed / ambiguous; automation steered to API keys [VERIFIED-doc] | Sub 5h+weekly (credit-metered); works today | **Latent/anecdotal** | **CONSENT-GATED**, experimental-leaning; Direct/official `codex login` is the safe path |
| b-Claude | Claude sub → OpenCode / non-native | **Explicitly prohibited**; OpenCode removed under legal demand [VERIFIED] | Server-blocked | **High, demonstrated** | **DO-NOT-SHIP** |
| b-OpenAI | ChatGPT sub → OpenCode / non-Codex | Ambiguous, unsanctioned ("OpenClaw pattern") [ANECDOTAL] | Sub windows; works today | Latent, higher than a-OpenAI | **EXPERIMENTAL-FLAG**, consent-gated, warning |
| c | Cross-vendor substitution | No *new* per-vendor issue; inherits the drawn source's posture; + capability-equivalence risk (spec §4.2 warns thinking/cache/tool semantics differ) | Inherited | Inherited from source | **EXPERIMENTAL-FLAG default-off** (spec §9 already) |
| ref | **API-key sources** (any vendor) → Hub | **Sanctioned** programmatic path (both vendors) [VERIFIED] | Metered per-token, standard API limits | None (ToS) | **DEFAULT-ON** — the safe backbone |

---

## 8. Recommended product posture (actionable)

1. **Ship API-key sources through the Hub default-on.** This is compliant for every vendor and
   already gate-free per impl plan §4. Make it the primary, fully-featured path.
2. **Claude subscription = Direct mode only, and say so in the UI.** Do not offer Claude-sub
   inside Hub-mediated failover by default. This means spec §2's "subscriptions consumed first +
   auto-failover through the hub" **cannot be honored for Claude subscriptions** — escalate this
   product tension to the owner. Option: for a Claude sub, run the official binary Direct and let
   *API-key* sources be the failover tier the Hub arbitrates.
3. **ChatGPT subscription in Hub = consent-gated experimental**, off by default, with a one-time
   informed opt-in and a visible "unofficial / may stop working / small ban risk" marker.
4. **Cross-client (b) and cross-vendor (c) subscription reuse = experimental-flag, default-off**,
   visible per-event marking (spec §9 already covers c; extend the same treatment to b).
5. **Use spec §4.2's `allowed_origins` reservation** to hard-restrict which local clients a
   subscription credential may serve, so a Claude sub credential can *never* be silently used to
   serve OpenCode etc. — enforce the (a)/(b) boundary in code, not just copy.
6. **Never present subscription-via-Hub as "free extra quota."** Frame honestly: it reuses quota
   the user already pays for, under the vendor's individual-use terms, at the user's own risk.

---

## 9. Consent copy points (for anything consent-gated: ChatGPT-sub-in-Hub, cross-client,
cross-vendor, cn-sub)

**中文 (zh):**
- 该来源使用你**本人订阅**的登录凭据；厂商条款通常要求订阅仅供**个人在官方客户端**内使用,经由中枢代理转发属于**非官方用法**,可能违反其服务条款。
- 存在**账号被风控、限流或封禁**的真实风险(已有 Claude 订阅经代理使用被自动封号的先例);是否启用由你自行决定并承担后果。
- 追求最稳妥时,请对订阅使用**直连模式**(官方客户端直接连接),或改用 **API Key** 来源(厂商明确允许程序化/代理调用)。

**English (en):**
- This source uses **your own subscription** login. Vendor terms generally intend a subscription
  for **personal use inside the official client**; routing it through the hub proxy is an
  **unofficial use** that may violate those terms.
- There is a **real risk of rate-limiting, review, or account suspension** (Claude subscriptions
  used via proxies have been auto-disabled). Enabling this is your choice and at your own risk.
- For the safest setup, use **Direct mode** for subscriptions (official client, direct
  connection), or add an **API Key** source instead — vendors explicitly permit programmatic /
  proxied use of API keys.

---

## 10. Citations (accessed 2026-07-23)

**Primary — Anthropic / OpenAI official**
- Claude Code Legal & compliance (verbatim OAuth policy): https://code.claude.com/docs/en/legal-and-compliance
- Anthropic Consumer Terms of Service: https://www.anthropic.com/legal/consumer-terms
- Anthropic Usage Policy (AUP): https://www.anthropic.com/legal/aup
- Claude Code Authentication docs: https://code.claude.com/docs/en/authentication
- OpenAI Terms of Use (RoW): https://openai.com/policies/row-terms-of-use/
- Codex Authentication docs: https://learn.chatgpt.com/docs/auth (redirect from https://developers.openai.com/codex/auth)
- OpenAI Help — Using Codex with your ChatGPT plan: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- OpenAI Help — Codex rate card: https://help.openai.com/en/articles/20001106-codex-rate-card

**Primary — artifacts (enforcement / ban)**
- OpenCode PR #18186 "anthropic legal requests": https://github.com/anomalyco/opencode/pull/18186
- CLIProxyAPI issue #2211 (Claude subscription banned): https://github.com/router-for-me/CLIProxyAPI/issues/2211
- CLIProxyAPI discussion #2244: https://github.com/router-for-me/CLIProxyAPI/discussions/2244
- openai/codex discussion #8338 (ToS for forked/modified Codex CLI): https://github.com/openai/codex/discussions/8338
- openai/openai-python issue #2951 (third-party Codex 429): https://github.com/openai/openai-python/issues/2951

**Secondary — reporting / analysis**
- The Register — Anthropic clarifies ban on third-party tool access (2026-02-20): https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/
- MindStudio — OpenClaw ban / OAuth crackdown (Jan-9 server check + error string): https://www.mindstudio.ai/blog/anthropic-openclaw-ban-oauth-authentication
- Rida Kaddir — Did Anthropic kill OpenCode: https://ridakaddir.com/blog/post/did-anthropic-kill-opencode-claude-subscription-ban
- autonomee.ai — Claude Code account suspended, safe usage: https://autonomee.ai/blog/claude-code-account-suspended-banned-safe-usage/
- knightli.com — Claude account suspension guide: https://knightli.com/en/2026/05/09/claude-account-suspension-code-limit-guide/
- explainx.ai — Login with ChatGPT / Codex subscription OAuth (2026): https://explainx.ai/blog/login-with-chatgpt-codex-subscription-oauth-2026
- lumadock.com — Codex on OpenClaw with ChatGPT subscription: https://lumadock.com/tutorials/openclaw-openai-codex-chatgpt-subscription

**Quota semantics**
- truefoundry — Claude Code limits explained: https://www.truefoundry.com/blog/claude-code-limits-explained
- usecarly — Claude Code usage limits: https://www.usecarly.com/blog/claude-code-usage-limits/
- morphllm — Codex pricing & usage limits: https://www.morphllm.com/codex-pricing

**cn-region**
- Jiegec KB — AI Coding Plan (GLM / Kimi tiers, tool-lock): https://jia.je/kb/en/software/coding_plan.html
- gsd-2 issue #4642 — Kimi/GLM OAuth device-flow onboarding: https://github.com/gsd-build/gsd-2/issues/4642
- aicoolies — Kimi Coding Plan: https://aicoolies.com/tools/kimi-coding-plan

---

## 11. Confidence & caveats

- **High confidence:** Anthropic's prohibition of subscription-credential routing by
  third-party services, its live server-side enforcement, and the demonstrated bans (primary
  docs + primary artifacts).
- **Medium confidence:** OpenAI's posture. It is *currently* permissive-by-silence, not
  explicitly sanctioned; the ToS "automated/programmatic" clause + docs steering automation to
  API keys + cross-vendor precedent make it a latent risk. No OpenAI subscription-proxying
  suspension wave was found, but absence of evidence ≠ safety.
- **Low confidence / one-paragraph:** cn-region specifics (secondary sources; verify per-vendor
  terms at implementation time).
- **Time-sensitivity:** vendor terms, quota numbers, and enforcement posture changed multiple
  times across 2025–2026 (e.g. Anthropic Feb-2026 doc revision, OpenAI Apr-2026 pricing shift).
  Re-verify §2.1/§3.1 language and the §7 architectural conclusion **immediately before**
  enabling any subscription flow default-on. This memo gates *defaults*, per impl plan §4; the
  build may proceed on API-key paths in parallel.
