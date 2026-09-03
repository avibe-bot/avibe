// Page object for `/settings/models`.
//
// Locator policy, since it is the whole reason this file exists: prefer ARIA
// role + accessible name (the name always read through `copy.ts`, never pasted),
// then the product's own `data-*` hooks (`data-source-id`, `data-route-backend`,
// `data-manage-kind`), and only then a `model-hub-*` class — which is used to
// NAME A SURFACE, never to assert one. Two dialogs can be mounted at once (a
// panel and the guard it raises), and `getByRole('dialog')` cannot tell them
// apart; the class says which one, and every assertion inside it still goes
// through role and copy.
//
// Every attribute selector is built by `attr` below, never by interpolation:
// some of the values are the operator's, not ours.
//
// No `data-testid` is added anywhere: this lane makes zero product-code changes.
import type { Locator, Page } from '@playwright/test';

import { hub } from './copy';

/**
 * One attribute selector, with the value escaped as the CSS string it is.
 *
 * The values here are not all ours. A route row is keyed by a MODEL ID, and for
 * an `open` menu that id is whatever the operator ticked — `ModelHubMenuConfig`
 * takes arbitrary non-credential strings. Pasted into a quoted attribute
 * selector, a `"` closes the string early and the locator either throws as
 * invalid syntax or, worse, still parses and matches something else: a green
 * assertion about a row that is not the row.
 *
 * Escaping per CSS Syntax §4.3.7 — backslash and quote by backslash, control
 * characters by hex — rather than rejecting those ids, because refusing to look
 * at a model the product accepted would be the suite choosing what the product
 * is allowed to contain.
 *
 * A per-character walk rather than a character-class replace: the class it
 * would need is the control characters themselves, and a regex naming them is
 * the one shape `no-control-regex` exists to stop. Suppressing that rule to
 * write the escape would leave the codebase carrying a suppression whose
 * justification is one line long and whose reader has to reconstruct it.
 */
const attr = (name: string, value: string): string => {
  const escaped = Array.from(value, (ch) => {
    const code = ch.codePointAt(0)!;
    if (code <= 0x1f || code === 0x7f) return `\\${code.toString(16)} `;
    return ch === '"' || ch === '\\' ? `\\${ch}` : ch;
  }).join('');
  return `[${name}="${escaped}"]`;
};

export class ModelHubPage {
  readonly page: Page;

  constructor(page: Page) {
    this.page = page;
  }

  async goto(): Promise<void> {
    await this.page.goto('/settings/models');
  }

  /** The page shell. Present whenever the capability gate let the route render. */
  get shell(): Locator {
    return this.page.locator('.model-hub-shell');
  }

  get header(): Locator {
    return this.page.locator('.model-hub-shell-head');
  }

  /** The gateway on/off switch. Its accessible name IS the product's
   *  explanation of why it is disabled, so specs read the name, not a tooltip. */
  get runtimeToggle(): Locator {
    return this.header.getByRole('switch');
  }

  async runtimeToggleLabel(): Promise<string> {
    return (await this.runtimeToggle.getAttribute('aria-label')) ?? '';
  }

  get runtimePill(): Locator {
    return this.header.locator('.model-hub-runtime-pill');
  }

  /** The body shown instead of the hub when the gateway is not configurable. */
  get closedState(): Locator {
    return this.page.locator('.model-hub-runtime-closed');
  }

  tab(id: 'hub' | 'usage' | 'logs'): Locator {
    return this.page.getByRole('tab', { name: hub(`shell.tab.${id}`) });
  }

  async openTab(id: 'hub' | 'usage' | 'logs'): Promise<void> {
    await this.tab(id).click();
  }

  /** The home a machine with no sources and every backend on Direct lands on,
   *  instead of an empty gateway. */
  get directHome(): Locator {
    return this.page.locator('.model-hub-direct');
  }

  // --- Upstream sources -----------------------------------------------------

  get addApiKeyButton(): Locator {
    return this.page.getByRole('button', { name: hub('upstream.addApiKey') });
  }

  get addSubscriptionButton(): Locator {
    return this.page.getByRole('button', { name: hub('upstream.addSubscription') });
  }

  sourceRow(sourceId: string): Locator {
    return this.page.locator(attr('data-source-id', sourceId));
  }

  sourceRowByName(displayName: string): Locator {
    return this.page.locator('[data-source-id]').filter({ hasText: displayName });
  }

  async openSource(sourceId: string): Promise<void> {
    await this.sourceRow(sourceId).click();
  }

  // --- Gateway routes -------------------------------------------------------

  agentCard(backend: string): Locator {
    return this.page.locator(attr('data-agent-backend', backend));
  }

  routeRow(backend: string, model: string): Locator {
    return this.page.locator(attr('data-route-backend', backend) + attr('data-route-model', model));
  }

  /** The card collapses its model list, so a spec that just needs *a* route
   *  takes the first row the card actually shows rather than naming a model
   *  that may be hidden behind "N more models". */
  firstRouteRow(backend: string): Locator {
    return this.agentCard(backend).locator('[data-route-model]').first();
  }

  adjustPriorityButton(backend: string): Locator {
    return this.agentCard(backend).getByRole('button', { name: hub('gateway.sourceOrder') });
  }

  /** The card's own "Manage models", scoped to the card head: a gateway backend
   *  whose list is empty offers the same action a second time from the body,
   *  and either one alone is the wrong thing for a name to match twice. */
  manageModelsButton(backend: string): Locator {
    return this.agentCard(backend)
      .locator('.model-hub-agent-head-action')
      .filter({ hasText: hub('gateway.manageModels') });
  }

  // --- Dialogs --------------------------------------------------------------

  /** Add / replace API key. Both modes render the same surface. */
  get addKeyDialog(): Locator {
    return this.page.locator('.model-hub-add-key-dialog');
  }

  get sourceDetailDialog(): Locator {
    return this.page.locator('.model-hub-source-dialog');
  }

  /** The `…` menu on the open source panel, and the two actions behind it. */
  manageMenuTrigger(sourceName: string): Locator {
    return this.sourceDetailDialog.getByRole('button', {
      name: hub('sourceDetail.manage.label', { source: sourceName }),
      exact: true,
    });
  }

  manageItem(kind: 'edit_source' | 'delete_source'): Locator {
    return this.page.locator(attr('data-manage-kind', kind));
  }

  /** One row of the source's model table. The rows carry no id attribute, but
   *  the id cell carries `title={model.id}` — an exact match on that is what
   *  keeps `claude-3-5` from also selecting `claude-3-5-haiku`. */
  modelRow(modelId: string): Locator {
    return this.sourceDetailDialog
      .locator('.model-hub-source-table-row')
      .filter({ has: this.page.getByTitle(modelId, { exact: true }) });
  }

  /** The pending "add a model by hand" row, before it is committed. */
  get manualDraftRow(): Locator {
    return this.sourceDetailDialog.locator('[data-manual-model-draft]');
  }

  // --- Reasoning tiers ------------------------------------------------------

  /**
   * The tiers cell of one model row.
   *
   * One name for two renderings on purpose. An editable cell is a button that
   * opens the editor; a cell whose tiers the server declared is a plain div with
   * no way in. A spec asserting "there is no way in here" has to be able to name
   * the cell without already knowing which of the two it got.
   */
  tierCell(modelId: string): Locator {
    return this.modelRow(modelId).locator('.model-hub-source-tier-cell');
  }

  /** The tiers the row carries right now, in the order the product lists them. */
  tierChips(modelId: string): Locator {
    return this.modelRow(modelId).locator('.model-hub-source-tier-chip');
  }

  /** The ghost tiers an open editor proposes — the protocol's vocabulary, minus
   *  whatever the model already has. */
  tierSuggestions(modelId: string): Locator {
    return this.modelRow(modelId).locator('.model-hub-source-tier-suggest');
  }

  /**
   * The cell of a model whose tiers the SERVER declared, named by the rung that
   * declared them.
   *
   * The attribute is absent on an editable row rather than carrying some other
   * value, which is what makes `toHaveCount(0)` here a real assertion instead of
   * a spelling of "the rung is something else".
   */
  managedTierCell(modelId: string, rung: 'upstream' | 'catalog'): Locator {
    return this.modelRow(modelId).locator(attr('data-tier-provenance', rung));
  }

  /**
   * The note a tier write that did not land leaves on its row.
   *
   * Named by kind rather than found by looking for a retry button: `retryable`
   * and `managed` are two verdicts about whether the write could ever succeed,
   * and the button is only one of the things that follows from that.
   */
  tierFailure(modelId: string, kind: 'managed' | 'retryable'): Locator {
    return this.modelRow(modelId).locator(attr('data-tier-failure', kind));
  }

  /**
   * The "+ Add tier" / "+ Tier" pill. Present on an editable resting row (even
   * when CSS hides it until hover) and absent on a locked one, which is the
   * distinction a lock assertion needs — a disabled pill would still be a door.
   */
  tierAddAffordance(modelId: string): Locator {
    return this.modelRow(modelId).locator('.model-hub-source-tier-add');
  }

  /** The free-text field an open editor types a new tier into. */
  tierInput(modelId: string): Locator {
    return this.modelRow(modelId).getByPlaceholder(hub('sourceDetail.tiers.inputHint'), { exact: true });
  }

  /**
   * The confirm-before-destroy dialog raised from the source panel.
   *
   * The edit dialog shares this class — same surface, same chrome — so a spec
   * that means one of them says which by title. `guardDialog` alone is right
   * only where at most one can be mounted.
   */
  get guardDialog(): Locator {
    return this.page.locator('.model-hub-guard-dialog');
  }

  dialogTitled(title: string): Locator {
    return this.page.locator('.model-hub-guard-dialog').filter({ hasText: title });
  }

  /** The after-the-fact report of a committed source mutation. It shares the
   *  guard's chrome — deliberately, since it restates the same facts — but it
   *  carries the action it is reporting, which is what tells them apart. */
  mutationReport(action: 'edit' | 'delete'): Locator {
    return this.page.locator(attr('data-source-mutation-report', action));
  }

  get routeDialog(): Locator {
    return this.page.locator('.model-hub-route-dialog');
  }

  get orderDrawer(): Locator {
    return this.page.locator('.model-hub-order-drawer');
  }

  /** Install / adopt the gateway component. It shares the adopt dialog surface
   *  with the subscription flow, so the class alone is not enough to name it —
   *  the title is what distinguishes the two. */
  get installDialog(): Locator {
    return this.page.locator('.model-hub-adopt-dialog').filter({ hasText: hub('install.title') });
  }

  // --- The backend's model list ---------------------------------------------

  /**
   * "Manage models" itself. The picker is built on the same surface — same
   * chrome, same search, same footer — and both can be mounted at once, which
   * is the whole point of the flow, so the class alone names two dialogs and
   * the list is the one that is not the picker.
   */
  get catalogDialog(): Locator {
    return this.page.locator('.model-hub-catalog-dialog:not(.model-hub-picker)');
  }

  get pickerDialog(): Locator {
    return this.page.locator('.model-hub-picker');
  }

  get modelEditorDialog(): Locator {
    return this.page.locator('.model-hub-model-editor');
  }

  get catalogRows(): Locator {
    return this.catalogDialog.locator('.model-hub-catalog-row');
  }

  /** One row of the list, named by the id the row shows. Exact, because the
   *  list is where `claude-3-5` and `claude-3-5-haiku` sit next to each other,
   *  and a row with no display name shows its id as the name instead — so the
   *  text is asked of the row rather than of either span. */
  catalogRow(modelId: string): Locator {
    return this.catalogRows.filter({ has: this.page.getByText(modelId, { exact: true }) });
  }

  /** The candidates the picker is offering right now: a row for an id already
   *  in the list is rendered checked and disabled, and adding it is not
   *  something a user can do or a spec can ask for. */
  get pickerOffers(): Locator {
    return this.pickerDialog.locator('.model-hub-picker-row:not(:disabled)');
  }

  /** The editor's first field, which is also its models.dev search box. */
  get modelIdField(): Locator {
    return this.modelEditorDialog.getByLabel(hub('gateway.modelEditor.id.label'), { exact: true });
  }

  /** The last row of the editor's typeahead: take the query as the id. It is
   *  present in every open state, including "searching" and "unavailable", and
   *  it names the id it will actually create — which on a backend with an
   *  identifier scheme of its own is not the query as typed. */
  get literalIdOption(): Locator {
    return this.modelEditorDialog.locator('.model-hub-model-match--literal');
  }
}

/** The model id a picker row names, wherever that row put it: a candidate with
 *  a display name shows the id beside the name, and one without shows the id AS
 *  the name. */
export const pickerRowId = async (row: Locator): Promise<string> => {
  const beside = row.locator('.model-hub-picker-id');
  const asName = row.locator('.model-hub-picker-name--id');
  const text = (await beside.count()) > 0 ? await beside.innerText() : await asName.innerText();
  return text.trim();
};

/**
 * The button a user would READ as `name`, inside a surface that also has an
 * icon-only close carrying the same accessible name.
 *
 * Every Model Hub surface with a footer does this: the header X takes the
 * footer action's own label as its `aria-label` — `Cancel` on the route dialog
 * and the priority drawer, `Done` on the mutation report. That is right for a
 * screen reader, since the X genuinely does perform that action, and it means a
 * name alone matches two elements, which Playwright refuses in strict mode. The
 * visible one is the one whose own text is the label; the X has none.
 */
export const labelledButton = (scope: Locator, name: string): Locator =>
  scope.getByRole('button', { name, exact: true }).filter({ hasText: name });

/**
 * Fills the add-API-key form. `name` is optional in the product and optional
 * here for the same reason: a spec that does not care about the label should
 * not have to invent one.
 */
export const fillApiKeyForm = async (
  dialog: Locator,
  values: { name?: string; baseUrl: string; apiKey: string; protocol?: string },
): Promise<void> => {
  if (values.name !== undefined) {
    await dialog.getByLabel(hub('addKey.field.name'), { exact: true }).fill(values.name);
  }
  if (values.protocol) {
    await dialog.getByRole('button', { name: values.protocol, exact: true }).click();
  }
  await dialog.getByLabel(hub('addKey.field.baseUrl'), { exact: true }).fill(values.baseUrl);
  await dialog.getByLabel(hub('addKey.field.apiKey'), { exact: true }).fill(values.apiKey);
};
