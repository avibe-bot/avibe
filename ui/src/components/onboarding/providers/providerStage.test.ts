// The rules the provider stage is easiest to get wrong, stated over the real shapes
// the server produces. Each case here is a mistake that would render plausibly and
// be wrong: a vendor occupying two slots because a scan and a source spell it
// differently, a connected card changing colour because someone toggled a selection,
// a summary that promises providers nobody added, a CTA that offers to continue with
// nothing to continue with.
import { describe, expect, it } from 'vitest';

import type { MigrationItem, Source, SourceStatus } from '@/components/settings/models/types';
import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';

import { readyRegion, loadingRegion, degradedRegion, unreadRegion } from '@/components/settings/models/regionRead';
import type { AgentSupply, RuntimeDependency, RuntimeHealth } from '@/components/settings/models/types';

import {
  ACTION_LABEL,
  PROVIDER_SLOT_COUNT,
  addedThroughMoreCount,
  adoptionBackend,
  gatewayIntent,
  pendingImportRows,
  providerAction,
  providerSetupAction,
  providerSlots,
  providerSummary,
  reconcileSelection,
  slotSelected,
  toggleSlotSelection,
  unlistedDetected,
  usableSource,
} from './providerStage';

const row = (over: Partial<MigrationItem>): MigrationItem => ({
  id: 'mig_1',
  backend: 'codex',
  kind: 'api_key',
  masked_detail: 'sk-…9f21 · Codex configuration',
  masked_credential: 'sk-…9f21',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'openai',
  ...over,
});

const source = (over: Partial<Source> & { id: string; vendor: string }): Source => ({
  last_discovered_at: null,
  kind: 'api_key',
  display_name: over.vendor,
  protocol: 'openai_chat',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active' },
  models: [],
  ...over,
});

const scanOf = (...items: MigrationItem[]) => ({ items });

describe('providerSlots', () => {
  it('fills the empty stage with the shortlist it opens with, in its order', () => {
    const slots = providerSlots({ sources: [], scan: null });

    expect(slots).toHaveLength(PROVIDER_SLOT_COUNT);
    expect(slots.map((slot) => slot.vendor)).toEqual(['openai', 'anthropic']);
    expect(slots.every((slot) => slot.kind === 'empty')).toBe(true);
  });

  it('puts what exists ahead of what was merely found, and tops up from the shortlist', () => {
    const slots = providerSlots({
      sources: [source({ id: 'src_1', vendor: 'zhipuai', display_name: 'Zhipu' })],
      scan: null,
    });

    expect(slots.map((slot) => [slot.vendor, slot.kind])).toEqual([
      ['zhipuai', 'connected'],
      ['openai', 'empty'],
    ]);
  });

  it('never seats one provider twice when the scan and the source spell it differently', () => {
    // An OpenCode row calls Gemini `google`; a source created from the catalog
    // calls it `gemini`. Without the alias both would take a slot and the stage
    // would offer to import a key for a provider already connected.
    const slots = providerSlots({
      sources: [source({ id: 'src_1', vendor: 'gemini' })],
      scan: scanOf(row({ id: 'mig_g', backend: 'opencode', kind: 'opencode_provider', vendor: 'google' })),
    });

    expect(slots.map((slot) => slot.vendor)).toEqual(['gemini', 'openai']);
  });

  it('ignores a source that cannot be used and offers the shortlist instead', () => {
    const slots = providerSlots({
      sources: [source({ id: 'src_1', vendor: 'xai', state: { status: 'needs_action', detail_key: null } })],
      scan: null,
    });

    expect(slots.map((slot) => slot.vendor)).toEqual(['openai', 'anthropic']);
  });

  it('keeps a detected candidate out of the stage when its consent group is blocked', () => {
    // A backend whose linked rows include something that cannot be imported is
    // reviewed in Settings, not offered a one-click card here.
    const slots = providerSlots({
      sources: [],
      scan: scanOf(
        row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
        row({ id: 'mig_b', backend: 'claude', vendor: 'anthropic', kind: 'oauth_native', proposed_action: 'reauth' }),
      ),
    });

    expect(slots.map((slot) => slot.kind)).toEqual(['empty', 'empty']);
  });

  it('carries the masked credential and every consenting backend on a detected card', () => {
    const [slot] = providerSlots({
      sources: [],
      scan: scanOf(
        row({ id: 'mig_1', backend: 'codex', vendor: 'openai', masked_credential: 'sk-…9f21' }),
        row({ id: 'mig_2', backend: 'opencode', kind: 'opencode_provider', vendor: 'openai' }),
      ),
    });

    expect(slot).toMatchObject({ vendor: 'openai', kind: 'detected', mask: 'sk-…9f21' });
    expect([...slot.backends].sort()).toEqual(['codex', 'opencode']);
  });

  it('leaves a row the server did not name to the import dialog', () => {
    // No vendor means no brand slot. The row still migrates — it just has no card.
    const slots = providerSlots({
      sources: [],
      scan: scanOf(row({ id: 'mig_1', vendor: undefined })),
    });

    expect(slots.every((slot) => slot.kind === 'empty')).toBe(true);
  });
});

describe('unlistedDetected', () => {
  it('is empty when the stage is already drawing everything that was found', () => {
    // An add dialog whose first tab repeats the two cards immediately behind it
    // offers nothing — this emptiness is what takes the tab away.
    const input = {
      sources: [],
      scan: scanOf(
        row({ id: 'mig_1', backend: 'codex', vendor: 'openai' }),
        row({ id: 'mig_2', backend: 'claude', vendor: 'anthropic' }),
      ),
    };

    expect(providerSlots(input).map((slot) => slot.kind)).toEqual(['detected', 'detected']);
    expect(unlistedDetected(input)).toEqual([]);
  });

  it('lists what the stage ran out of room for', () => {
    const detected = unlistedDetected({
      sources: [],
      scan: scanOf(
        row({ id: 'mig_1', backend: 'codex', vendor: 'openai' }),
        row({ id: 'mig_2', backend: 'claude', vendor: 'anthropic' }),
        row({ id: 'mig_3', backend: 'opencode', kind: 'opencode_provider', vendor: 'google' }),
      ),
    });

    // `google` is the scan's spelling of the brand the catalog calls `gemini`.
    expect(detected.map((slot) => slot.vendor)).toEqual(['gemini']);
  });

  it('never offers to review a brand that is already connected', () => {
    const detected = unlistedDetected({
      sources: [
        source({ id: 'src_1', vendor: 'openai' }),
        source({ id: 'src_2', vendor: 'anthropic' }),
      ],
      scan: scanOf(row({ id: 'mig_1', backend: 'codex', vendor: 'openai' })),
    });

    expect(detected).toEqual([]);
  });
});

describe('pendingImportRows', () => {
  it('submits nothing while nothing is consented to', () => {
    expect(pendingImportRows({
      scan: scanOf(row({ id: 'mig_1' })),
      selectedBackends: [],
    })).toEqual([]);
  });

  it('counts every importable row of a consented backend, not the cards it drew', () => {
    // Two keys, one brand, one card — and two rows in the batch. A count taken from
    // the stage would promise one import and perform two.
    const rows = pendingImportRows({
      scan: scanOf(
        row({ id: 'mig_1', backend: 'codex', vendor: 'openai' }),
        row({ id: 'mig_2', backend: 'codex', vendor: 'openai', masked_credential: 'sk-…0001' }),
      ),
      selectedBackends: ['codex'],
    });

    expect(rows.map((item) => item.id)).toEqual(['mig_1', 'mig_2']);
  });

  it('submits nothing for a group holding any row it cannot import', () => {
    // Consent is per group, never per row: one row the server does not propose
    // importing blocks the whole custody boundary, so the batch is empty rather
    // than partial. Settings is where a mixed group gets resolved.
    const rows = pendingImportRows({
      scan: scanOf(
        row({ id: 'mig_1', backend: 'codex' }),
        row({ id: 'mig_2', backend: 'codex', proposed_action: 'keep' }),
      ),
      selectedBackends: ['codex'],
    });

    expect(rows).toEqual([]);
  });

  it('refuses a backend this entry point cannot consent to, however it got selected', () => {
    // A blocked group is Settings' to resolve. Naming it in the selection must not
    // make the CTA promise a batch the dialog would decline to build.
    const rows = pendingImportRows({
      scan: scanOf(
        row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
        row({ id: 'mig_b', backend: 'claude', kind: 'oauth_native', proposed_action: 'reauth' }),
      ),
      selectedBackends: ['claude'],
    });

    expect(rows).toEqual([]);
  });
});

describe('toggleSlotSelection', () => {
  const stage = (...items: MigrationItem[]) => providerSlots({ sources: [], scan: scanOf(...items) });

  it('consents to the whole linked closure, not just the card that was pressed', () => {
    // Codex and OpenCode read the same credential file, so migrating one migrates
    // both. Half of that consent does not exist, so it is never representable.
    const items = [
      row({ id: 'mig_1', backend: 'codex', vendor: 'openai', required_backends: ['codex', 'opencode'] }),
      row({ id: 'mig_2', backend: 'opencode', kind: 'opencode_provider', vendor: 'openai', required_backends: ['codex', 'opencode'] }),
    ];
    const [slot] = stage(...items);

    const next = toggleSlotSelection({ scan: scanOf(...items), selectedBackends: [] }, slot);

    expect([...next].sort()).toEqual(['codex', 'opencode']);
  });

  it('gives the whole closure back when the card is pressed again', () => {
    const items = [
      row({ id: 'mig_1', backend: 'codex', vendor: 'openai', required_backends: ['codex', 'opencode'] }),
      row({ id: 'mig_2', backend: 'opencode', kind: 'opencode_provider', vendor: 'openai', required_backends: ['codex', 'opencode'] }),
    ];
    const [slot] = stage(...items);

    const next = toggleSlotSelection(
      { scan: scanOf(...items), selectedBackends: ['codex', 'opencode'] },
      slot,
    );

    expect(next).toEqual([]);
  });

  it('leaves another card\'s consent alone', () => {
    const items = [
      row({ id: 'mig_1', backend: 'codex', vendor: 'openai' }),
      row({ id: 'mig_2', backend: 'claude', vendor: 'anthropic' }),
    ];
    const [openai] = stage(...items);

    const next = toggleSlotSelection({ scan: scanOf(...items), selectedBackends: ['claude'] }, openai);

    expect([...next].sort()).toEqual(['claude', 'codex']);
  });

  it('is inert on a connected card, so a fact cannot be toggled off', () => {
    const items = [row({ id: 'mig_1', backend: 'claude', vendor: 'anthropic' })];
    const [connected] = providerSlots({
      sources: [source({ id: 'src_1', vendor: 'openai' })],
      scan: scanOf(...items),
    });

    expect(connected.kind).toBe('connected');
    expect(toggleSlotSelection({ scan: scanOf(...items), selectedBackends: ['claude'] }, connected))
      .toEqual(['claude']);
  });
});

describe('reconcileSelection', () => {
  it('keeps a consent the fresh scan still supports', () => {
    expect(reconcileSelection({
      scan: scanOf(row({ id: 'mig_1', backend: 'codex' })),
      selectedBackends: ['codex'],
    })).toEqual(['codex']);
  });

  it('drops a backend the rescan no longer offers', () => {
    // What this prevents: importing one of two consented backends, rescanning, and
    // leaving the CTA counting a batch the dialog would now build as empty.
    expect(reconcileSelection({
      scan: scanOf(row({ id: 'mig_1', backend: 'codex' })),
      selectedBackends: ['codex', 'claude'],
    })).toEqual(['codex']);
  });

  it('drops a backend that became blocked between scans', () => {
    expect(reconcileSelection({
      scan: scanOf(
        row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
        row({ id: 'mig_b', backend: 'claude', kind: 'oauth_native', proposed_action: 'reauth' }),
      ),
      selectedBackends: ['claude'],
    })).toEqual([]);
  });

  it('empties the selection when the scan itself is gone', () => {
    expect(reconcileSelection({ scan: null, selectedBackends: ['codex'] })).toEqual([]);
  });
});

const supply = (over: Partial<AgentSupply> & { backend: AgentSupply['backend'] }): AgentSupply => ({
  cli_present: true,
  mode: 'direct',
  menu_kind: 'native',
  ...over,
});

describe('adoptionBackend', () => {
  it('names nothing when no CLI is installed', () => {
    expect(adoptionBackend([supply({ backend: 'codex', cli_present: false })])).toBeNull();
  });

  it('names a present backend that has not been adopted yet', () => {
    expect(adoptionBackend([
      supply({ backend: 'claude', cli_present: false }),
      supply({ backend: 'codex' }),
    ])).toBe('codex');
  });

  it('prefers the unadopted backend over an adopted one earlier in the order', () => {
    // `resumeGatewayAdoption` returns early — without touching the runtime at all —
    // when the backend it is named for is already in hub mode. Naming `claude` here
    // would skip the install and start this whole screen exists to perform.
    expect(adoptionBackend([
      supply({ backend: 'claude', mode: 'hub' }),
      supply({ backend: 'codex' }),
    ])).toBe('codex');
  });

  it('falls back to the first present backend when every one is already adopted', () => {
    expect(adoptionBackend([
      supply({ backend: 'codex', mode: 'hub' }),
      supply({ backend: 'claude', mode: 'hub' }),
    ])).toBe('claude');
  });
});

const runtime = (health: RuntimeHealth, over: Partial<RuntimeDependency> = {}): RuntimeDependency => ({
  contract_version: 10,
  manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1.0.0', source_sha: 'sha', assets: [] },
  status: { verified: true, health },
  ...over,
});

describe('gatewayIntent', () => {
  const enabled = { capability: 'enabled', gatewayEnabled: true } as const;

  it('has nothing to do about an engine that is already serving', () => {
    expect(gatewayIntent({ ...enabled, runtimeRead: readyRegion(runtime('ok')) }))
      .toEqual({ kind: 'running' });
  });

  it('treats a degraded but running engine as serving, because it still routes', () => {
    expect(gatewayIntent({ ...enabled, runtimeRead: readyRegion(runtime('degraded')) }))
      .toEqual({ kind: 'running' });
  });

  it('installs a missing engine where installation is admitted', () => {
    expect(gatewayIntent({ ...enabled, runtimeRead: readyRegion(runtime('not_installed')) }))
      .toEqual({ kind: 'resume', step: 'install' });
  });

  it('starts an installed but stopped engine even where installation is unsupported', () => {
    // Admission gates fresh installation only. Refusing to START a Hub that is
    // already on the machine would strand a working engine on a technicality.
    const unsupported = runtime('not_started', {
      manifest: { name: 'cliproxyapi', resolution: 'unsupported', version: '1.0.0', source_sha: 'sha', assets: [] },
    });

    expect(gatewayIntent({ ...enabled, runtimeRead: readyRegion(unsupported) }))
      .toEqual({ kind: 'resume', step: 'start' });
  });

  it('reports installation as unsupported rather than attempting it', () => {
    const unsupported = runtime('not_installed', {
      manifest: { name: 'cliproxyapi', resolution: 'unsupported', version: '1.0.0', source_sha: 'sha', assets: [] },
    });

    expect(gatewayIntent({ ...enabled, runtimeRead: readyRegion(unsupported) }))
      .toEqual({ kind: 'unsupported' });
  });

  it.each([
    ['loading', loadingRegion<RuntimeDependency>()],
    ['unread', unreadRegion<RuntimeDependency>()],
    ['degraded', degradedRegion<RuntimeDependency>('read_failed')],
  ] as const)('authorizes nothing on a %s read', (_label, runtimeRead) => {
    // None of these is evidence the engine is absent. Acting on one would install
    // over a Hub that is merely unreachable this second.
    expect(gatewayIntent({ ...enabled, runtimeRead })).toEqual({ kind: 'waiting' });
  });

  it('waits rather than re-enabling a configuration someone turned off', () => {
    expect(gatewayIntent({ ...enabled, runtimeRead: readyRegion(runtime('not_installed', { enabled: false })) }))
      .toEqual({ kind: 'waiting' });
  });

  it.each([
    ['pending capability', { capability: 'pending', gatewayEnabled: true }],
    ['disabled capability', { capability: 'disabled', gatewayEnabled: true }],
    ['config not yet read', { capability: 'enabled', gatewayEnabled: null }],
    ['config turned off', { capability: 'enabled', gatewayEnabled: false }],
  ] as const)('waits while %s', (_label, gate) => {
    expect(gatewayIntent({ ...gate, runtimeRead: readyRegion(runtime('not_installed')) }))
      .toEqual({ kind: 'waiting' });
  });

  it('still reports a running engine the flow is otherwise not ready to use', () => {
    // Running health is independent of admission: the card should say the engine is
    // up even while the configuration read that gates the flow is still pending.
    expect(gatewayIntent({
      capability: 'pending',
      gatewayEnabled: null,
      runtimeRead: readyRegion(runtime('ok')),
    })).toEqual({ kind: 'running' });
  });
});

describe('slotSelected', () => {
  const connected = providerSlots({
    sources: [source({ id: 'src_1', vendor: 'openai' })],
    scan: null,
  })[0];
  const detected = providerSlots({
    sources: [],
    scan: scanOf(row({ id: 'mig_1', backend: 'codex', vendor: 'openai' })),
  })[0];

  it('never reports a connected card as selected, whatever is consented to', () => {
    // A connected card states a fact. If a selection toggle could change its fill,
    // deselecting a detected key would make an existing source look disconnected.
    expect(slotSelected(connected, [])).toBe(false);
    expect(slotSelected(connected, ['claude', 'codex', 'opencode'])).toBe(false);
  });

  it('reports a detected card selected only once its whole group is consented to', () => {
    expect(slotSelected(detected, [])).toBe(false);
    expect(slotSelected(detected, ['codex'])).toBe(true);
  });

  it('does not report a two-backend card selected on half its consent', () => {
    const [both] = providerSlots({
      sources: [],
      scan: scanOf(
        row({ id: 'mig_1', backend: 'codex', vendor: 'openai' }),
        row({ id: 'mig_2', backend: 'opencode', kind: 'opencode_provider', vendor: 'openai' }),
      ),
    });

    expect(slotSelected(both, ['codex'])).toBe(false);
    expect(slotSelected(both, ['codex', 'opencode'])).toBe(true);
  });
});

describe('providerSummary', () => {
  const slots = (input: Parameters<typeof providerSlots>[0]) => providerSlots(input);

  it('says nothing when nothing is added or chosen', () => {
    expect(providerSummary({ slots: [], sources: [], selected: [], failed: false })).toEqual({ kind: 'none' });
  });

  it('counts every added provider, not just the two on the stage', () => {
    const sources = [
      source({ id: 'src_1', vendor: 'openai' }),
      source({ id: 'src_2', vendor: 'anthropic' }),
      source({ id: 'src_3', vendor: 'xai' }),
    ];

    expect(providerSummary({ slots: slots({ sources, scan: null }), sources, selected: [], failed: false }))
      .toEqual({ kind: 'added', count: 3, names: ['OpenAI', 'Anthropic', 'xAI'] });
  });

  it('counts one provider once however many keys it has', () => {
    const sources = [
      source({ id: 'src_1', vendor: 'openai' }),
      source({ id: 'src_2', vendor: 'openai', display_name: 'OpenAI (work)' }),
    ];

    expect(providerSummary({ slots: [], sources, selected: [], failed: false }))
      .toMatchObject({ kind: 'added', count: 1 });
  });

  it('prefers what is really there over what is merely consented to', () => {
    const sources = [source({ id: 'src_1', vendor: 'openai' })];
    const scan = scanOf(row({ id: 'mig_1', backend: 'claude', vendor: 'anthropic' }));

    expect(providerSummary({
      slots: slots({ sources, scan }),
      sources,
      selected: ['claude'],
      failed: false,
    })).toMatchObject({ kind: 'added' });
  });

  it('names the consented cards while nothing has been added yet', () => {
    const scan = scanOf(row({ id: 'mig_1', backend: 'claude', vendor: 'anthropic' }));

    expect(providerSummary({
      slots: slots({ sources: [], scan }),
      sources: [],
      selected: ['claude'],
      failed: false,
    })).toEqual({ kind: 'selected', count: 1, names: ['Anthropic'] });
  });

  it('reports a failure ahead of anything it could otherwise say', () => {
    const sources = [source({ id: 'src_1', vendor: 'openai' })];

    expect(providerSummary({ slots: [], sources, selected: [], failed: true })).toEqual({ kind: 'error' });
  });
});

describe('addedThroughMoreCount', () => {
  it('counts only the added sources that are still usable', () => {
    const sources = [
      source({ id: 'src_1', vendor: 'openai' }),
      source({ id: 'src_2', vendor: 'xai', state: { status: 'error', detail_key: null } }),
    ];

    expect(addedThroughMoreCount(['src_1', 'src_2', 'src_gone'], sources)).toBe(1);
  });

  it('counts a repeated id once', () => {
    expect(addedThroughMoreCount(['src_1', 'src_1'], [source({ id: 'src_1', vendor: 'openai' })])).toBe(1);
  });
});

describe('usableSource', () => {
  const cases: [SourceStatus, boolean][] = [
    ['active', true],
    ['standby', true],
    ['cooldown', false],
    ['needs_action', false],
    ['error', false],
  ];

  it.each(cases)('treats %s as usable=%s', (status, expected) => {
    expect(usableSource(source({ id: 'src_1', vendor: 'openai', state: { status, detail_key: null } }))).toBe(expected);
  });
});

describe('providerAction', () => {
  const state = (over: Partial<Parameters<typeof providerAction>[0]> = {}) => providerAction({
    pendingCount: 0,
    importFailed: false,
    hasSource: false,
    gatewayBusy: false,
    verifying: false,
    ...over,
  });

  it('offers to add when there is nothing yet', () => {
    expect(state()).toEqual({ kind: 'add', count: 0 });
  });

  it('offers to continue once a source exists', () => {
    expect(state({ hasSource: true })).toEqual({ kind: 'continue', count: 0 });
  });

  it('puts a pending take-over ahead of continuing, and carries its count', () => {
    expect(state({ hasSource: true, pendingCount: 3 })).toEqual({ kind: 'import', count: 3 });
  });

  it('changes what it offers after a failed batch without losing the count', () => {
    expect(state({ pendingCount: 2, importFailed: true })).toEqual({ kind: 'retryImport', count: 2 });
  });

  it('reports the engine coming up ahead of everything else', () => {
    expect(state({ gatewayBusy: true, pendingCount: 3, hasSource: true })).toEqual({ kind: 'connecting', count: 0 });
  });

  it('reports a connection being read back ahead of what it would unlock', () => {
    expect(state({ verifying: true, hasSource: true })).toEqual({ kind: 'checking', count: 0 });
  });
});

describe('providerSetupAction', () => {
  it('sends the count only when there is one, so a plural key never renders a zero', () => {
    expect(providerSetupAction({ kind: 'import', count: 2 })).toEqual({
      labelKey: 'onboarding.providers.actionImport',
      labelArgs: { count: 2 },
      disabled: false,
      busy: false,
      icon: 'arrow-right',
    });
    expect(providerSetupAction({ kind: 'continue', count: 0 })).not.toHaveProperty('labelArgs');
  });

  it('marks a busy state both busy and disabled, so neither alone can let a click through', () => {
    for (const kind of ['connecting', 'checking'] as const) {
      expect(providerSetupAction({ kind, count: 0 })).toMatchObject({ disabled: true, busy: true, icon: 'spinner' });
    }
  });

  it('drops the arrow on the one state that opens a dialog instead of moving on', () => {
    expect(providerSetupAction({ kind: 'add', count: 0 }).icon).toBe('none');
  });

  // `labelKey` is handed to the shell through an assertion, because C1 ships the two
  // counted labels as plural families and C2's type admits only count-free leaves.
  // This is that assertion's evidence: the thing the type was protecting was that a
  // screen cannot name a string which has not shipped, and here it is, checked
  // against both bundles rather than promised. A plural family counts as shipped
  // when `_other` is there — that is the form `t` reaches for at every count in the
  // two locales Avibe ships.
  it('names only labels that have shipped, in both locales', () => {
    const resolves = (bundle: unknown, key: string): boolean => typeof key
      .split('.')
      .reduce<unknown>(
        (node, part) => (node && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined),
        bundle,
      ) === 'string';

    for (const key of Object.values(ACTION_LABEL)) {
      for (const [locale, bundle] of [['en', en], ['zh', zh]] as const) {
        expect(resolves(bundle, key) || resolves(bundle, `${key}_other`), `${key} in ${locale}`).toBe(true);
      }
    }
  });
});
