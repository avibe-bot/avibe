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
  COUNTED_LABEL,
  PLAIN_LABEL,
  PROVIDER_SLOT_COUNT,
  addedThroughMoreCount,
  adoptionBackend,
  defaultSelection,
  gatewayEvidenceSettled,
  gatewayIntent,
  offeredImportKeys,
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

  it('keeps a detected candidate on the stage when its consent group is blocked, and says why', () => {
    // A backend whose linked rows include something this entry cannot import is
    // reviewed in Settings. What it is not is invisible: the key is on the machine,
    // and replacing it with an empty 「add Anthropic」 invitation is how a person
    // adds a second copy of the key they already have.
    const slots = providerSlots({
      sources: [],
      scan: scanOf(
        row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
        row({ id: 'mig_b', backend: 'claude', vendor: 'anthropic', kind: 'oauth_native', proposed_action: 'reauth' }),
      ),
    });

    expect(slots.map((slot) => [slot.vendor, slot.kind])).toEqual([['anthropic', 'detected'], ['openai', 'empty']]);
    // Found, with a reason, and no permission: nothing in the card's `backends` is a
    // backend anything here may take over. The reason is the migration feature's own
    // note for that row — a server note nobody recognises still reads as a sentence.
    expect(slots[0].reasons).toEqual(['settings.models.migration.blocked.fallback']);
    expect(slots[0].backends).toEqual([]);
  });

  it('keeps the card and names setup’s own scope when the group holds an importable subscription', () => {
    // The harder half of the same rule, and the one that reads as working. This
    // OAuth store is importable — Settings would take it over — so nothing on the
    // server declines it. What declines it is setup's own scope: its copy names API
    // keys only. And the key beside it cannot be taken alone, because the server
    // migrates a backend whole and refuses a batch that omits one of its rows.
    const slots = providerSlots({
      sources: [],
      scan: scanOf(
        row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
        row({ id: 'mig_b', backend: 'claude', vendor: 'anthropic', kind: 'oauth_native' }),
      ),
    });

    expect(slots.map((slot) => [slot.vendor, slot.kind])).toEqual([['anthropic', 'detected'], ['openai', 'empty']]);
    expect(slots[0].reasons).toEqual(['onboarding.import.outOfScope']);
    expect(slots[0].backends).toEqual([]);
  });

  it('keeps a blocked-only scan on the stage rather than falling back to the shortlist', () => {
    // Nothing here is takeable from setup, so the actionable count is zero. The stage
    // is still what the machine holds: an empty shortlist would say nothing was found.
    const scan = scanOf(
      row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
      row({ id: 'mig_b', backend: 'claude', vendor: 'anthropic', kind: 'oauth_native' }),
      row({ id: 'mig_c', backend: 'opencode', kind: 'opencode_provider', vendor: 'zhipuai' }),
      row({ id: 'mig_d', backend: 'opencode', kind: 'oauth_native', proposed_action: 'keep_native', vendor: undefined }),
    );
    const slots = providerSlots({ sources: [], scan });

    expect(slots.map((slot) => [slot.vendor, slot.kind])).toEqual([
      ['anthropic', 'detected'],
      ['zhipuai', 'detected'],
    ]);
    expect(slots.every((slot) => slot.reasons.length > 0 && slot.backends.length === 0)).toBe(true);

    // Visible, and contributing nothing: no consent by default, none obtainable by
    // pressing, no rows to submit, and nothing for the capsule to advertise.
    expect(defaultSelection(scan)).toEqual([]);
    expect(slots.some((slot) => slotSelected(slot, ['claude', 'opencode']))).toBe(false);
    expect(toggleSlotSelection({ scan, selectedBackends: [] }, slots[0])).toEqual([]);
    expect(pendingImportRows({ scan, selectedBackends: ['claude', 'opencode'] })).toEqual([]);
    expect(offeredImportKeys({ scan, selectedBackends: [] })).toEqual([]);
  });

  it('does not lend one vendor’s takeable group the reason of its blocked one', () => {
    // The same brand's key sits in two backends: one this entry may take over whole,
    // one it may not. The card is the takeable one — pressing it consents to the
    // group it may — and the blocked group keeps its reason in the review it belongs
    // to. A card saying 「not from here」 that was pressable anyway contradicts itself.
    const slots = providerSlots({
      sources: [],
      scan: scanOf(
        row({ id: 'mig_a', backend: 'codex', vendor: 'openai' }),
        row({ id: 'mig_b', backend: 'opencode', kind: 'opencode_provider', vendor: 'openai' }),
        row({ id: 'mig_c', backend: 'opencode', kind: 'oauth_native', proposed_action: 'keep_native' }),
      ),
    });

    expect(slots[0]).toMatchObject({ vendor: 'openai', kind: 'detected', reasons: [] });
    // Consent covers the backend that offered it, and not the one that did not.
    expect(slots[0].backends).toEqual(['codex']);
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

  it('marks a source saved but not yet verified, and nothing else', () => {
    // `save_unverified: true` is what both hosts of the key form send, so a card can
    // be connected with nothing having confirmed the key answers. Settings discloses
    // that state; a card that did not would report it as a working provider.
    const slots = providerSlots({
      sources: [
        source({ id: 'src_1', vendor: 'openai', state: { status: 'standby' }, verification_pending: 'vp_fixture' }),
        source({ id: 'src_2', vendor: 'anthropic' }),
      ],
      scan: null,
    });

    expect(slots.map((slot) => [slot.vendor, slot.kind, slot.pending])).toEqual([
      ['openai', 'connected', true],
      ['anthropic', 'connected', false],
    ]);
  });

  it('never marks a detected or empty card pending: neither has a key that was written', () => {
    const slots = providerSlots({
      sources: [],
      scan: scanOf(row({ id: 'mig_1', backend: 'codex', vendor: 'openai' })),
    });

    expect(slots.map((slot) => [slot.kind, slot.pending])).toEqual([
      ['detected', false],
      ['empty', false],
    ]);
  });

  it('names a credential the server did not name by its mask, never by a brand', () => {
    // No vendor and no display name: an older server, or a key in a file nothing
    // claims. It is still a key on this machine, so it keeps its card — named by
    // what is known about it. Naming it after the backend that held the file would
    // put 「OpenAI」 on a key that may be anything, and a brand mark would do the
    // same silently, so the card falls back to the key it is.
    const slots = providerSlots({
      sources: [],
      scan: scanOf(row({
        id: 'mig_1',
        vendor: undefined,
        display_name: undefined,
        masked_detail: 'sk-…abcd · Codex configuration',
        masked_credential: undefined,
      })),
    });

    expect(slots.map((slot) => slot.kind)).toEqual(['detected', 'empty']);
    expect(slots[0].label).toBe('sk-…abcd · Codex configuration');
    // Its own identity, so a second unnamed row is a second card rather than a
    // collision, and no brand mark: `brand` is what the glyph is filed under.
    expect(slots[0]).toMatchObject({ vendor: 'mig_1', brand: null, mask: null });
    // And takeable: nothing about it being unnamed blocks the group it belongs to.
    expect(slots[0].backends).toEqual(['codex']);
    expect(slots[0].reasons).toEqual([]);
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

  it('still offers a detected credential whose brand is already connected', () => {
    // The scan reads native stores and knows nothing about what the Hub already
    // holds, so a row sharing a brand with a connected source is a second key
    // until something compares the credentials. The stage collapsing the two into
    // one card is presentation; letting that collapse answer 「is there another key
    // on this machine」 is how a row the capsule still counts disappears from the
    // stage AND from the only pane that could take it.
    const input = {
      sources: [
        source({ id: 'src_1', vendor: 'openai', masked_credential: 'sk-…1111' }),
        source({ id: 'src_2', vendor: 'anthropic', masked_credential: 'sk-…2222' }),
      ],
      scan: scanOf(row({ id: 'mig_1', backend: 'codex', vendor: 'openai' })),
    };

    // One card per brand, as before: two connected facts, no third slot.
    expect(providerSlots(input).map((slot) => [slot.vendor, slot.kind])).toEqual([
      ['openai', 'connected'],
      ['anthropic', 'connected'],
    ]);
    // And the row is reviewable, carrying its own masked credential so the person
    // can tell it apart from the key already connected under that brand.
    expect(unlistedDetected(input).map((slot) => [slot.vendor, slot.kind, slot.mask])).toEqual([
      ['openai', 'detected', 'sk-…9f21'],
    ]);
  });

  it('keeps an alias-collapsed detection reviewable behind the card that absorbed it', () => {
    // `google` and `gemini` are one brand, so the stage draws one card — the
    // connected one, because it is a fact. The detection behind it is still a
    // separate native store, and Add more is where it stays reachable.
    const input = {
      sources: [source({ id: 'src_1', vendor: 'gemini', masked_credential: 'sk-…1111' })],
      scan: scanOf(row({ id: 'mig_g', backend: 'opencode', kind: 'opencode_provider', vendor: 'google' })),
    };

    expect(providerSlots(input).map((slot) => [slot.vendor, slot.kind])).toEqual([
      ['gemini', 'connected'],
      ['openai', 'empty'],
    ]);
    expect(unlistedDetected(input).map((slot) => slot.vendor)).toEqual(['gemini']);
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
        row({ id: 'mig_2', backend: 'codex', proposed_action: 'keep_native' }),
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

  it('never submits a subscription store alongside the key it is linked to', () => {
    // The one that would have shipped silently: the batch is unscoped by design, so
    // a backend reaching the selection at all is what has to be impossible. If this
    // returned both rows, setup would take over an OAuth sign-in its copy never
    // mentioned; if it returned only the key, the server would refuse the batch.
    const rows = pendingImportRows({
      scan: scanOf(
        row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
        row({ id: 'mig_b', backend: 'claude', vendor: 'anthropic', kind: 'oauth_native' }),
      ),
      selectedBackends: ['claude'],
    });

    expect(rows).toEqual([]);
  });
});

describe('offeredImportKeys', () => {
  it('advertises only what the review could really act on', () => {
    // The capsule's number and the dialog's batch come from one grouping. A count
    // taken from the raw scan would offer keys the review then refuses to build,
    // which reads as the dialog losing them.
    const offered = offeredImportKeys({
      scan: scanOf(
        row({ id: 'mig_1', backend: 'codex', vendor: 'openai' }),
        row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
        row({ id: 'mig_b', backend: 'claude', kind: 'oauth_native', proposed_action: 'reauth' }),
      ),
      selectedBackends: [],
    });

    expect(offered.map((item) => item.id)).toEqual(['mig_1']);
  });

  it('does not count a key it could only take by taking a subscription too', () => {
    // Counting this key would put 「发现 1 个可导入的 API Key」 above a review whose
    // only group is blocked — an offer with nothing to press.
    const offered = offeredImportKeys({
      scan: scanOf(
        row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
        row({ id: 'mig_b', backend: 'claude', vendor: 'anthropic', kind: 'oauth_native' }),
      ),
      selectedBackends: [],
    });

    expect(offered).toEqual([]);
  });

  it('is independent of what is consented to, unlike the batch', () => {
    const scan = scanOf(row({ id: 'mig_1', backend: 'codex', vendor: 'openai' }));

    expect(offeredImportKeys({ scan, selectedBackends: [] })).toHaveLength(1);
    expect(pendingImportRows({ scan, selectedBackends: [] })).toHaveLength(0);
  });
});

describe('defaultSelection', () => {
  it('opens on the rows the server itself proposes, the way the shipped dialog does', () => {
    expect(defaultSelection(scanOf(
      row({ id: 'mig_1', backend: 'codex', vendor: 'openai' }),
      row({ id: 'mig_2', backend: 'opencode', kind: 'opencode_provider', vendor: 'xai' }),
    ))).toEqual(['codex', 'opencode']);
  });

  it('leaves a group the server did not tick alone', () => {
    // `selected: false` is the server declining to propose that row. Ticking it here
    // would be this screen consenting on someone's behalf.
    expect(defaultSelection(scanOf(
      row({ id: 'mig_1', backend: 'codex', vendor: 'openai' }),
      row({ id: 'mig_2', backend: 'opencode', kind: 'opencode_provider', vendor: 'xai', selected: false }),
    ))).toEqual(['codex']);
  });

  it('never opens on a blocked group', () => {
    expect(defaultSelection(scanOf(
      row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
      row({ id: 'mig_b', backend: 'claude', kind: 'oauth_native', proposed_action: 'reauth' }),
    ))).toEqual([]);
  });

  it('never opens on a group setup itself cannot take whole', () => {
    // The server ticked both rows; Settings would open on them. Setup may not, and
    // opening ticked would make the CTA promise a batch it refuses to build.
    expect(defaultSelection(scanOf(
      row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
      row({ id: 'mig_b', backend: 'claude', vendor: 'anthropic', kind: 'oauth_native' }),
    ))).toEqual([]);
  });

  it('has nothing to open on without a scan', () => {
    expect(defaultSelection(null)).toEqual([]);
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
    const before = scanOf(row({ id: 'mig_1', backend: 'codex' }));
    expect(reconcileSelection({
      scan: scanOf(row({ id: 'mig_1', backend: 'codex' })),
      selectedBackends: ['codex'],
    }, before)).toEqual(['codex']);
  });

  it('keeps a consent whose rows came back in a different order', () => {
    // Scan order is the server's, not a promise. Comparing the rows as a set is
    // what stops a reordered rescan from reading as changed credentials and
    // silently un-ticking a card nobody touched.
    const before = scanOf(
      row({ id: 'mig_1', backend: 'codex' }),
      row({ id: 'mig_2', backend: 'codex', vendor: 'openai' }),
    );
    expect(reconcileSelection({
      scan: scanOf(
        row({ id: 'mig_2', backend: 'codex', vendor: 'openai' }),
        row({ id: 'mig_1', backend: 'codex' }),
      ),
      selectedBackends: ['codex'],
    }, before)).toEqual(['codex']);
  });

  it('drops a backend the rescan no longer offers', () => {
    // What this prevents: importing one of two consented backends, rescanning, and
    // leaving the CTA counting a batch the dialog would now build as empty.
    const before = scanOf(
      row({ id: 'mig_1', backend: 'codex' }),
      row({ id: 'mig_2', backend: 'claude', vendor: 'anthropic' }),
    );
    expect(reconcileSelection({
      scan: scanOf(row({ id: 'mig_1', backend: 'codex' })),
      selectedBackends: ['codex', 'claude'],
    }, before)).toEqual(['codex']);
  });

  it('drops a backend that gained a row nobody consented to', () => {
    // The quiet one. A key that appeared under an already-ticked backend would ride
    // into the next batch on a decision made about a different set of credentials.
    const before = scanOf(row({ id: 'mig_1', backend: 'codex' }));
    expect(reconcileSelection({
      scan: scanOf(
        row({ id: 'mig_1', backend: 'codex' }),
        row({ id: 'mig_2', backend: 'codex', vendor: 'openai' }),
      ),
      selectedBackends: ['codex'],
    }, before)).toEqual([]);
  });

  it('drops a backend whose closure grew into one that was never consented to', () => {
    // Half a custody closure is a press that always fails: the server refuses a
    // batch that migrates one backend and leaves a linked one out.
    const before = scanOf(row({ id: 'mig_1', backend: 'codex' }));
    expect(reconcileSelection({
      scan: scanOf(
        row({ id: 'mig_1', backend: 'codex', required_backends: ['codex', 'claude'] }),
        row({ id: 'mig_2', backend: 'claude', vendor: 'anthropic' }),
      ),
      selectedBackends: ['codex'],
    }, before)).toEqual([]);
  });

  it('drops a backend that became blocked between scans', () => {
    const before = scanOf(row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }));
    expect(reconcileSelection({
      scan: scanOf(
        row({ id: 'mig_a', backend: 'claude', vendor: 'anthropic' }),
        row({ id: 'mig_b', backend: 'claude', kind: 'oauth_native', proposed_action: 'reauth' }),
      ),
      selectedBackends: ['claude'],
    }, before)).toEqual([]);
  });

  it('empties the selection when the scan itself is gone', () => {
    expect(reconcileSelection({ scan: null, selectedBackends: ['codex'] }, null)).toEqual([]);
  });
});

const supply = (over: Partial<AgentSupply> & { backend: AgentSupply['backend'] }): AgentSupply => ({
  cli_present: true,
  mode: 'direct',
  menu_kind: 'fixed',
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
    // The snapshot rides along: the resume starts from the state that authorized
    // it rather than reading the same runtime a second time and racing itself.
    const missing = runtime('not_installed');
    expect(gatewayIntent({ ...enabled, runtimeRead: readyRegion(missing) }))
      .toEqual({ kind: 'resume', step: 'install', runtime: missing });
  });

  it('starts an installed but stopped engine even where installation is unsupported', () => {
    // Admission gates fresh installation only. Refusing to START a Hub that is
    // already on the machine would strand a working engine on a technicality.
    const unsupported = runtime('not_started', {
      manifest: { name: 'cliproxyapi', resolution: 'unsupported', version: '1.0.0', source_sha: 'sha', assets: [] },
    });

    expect(gatewayIntent({ ...enabled, runtimeRead: readyRegion(unsupported) }))
      .toEqual({ kind: 'resume', step: 'start', runtime: unsupported });
  });

  it('reports installation as unsupported rather than attempting it', () => {
    const unsupported = runtime('not_installed', {
      manifest: { name: 'cliproxyapi', resolution: 'unsupported', version: '1.0.0', source_sha: 'sha', assets: [] },
    });

    expect(gatewayIntent({ ...enabled, runtimeRead: readyRegion(unsupported) }))
      .toEqual({ kind: 'unsupported' });
  });

  it.each([
    ['still in flight', loadingRegion<RuntimeDependency>()],
    ['already being retried', degradedRegion(runtime('ok'), 'refreshing', true)],
    ['failed with nothing to ask again', unreadRegion<RuntimeDependency>(false)],
    ['failed a second time with nothing to ask again', degradedRegion(runtime('ok'), 'read_failed', false)],
  ] as const)('authorizes nothing, and asks for nothing, on a read %s', (_label, runtimeRead) => {
    // None of these is evidence the engine is absent — acting on one would install
    // over a Hub that is merely unreachable this second — and none of them is
    // something the person can do anything about either, so the card stays quiet.
    expect(gatewayIntent({ ...enabled, runtimeRead })).toEqual({ kind: 'waiting' });
  });

  it.each([
    ['a first read that failed', unreadRegion<RuntimeDependency>()],
    ['a later read that failed', degradedRegion(runtime('ok'), 'read_failed', true)],
  ] as const)('says %s is worth asking again, rather than showing an idle engine', (_label, runtimeRead) => {
    // The distinction `waiting` cannot carry: nothing is coming unless someone asks.
    // Folded into `waiting` this renders as the idle card — the engine looks fine
    // while Continue stays disabled for a reason nothing on screen gives.
    expect(gatewayIntent({ ...enabled, runtimeRead })).toEqual({ kind: 'unreadable' });
  });

  it('holds a failed read behind the gates that have not opened yet', () => {
    // A retry here would re-read a runtime the flow is not admitted to use. The
    // pending capability is the thing to wait for, and it is the shell's to resolve.
    expect(gatewayIntent({
      capability: 'pending',
      gatewayEnabled: null,
      runtimeRead: unreadRegion<RuntimeDependency>(),
    })).toEqual({ kind: 'waiting' });
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

describe('gatewayEvidenceSettled', () => {
  const enabled = { capability: 'enabled', gatewayEnabled: true } as const;

  it.each([
    ['a read in flight', loadingRegion<RuntimeDependency>()],
    ['a re-read over the last value', degradedRegion(runtime('not_started'), 'refreshing', false)],
  ] as const)('is not answered by %s', (_label, runtimeRead) => {
    // The states the shell hands down on its way to an answer. `gatewayIntent` folds
    // all of them to `waiting`, which is the right thing to draw and the wrong thing
    // to spend a pending request on.
    expect(gatewayEvidenceSettled({ ...enabled, runtimeRead })).toBe(false);
  });

  it.each([
    ['capability is still being read', { capability: 'pending', gatewayEnabled: true }],
    ['the configuration has not been read', { capability: 'enabled', gatewayEnabled: null }],
  ] as const)('is not answered while %s', (_label, gate) => {
    expect(gatewayEvidenceSettled({ ...gate, runtimeRead: readyRegion(runtime('not_started')) }))
      .toBe(false);
  });

  it.each([
    ['a read that landed', readyRegion(runtime('not_started'))],
    ['a first read that failed', unreadRegion<RuntimeDependency>()],
    ['a later read that failed', degradedRegion(runtime('ok'), 'read_failed', true)],
    ['a failure nothing can ask again about', unreadRegion<RuntimeDependency>(false)],
  ] as const)('is answered by %s', (_label, runtimeRead) => {
    // Including the failures: 「不知道」 is an answer, and terminal until someone asks
    // again. A mutation left pending on one would fire on an unrelated later read.
    expect(gatewayEvidenceSettled({ ...enabled, runtimeRead })).toBe(true);
  });

  it.each([
    ['capability says so', { capability: 'disabled', gatewayEnabled: true }],
    ['the configuration says so', { capability: 'enabled', gatewayEnabled: false }],
  ] as const)('is answered by a gateway that is off, whatever the read holds, when %s', (_label, gate) => {
    // No runtime read overrides this, so there is nothing left to wait for: the
    // request is over, refused.
    expect(gatewayEvidenceSettled({ ...gate, runtimeRead: loadingRegion<RuntimeDependency>() }))
      .toBe(true);
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
  it('says nothing when nothing is added or chosen', () => {
    expect(providerSummary({ scan: null, sources: [], selected: [], failed: false })).toEqual({ kind: 'none' });
  });

  it('counts every added provider, not just the two on the stage', () => {
    const sources = [
      source({ id: 'src_1', vendor: 'openai' }),
      source({ id: 'src_2', vendor: 'anthropic' }),
      source({ id: 'src_3', vendor: 'xai' }),
    ];

    expect(providerSummary({ scan: null, sources, selected: [], failed: false }))
      .toEqual({ kind: 'added', count: 3, names: ['OpenAI', 'Anthropic', 'xAI'] });
  });

  it('counts one provider once however many keys it has', () => {
    const sources = [
      source({ id: 'src_1', vendor: 'openai' }),
      source({ id: 'src_2', vendor: 'openai', display_name: 'OpenAI (work)' }),
    ];

    expect(providerSummary({ scan: null, sources, selected: [], failed: false }))
      .toMatchObject({ kind: 'added', count: 1 });
  });

  it('prefers what is really there over what is merely consented to', () => {
    const sources = [source({ id: 'src_1', vendor: 'openai' })];
    const scan = scanOf(row({ id: 'mig_1', backend: 'claude', vendor: 'anthropic' }));

    expect(providerSummary({
      scan,
      sources,
      selected: ['claude'],
      failed: false,
    })).toMatchObject({ kind: 'added' });
  });

  it('names the consented cards while nothing has been added yet', () => {
    const scan = scanOf(row({ id: 'mig_1', backend: 'claude', vendor: 'anthropic' }));

    expect(providerSummary({
      scan,
      sources: [],
      selected: ['claude'],
      failed: false,
    })).toEqual({ kind: 'selected', count: 1, names: ['Anthropic'] });
  });

  it('names a consented provider the stage had no room to show', () => {
    // Three detected backends, two slots. Reading the sentence off the slots would
    // silently drop the third from a count the person is about to act on.
    const scan = scanOf(
      row({ id: 'mig_1', backend: 'claude', vendor: 'anthropic' }),
      row({ id: 'mig_2', backend: 'codex', vendor: 'openai' }),
      row({ id: 'mig_3', backend: 'opencode', kind: 'opencode_provider', vendor: 'xai' }),
    );

    expect(providerSummary({
      scan,
      sources: [],
      selected: ['claude', 'codex', 'opencode'],
      failed: false,
    })).toMatchObject({ kind: 'selected', count: 3 });
  });

  it('reports a failure ahead of anything it could otherwise say', () => {
    const sources = [source({ id: 'src_1', vendor: 'openai' })];

    expect(providerSummary({ scan: null, sources, selected: [], failed: true })).toEqual({ kind: 'error' });
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
  // `hasSource` stays a shorthand for the common case — a read that landed, saying
  // this — so the cases below read as what they are about. `supply` is passed whole
  // when what the case is about is the read itself.
  const state = (
    over: Partial<Parameters<typeof providerAction>[0]> & { hasSource?: boolean } = {},
  ) => {
    const { hasSource = false, ...rest } = over;
    return providerAction({
      pendingCount: 0,
      importFailed: false,
      supply: { kind: 'read', hasSource },
      gatewayBusy: false,
      hubAdmitted: true,
      verifying: false,
      ...rest,
    });
  };

  it('offers to add when there is nothing yet', () => {
    expect(state()).toEqual({ kind: 'add', count: 0, blocked: false });
  });

  it('offers to continue once a source exists', () => {
    expect(state({ hasSource: true })).toEqual({ kind: 'continue', count: 0, blocked: false });
  });

  it('keeps saying continue but refuses it while the engine is not serving', () => {
    // The next screen picks a model per assistant out of what the Hub supplies, so
    // arriving there with a stopped engine is arriving at an empty screen. Changing
    // the label instead would move that explanation off the card it belongs to.
    expect(state({ hasSource: true, hubAdmitted: false }))
      .toEqual({ kind: 'continue', count: 0, blocked: true });
    expect(providerSetupAction(state({ hasSource: true, hubAdmitted: false })))
      .toMatchObject({ labelKey: 'onboarding.providers.actionContinue', disabled: true, busy: false });
  });

  it('keeps naming the write and refuses it while the engine is not serving', () => {
    // Both of these write to the Hub — one saves a source, the other hands a batch to
    // the take-over — and a stopped, failed, unsupported or unreadable engine cannot
    // take either. Offering the press anyway is a control that reaches nothing; hiding
    // it would take away the only name for what the person came here to do.
    expect(state({ hubAdmitted: false })).toEqual({ kind: 'add', count: 0, blocked: true });
    expect(state({ hubAdmitted: false, pendingCount: 2 }))
      .toEqual({ kind: 'import', count: 2, blocked: true });
    expect(providerSetupAction(state({ hubAdmitted: false })))
      .toMatchObject({ labelKey: 'onboarding.providers.actionAdd', disabled: true, busy: false });
  });

  it('puts a pending take-over ahead of continuing, and carries its count', () => {
    expect(state({ hasSource: true, pendingCount: 3 }))
      .toEqual({ kind: 'import', count: 3, blocked: false });
  });

  it('changes what it offers after a failed batch without losing the count', () => {
    expect(state({ pendingCount: 2, importFailed: true }))
      .toEqual({ kind: 'retryImport', count: 2, blocked: false });
  });

  it('reports the engine coming up ahead of everything else', () => {
    expect(state({ gatewayBusy: true, pendingCount: 3, hasSource: true })).toEqual({ kind: 'connecting', count: 0 });
  });

  it('reports a connection being read back ahead of what it would unlock', () => {
    expect(state({ verifying: true, hasSource: true })).toEqual({ kind: 'checking', count: 0 });
  });

  it('says it is still looking rather than offering to add against an unread inventory', () => {
    // 「添加」 here is the same button on a machine that already has credentials as
    // on one that has none, which is how a second copy of an existing key gets
    // written before the first read even lands.
    expect(state({ supply: { kind: 'reading' } })).toEqual({ kind: 'checking', count: 0 });
  });

  it('offers to ask again when the inventory could not be read', () => {
    // Not 「添加」: nothing is known about what is there. Not silence either — the
    // contract keeps Retry reachable on every failed supply read.
    expect(state({ supply: { kind: 'unreadable' } })).toEqual({ kind: 'retrySupply', count: 0 });
    expect(providerSetupAction(state({ supply: { kind: 'unreadable' } })))
      // No arrow: the press stays on this screen.
      .toMatchObject({ labelKey: 'common.retry', disabled: false, busy: false, icon: 'none' });
  });

  it('still takes over a key a failed inventory read knows nothing about', () => {
    // The scan answered even though the source list did not, and taking over what
    // it found does not depend on knowing what else is already there.
    expect(state({ supply: { kind: 'unreadable' }, pendingCount: 2 }))
      .toEqual({ kind: 'import', count: 2, blocked: false });
    expect(state({ supply: { kind: 'reading' }, pendingCount: 2 }))
      .toEqual({ kind: 'import', count: 2, blocked: false });
  });
});

describe('providerSetupAction', () => {
  // The count travels with the label that needs it and with nothing else: a family
  // does not resolve without one, and a plain leaf has nothing to interpolate. The
  // only states carrying a count are the two `providerAction` builds from a positive
  // pending count, so this is the same pairing C2's type requires.
  it('carries the count with a counted label and nothing with a plain one', () => {
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

  // Which table a label sits in is what decides whether the count travels with it,
  // and the compiler cannot check that in the e2e project, where this module is
  // compiled without the bundle augmentation. So both halves answer to the bundles
  // themselves: a counted label must be a complete family and must NOT resolve on
  // its own (a plain leaf given a count is a different string than the one C1
  // approved), and a plain label must resolve exactly as it stands.
  it('names only labels that have shipped, in the form its table claims, in both locales', () => {
    const leaf = (bundle: unknown, key: string): unknown => key
      .split('.')
      .reduce<unknown>(
        (node, part) => (node && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined),
        bundle,
      );
    const resolves = (bundle: unknown, key: string): boolean => typeof leaf(bundle, key) === 'string';

    for (const [locale, bundle] of [['en', en], ['zh', zh]] as const) {
      for (const key of Object.values(COUNTED_LABEL)) {
        for (const suffix of ['_one', '_other']) {
          expect(resolves(bundle, `${key}${suffix}`), `${key}${suffix} in ${locale}`).toBe(true);
        }
        expect(leaf(bundle, key), `${key} in ${locale} is a family, not a leaf`).toBeUndefined();
      }
      for (const key of Object.values(PLAIN_LABEL)) {
        expect(resolves(bundle, key), `${key} in ${locale}`).toBe(true);
      }
    }
  });
});
