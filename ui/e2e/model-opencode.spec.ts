// OpenCode's model list: the id a row is saved under, and the one field that
// asks which API Avibe answers that model on.
//
// No scenario ID, for the reason model-list.spec.ts states: the e2e plan has no
// family for a backend's model list, so these are named by the property they
// assert. The properties are the two the v3/v4 identifier work is about —
// docs/plans/backend-model-catalogs.md §"v3 — OpenCode" C8/C9 and Acceptance 6:
//
//   - an OpenCode row is stored under the BARE id it was offered or typed as,
//     with nothing prefixed onto it on the way in;
//   - `native_protocol` is carried by the row and named by ONE surface — the
//     editor's last field. No list, picker, row, chip or card says it.
//
// SAVED, unlike model-list.spec.ts, which cancels because the list belongs to
// the instance. It has to be: "the row is stored with the API the user chose"
// is a claim about what the server holds, and a draft the dialog is still
// carrying would only prove the dialog can carry it. What that costs is a real
// restoration, so the list is snapshotted and put back by the fixture below —
// through `restoreCatalogModels`, which re-reads its own baseline and throws if
// the list it left is still there.
import type { Locator } from '@playwright/test';

import type { Agent, CatalogModel } from './support/api';
import { hub as copy } from './support/copy';
import { E2E_SOURCE_PREFIX } from './support/env';
import { expectVisibleWithout, requireModelHub, requireRuntimeRunning } from './support/fixtures';
import { expect, test as gatewayTest } from './support/gateway';
import { labelledButton, pickerRowId } from './support/hub';
import { restoreNativeSources } from './support/restore';

const BACKEND = 'opencode';

/** The two answers the field offers, as the row carries them. */
const PROTOCOLS = ['openai_responses', 'anthropic'] as const;
type Protocol = (typeof PROTOCOLS)[number];

/** The words a user reads for one of them. */
const protocolLabel = (value: Protocol): string => copy(`gateway.modelEditor.nativeProtocol.${value}`);

const literal = (word: string): string => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * Every word the protocol is spelled with — the value `openai_responses`, the
 * two labels the editor offers, and the two overlay providers OpenCode is given
 * — as one pattern a list surface must match none of.
 *
 * `anthropic` alone is deliberately NOT here, even though it is one of the two
 * values. It is also a provider name, a vendor id, and a substring of half the
 * model names a real instance lists, so its presence on a list says nothing and
 * its absence would be asserted by accident. The words below have no innocent
 * reading: an id, a chip or a card that contains one of them is the mechanism
 * leaking, which is exactly what Acceptance 6 forbids.
 */
const PROTOCOL_WORDS = new RegExp(
  [
    'openai_responses',
    'avibe-openai',
    'avibe-anthropic',
    protocolLabel('openai_responses'),
    protocolLabel('anthropic'),
  ].map(literal).join('|'),
);

/** Which of the two the field is on, named by the value the row carries rather
 *  than by the words on the button. */
const answeredProtocol = async (field: Locator): Promise<Protocol> => {
  for (const value of PROTOCOLS) {
    const radio = field.getByRole('radio', { name: protocolLabel(value), exact: true });
    if ((await radio.getAttribute('aria-checked')) === 'true') return value;
  }
  throw new Error('The API field is on neither of the two protocols it offers.');
};

const otherProtocol = (value: Protocol): Protocol =>
  PROTOCOLS.find((option) => option !== value)!;

export type OpencodeCatalog = {
  backend: string;
  /** The list as it was found. Restored by the fixture, whatever the test did. */
  baseline: CatalogModel[];
};

/**
 * OpenCode in Gateway mode, with its model list snapshotted for restore.
 *
 * Its own fixture rather than the shared `gateway` one, which picks whichever
 * installed backend already has a route: these two properties are OpenCode's
 * alone — `claude` and `codex` have neither an open menu nor a protocol to
 * choose — so a spec handed `claude` would pass by asserting nothing.
 *
 * "Manage models" is on the card only in Gateway mode (`AgentCard.tsx`), so the
 * mode is arranged rather than assumed, and put back only if this fixture is
 * what changed it.
 */
const test = gatewayTest.extend<{ opencode: OpencodeCatalog }>({
  opencode: async ({ api }, provide, testInfo) => {
    // Its own preconditions, for the reason the `gateway` fixture states: a
    // fixture is set up before the suite's `beforeEach` guards run, so a
    // disabled capability would reach the reads below as a thrown 404 rather
    // than as the skip it is.
    await requireModelHub(api);
    await requireRuntimeRunning(api);
    const agent = (await api.agents()).find((entry: Agent) => entry.backend === BACKEND);
    testInfo.skip(
      !agent?.cli_present,
      `${BACKEND} is not installed on this instance, so it has no model list to manage and no `
      + 'card to open one from.',
    );

    let switched = false;
    let sourcesBeforeSwitch: Set<string> | null = null;
    let baseline: CatalogModel[] | null = null;
    try {
      if (agent!.mode !== 'hub') {
        // Same snapshot the `gateway` fixture takes, for the same reason: on a
        // machine whose CLI holds a native login the Direct→Gateway switch
        // APPENDS a `native_cli` source, and it carries no suite prefix for the
        // sweep to find.
        sourcesBeforeSwitch = new Set((await api.sources()).map((source) => source.id));
        // Marked before the await, not after it succeeds: the server commits
        // the mode before the response leaves, so a lost response would skip
        // the restore on exactly the path that needs it.
        switched = true;
        await api.setAgentMode(BACKEND, 'hub');
      }

      baseline = await api.catalogModels(BACKEND);
      testInfo.skip(
        baseline === null,
        `This instance's ${BACKEND} payload carries no \`catalog_models\`, so it predates the saved `
        + 'model list these specs read back. That projection arrives with the compose-from-providers '
        + 'server.',
      );

      await provide({ backend: BACKEND, baseline: baseline! });
    } finally {
      // The list first and the mode second, each in its own block: they are
      // independent promises, and a mode PUT that throws must not be the reason
      // the suite's rows stay on the instance.
      try {
        if (baseline) await api.restoreCatalogModels(BACKEND, baseline);
      } finally {
        try {
          if (switched) await api.setAgentMode(BACKEND, 'direct');
        } finally {
          if (sourcesBeforeSwitch) await restoreNativeSources(api, sourcesBeforeSwitch);
        }
      }
    }
  },
});

test.describe('OpenCode models · bare ids, and the one field that names the API', () => {
  test('takes a typed ID verbatim and stores it on the API the editor asked for', async ({ api, hub, opencode }) => {
    await hub.goto();
    await hub.manageModelsButton(opencode.backend).click();

    const catalog = hub.catalogDialog;
    await expect(catalog).toBeVisible();
    // Enabled is this dialog's own "ready": it reads the saved list first, and
    // counting rows before that would count a list still arriving.
    const addModels = labelledButton(catalog, copy('gateway.catalog.add'));
    await expect(addModels).toBeEnabled();
    const before = await hub.catalogRows.count();

    await addModels.click();
    const picker = hub.pickerDialog;
    await expect(picker).toBeVisible();
    await picker.getByRole('button', { name: copy('gateway.picker.custom'), exact: true }).click();

    const editor = hub.modelEditorDialog;
    await expect(editor).toBeVisible();
    // An id models.dev has never heard of, which is the case the typeahead's
    // last row exists for — and the case the default below is chosen for.
    const typed = `${E2E_SOURCE_PREFIX}model-${Date.now()}`;
    await hub.modelIdField.fill(typed);
    await hub.literalIdOption.click();
    // Bare: the id the row will carry is the string that was typed. Nothing is
    // prefixed onto it, by the backend's scheme or by the protocol.
    expect(await hub.modelIdField.inputValue()).toBe(typed);

    const field = hub.nativeProtocolField;
    await expect(field).toBeVisible();
    // Nobody published this id, so nothing can be derived from it; the form
    // opens on the answer a self-hosted or proxied endpoint almost always is,
    // rather than refusing to save without one.
    expect(await answeredProtocol(field)).toBe('openai_responses');

    // And the answer is the user's. An endpoint serving this model over the
    // Messages API is the whole reason the field is asked at all, so the other
    // one has to survive the save.
    await field.getByRole('radio', { name: protocolLabel('anthropic'), exact: true }).click();
    await labelledButton(editor, copy('gateway.modelEditor.add')).click();
    await expect(editor).toHaveCount(0);

    await expect(hub.catalogRow(typed)).toBeVisible();
    await expect(hub.catalogRows).toHaveCount(before + 1);
    // The list shows the row and says nothing about how it is spoken to.
    await expectVisibleWithout(catalog, PROTOCOL_WORDS);

    await labelledButton(catalog, copy('gateway.catalog.save')).click();
    await expect(catalog).toHaveCount(0);

    // What the SERVER holds, not what the dialog was holding: the row is stored
    // under the bare id, carrying the API that was chosen for it.
    const stored = (await api.catalogModels(opencode.backend)) ?? [];
    const row = stored.find((model) => model.id === typed);
    expect(
      row,
      `The saved ${opencode.backend} list has no row for ${typed}; it holds ${stored.map((model) => model.id).join(', ')}.`,
    ).toBeDefined();
    expect(row?.native_protocol).toBe('anthropic');

    // Stored, and still not shown: the card is the surface the row reaches once
    // the dialog is gone.
    await expectVisibleWithout(hub.agentCard(opencode.backend), PROTOCOL_WORDS);
  });

  test('saves a picked model under the id it was offered as, on the API it was picked with', async ({ api, hub, opencode }) => {
    test.skip(
      !(await api.servesModelCandidates(opencode.backend)),
      'This instance does not serve the models a backend can be given '
        + '(GET /api/models/agents/<backend>/models/candidates), so the picker has no built-in group and no '
        + 'provider group to offer. That read arrives with the compose-from-providers server, and the '
        + 'integration pass on a server that has it is the layer that can execute this scenario.',
    );

    await hub.goto();
    await hub.manageModelsButton(opencode.backend).click();

    const catalog = hub.catalogDialog;
    const addModels = labelledButton(catalog, copy('gateway.catalog.add'));
    await expect(addModels).toBeEnabled();
    const before = await hub.catalogRows.count();

    await addModels.click();
    const picker = hub.pickerDialog;
    await expect(picker).toBeVisible();
    // Settled is either an offer or the answer that there is nothing to offer;
    // waiting on a row alone would spend the whole timeout on the second one.
    const offers = hub.pickerOffers;
    await expect(offers.first().or(picker.getByText(copy('gateway.picker.noMatch')))).toBeVisible();

    const available = await offers.count();
    test.skip(
      available === 0,
      `Every model this instance can offer ${opencode.backend} is already in its list, so the picker has `
        + 'nothing left to add.',
    );

    // The offer is a list surface, and the protocol every candidate carries is
    // decided by the time it is offered — so this is the first place it could
    // leak, and the first place it may not.
    await expectVisibleWithout(picker, PROTOCOL_WORDS);

    const offered = await pickerRowId(offers.first());
    await offers.first().click();
    await labelledButton(picker, copy('gateway.picker.confirm', { count: 1 })).click();
    await expect(picker).toHaveCount(0);

    // The same string in the list as on the offer: an id is not rewritten by
    // being added.
    await expect(hub.catalogRow(offered)).toBeVisible();
    await expect(hub.catalogRows).toHaveCount(before + 1);
    await expectVisibleWithout(catalog, PROTOCOL_WORDS);

    // The row's own actions are named after what the row DISPLAYS, which is its
    // name when it has one and its id when it does not — so the label is read
    // off the row rather than assumed to be the id.
    const row = hub.catalogRow(offered);
    const shownAs = (await row.locator('.model-hub-catalog-name').innerText()).trim();
    await row.getByRole('button', { name: copy('gateway.catalog.edit', { model: shownAs }), exact: true }).click();

    const editor = hub.modelEditorDialog;
    await expect(editor).toBeVisible();
    const field = hub.nativeProtocolField;
    await expect(field).toBeVisible();
    // A picked model arrives already answered: models.dev knows whose model it
    // is, and the vendor family is what the protocol follows from. Which of the
    // two it landed on depends on the model this instance happened to offer, so
    // what is asserted is that it landed on one of them — and then that the
    // OTHER one survives being chosen, which no default can fake.
    const proposed = await answeredProtocol(field);
    const chosen = otherProtocol(proposed);
    await field.getByRole('radio', { name: protocolLabel(chosen), exact: true }).click();
    await labelledButton(editor, copy('gateway.modelEditor.apply')).click();
    await expect(editor).toHaveCount(0);

    await labelledButton(catalog, copy('gateway.catalog.save')).click();
    await expect(catalog).toHaveCount(0);

    const stored = (await api.catalogModels(opencode.backend)) ?? [];
    const saved = stored.find((model) => model.id === offered);
    expect(
      saved,
      `The saved ${opencode.backend} list has no row for the offered id ${offered}; it holds `
        + `${stored.map((model) => model.id).join(', ')}. An id that changed between the offer and the `
        + 'row is the prefixing this scheme removed.',
    ).toBeDefined();
    expect(saved?.native_protocol).toBe(chosen);

    await expectVisibleWithout(hub.agentCard(opencode.backend), PROTOCOL_WORDS);
  });
});
