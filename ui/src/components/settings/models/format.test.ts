import { describe, expect, it } from 'vitest';

import { cooldownEtaMinutes, currencySymbol, formatNameList, formatSpend, friendlyModelName } from './format';
import type { AgentSupply, Source, SuppliedModel } from './types';

describe('currencySymbol', () => {
  it('falls back to USD when the backend reports no currency', () => {
    expect(currencySymbol(null)).toBe('$');
    expect(currencySymbol(undefined)).toBe('$');
    expect(currencySymbol()).toBe('$');
  });

  it('honors an explicitly reported currency', () => {
    expect(currencySymbol('USD')).toBe('$');
    expect(currencySymbol('CNY')).toBe('¥');
    expect(currencySymbol('EUR')).toBe('€');
  });

  it('returns no symbol for a code it cannot map', () => {
    // Callers use '' to pick a currency-free label instead of printing a wrong
    // symbol — the amount cell carries the authoritative value.
    expect(currencySymbol('JPY')).toBe('');
  });

  // The billing chip renders currencySymbol(...) while the usage cell renders
  // formatSpend(...). They must never disagree: a static '$' on the chip read
  // "按量 $" beside "¥12.4" for a CNY source (Codex P2, 2026-07-25).
  it('agrees with the symbol formatSpend uses, for every currency', () => {
    for (const c of [null, undefined, 'USD', 'CNY', 'EUR', 'JPY'] as const) {
      expect(formatSpend(1240, c).startsWith(currencySymbol(c))).toBe(true);
      const stripped = formatSpend(1240, c).slice(currencySymbol(c).length);
      expect(stripped).toBe('12.4');
    }
  });
});

describe('formatSpend', () => {
  // Owner ruling 2026-07-25: upstream vendors bill in USD, so a missing currency
  // must render as USD and the UI must never fall back to a local currency.
  // This is the tripwire for that decision — if someone restores a CNY (or any
  // other) fallback, this fails instead of shipping silently.
  it('falls back to USD when the backend reports no currency', () => {
    expect(formatSpend(1240, null)).toBe('$12.4');
    expect(formatSpend(1240, undefined)).toBe('$12.4');
    expect(formatSpend(1240)).toBe('$12.4');
  });

  it('honors an explicitly reported currency', () => {
    // The ISO 4217 map is deliberately retained: a source that genuinely bills in
    // CNY/EUR still renders in its own currency.
    expect(formatSpend(1240, 'USD')).toBe('$12.4');
    expect(formatSpend(1240, 'CNY')).toBe('¥12.4');
    expect(formatSpend(1240, 'EUR')).toBe('€12.4');
  });

  it('omits the symbol for a currency it cannot map, keeping the amount readable', () => {
    expect(formatSpend(1240, 'JPY')).toBe('12.4');
  });

  it('converts cents to one decimal without applying any FX rate', () => {
    // 1240 cents stays 12.40 whatever the symbol — amounts are never converted.
    expect(formatSpend(0, null)).toBe('$0.0');
    expect(formatSpend(5, null)).toBe('$0.1');
    expect(formatSpend(1234567, null)).toBe('$12345.7');
  });
});

describe('formatNameList', () => {
  // The regression: `names.join('、')` shipped Chinese punctuation into the English
  // UI, where the attribution line read "No supply for agent-a、agent-b".
  it('separates with the punctuation of the reader, not of the author', () => {
    expect(formatNameList(['agent-a', 'agent-b'], 'zh')).toBe('agent-a、agent-b');
    expect(formatNameList(['agent-a', 'agent-b'], 'en')).toBe('agent-a, agent-b');
  });

  it('works from the regional tags a browser actually reports', () => {
    expect(formatNameList(['a', 'b'], 'zh-CN')).toBe('a、b');
    expect(formatNameList(['a', 'b'], 'en-US')).toBe('a, b');
  });

  // `narrow` + `conjunction` is load-bearing, not a taste: `unit` joins Chinese
  // with NOTHING ("ab"), and the wider styles add a 和 / "and" that the 11px line
  // has no room for. Three names is where those differ, so three names is the test.
  it('stays a plain separated list at three names, in both locales', () => {
    expect(formatNameList(['a', 'b', 'c'], 'zh')).toBe('a、b、c');
    expect(formatNameList(['a', 'b', 'c'], 'en')).toBe('a, b, c');
  });

  it('adds nothing around a single name', () => {
    expect(formatNameList(['only'], 'zh')).toBe('only');
    expect(formatNameList(['only'], 'en')).toBe('only');
  });
});

describe('cooldownEtaMinutes', () => {
  it('is 0 for a missing or already-elapsed retry_at', () => {
    expect(cooldownEtaMinutes(null)).toBe(0);
    expect(cooldownEtaMinutes(undefined)).toBe(0);
    expect(cooldownEtaMinutes(new Date(Date.now() - 60_000).toISOString())).toBe(0);
  });

  it('rounds the remaining wait to whole minutes', () => {
    expect(cooldownEtaMinutes(new Date(Date.now() + 5 * 60_000 + 1_000).toISOString())).toBe(5);
  });
});

describe('friendlyModelName', () => {
  const model = (id: string, display_name: string | null = null): SuppliedModel => ({
    id,
    display_name,
    provenance: 'discovered',
  });

  const src = (id: string, models: SuppliedModel[], vendor = 'custom'): Source => ({
    id,
    kind: 'api_key',
    vendor,
    display_name: id,
    protocol: 'openai_compatible',
    supply_channel: 'hub',
    billing: 'metered',
    state: { status: 'active' },
    models,
  });

  const agent = (over: Partial<AgentSupply>): AgentSupply => ({
    backend: 'claude',
    mode: 'hub',
    menu_kind: 'fixed',
    ...over,
  });

  // An opencode agent: prefixed identifiers are its shape, and `standard_vendors`
  // is the server's mirror of the vendor ids that may head one.
  const opencode = (over: Partial<AgentSupply>): AgentSupply =>
    agent({ backend: 'opencode', menu_kind: 'open', standard_vendors: ['anthropic', 'zhipuai'], ...over });

  it('renders nothing when no model resolves', () => {
    expect(friendlyModelName(agent({ current: null, selected_model_id: null }), [])).toBe('');
  });

  it('prefers the supplying source name over another source that also lists the model', () => {
    const sources = [src('src_b', [model('m1', 'Wrong One')]), src('src_a', [model('m1', 'Right One')])];
    const a = agent({ current: { model_id: 'm1', source_id: 'src_a', channel: 'hub' } });
    expect(friendlyModelName(a, sources)).toBe('Right One');
  });

  it('falls back from 「what is serving」 to 「what was selected」', () => {
    // `current` is null by contract while waiting or interrupted, and a model box
    // that empties out exactly then hides WHICH model has no supply.
    const a = agent({ current: null, selected_model_id: 'm1' });
    expect(friendlyModelName(a, [src('src_a', [model('m1', 'Opus')])])).toBe('Opus');
  });

  // The 「bare id」 in the contract means NO PROVIDER PREFIX, which is not the same
  // as no slash: `SuppliedModel.id` carries no `pattern`, so a relay endpoint or a
  // manual entry may legitimately supply a slash-bearing id under its own name.
  it('keeps a slash-bearing supplied id whole', () => {
    const id = 'accounts/fireworks/models/llama-v3';
    const sources = [src('relay', [model(id, 'Llama v3 (Fireworks)')])];
    expect(friendlyModelName(agent({ selected_model_id: id }), sources)).toBe('Llama v3 (Fireworks)');
  });

  it('never collides a relay id with another source’s same-suffix model', () => {
    // The bug: cutting through the last slash turned this into `llama-v3` and
    // rendered the OTHER source's name for it. Rebuilt, that model is
    // `custom/llama-v3` — not what was selected — so nothing matches.
    const sources = [src('other', [model('llama-v3', 'Llama v3 (Together)')])];
    const a = agent({ selected_model_id: 'accounts/fireworks/models/llama-v3' });
    expect(friendlyModelName(a, sources)).toBe('accounts/fireworks/models/llama-v3');
  });

  it('renders an unknown id as selected rather than as its tail', () => {
    expect(friendlyModelName(agent({ selected_model_id: 'vendor/x/unknown' }), [])).toBe('vendor/x/unknown');
  });

  it('renders a supplied id with no display name as itself', () => {
    const sources = [src('relay', [model('accounts/f/models/llama-v3')])];
    const a = agent({ selected_model_id: 'accounts/f/models/llama-v3' });
    expect(friendlyModelName(a, sources)).toBe('accounts/f/models/llama-v3');
  });

  // The counter-example that keeps the second lookup alive: an opencode SELECTION
  // is a prefixed identifier (`zhipuai/glm-5.2`) whose SuppliedModel.id is bare, so
  // the full form is absent from the inventory and only the rebuild resolves it.
  it('resolves a prefixed identifier against the source that supplies it bare', () => {
    const sources = [src('glm', [model('glm-5.2', 'GLM-5.2')], 'zhipuai')];
    expect(friendlyModelName(opencode({ selected_model_id: 'zhipuai/glm-5.2' }), sources)).toBe('GLM-5.2');
  });

  it('resolves to the bare id even when nothing names it', () => {
    const sources = [src('glm', [model('glm-5.2')], 'zhipuai')];
    expect(friendlyModelName(opencode({ selected_model_id: 'zhipuai/glm-5.2' }), sources)).toBe('glm-5.2');
  });

  // `custom/` is the identifier scheme's catch-all provider, so a non-standard
  // vendor heads NO identifier of its own — a slash-count rule would have missed
  // this one, and the rebuild gets it for free.
  it('resolves a custom/ identifier supplied by a non-standard vendor', () => {
    const sources = [src('relay', [model('glm-5.2-air', 'GLM-5.2 Air')], 'relay.example')];
    expect(friendlyModelName(opencode({ selected_model_id: 'custom/glm-5.2-air' }), sources)).toBe('GLM-5.2 Air');
  });

  it('does not resolve a standard-vendor prefix the supplying source does not carry', () => {
    // The relay's `glm-5.2` is `custom/glm-5.2`; claiming it for `zhipuai/glm-5.2`
    // would name a vendor the user is not actually reaching.
    const sources = [src('relay', [model('glm-5.2', 'GLM-5.2 (relay)')], 'relay.example')];
    expect(friendlyModelName(opencode({ selected_model_id: 'zhipuai/glm-5.2' }), sources)).toBe('zhipuai/glm-5.2');
  });

  it('prefers the supplying source when two sources rebuild the same identifier', () => {
    const sources = [
      src('glm_b', [model('glm-5.2', 'Wrong One')], 'zhipuai'),
      src('glm_a', [model('glm-5.2', 'Right One')], 'zhipuai'),
    ];
    const a = opencode({ current: { model_id: 'zhipuai/glm-5.2', source_id: 'glm_a', channel: 'hub' } });
    expect(friendlyModelName(a, sources)).toBe('Right One');
  });
});
