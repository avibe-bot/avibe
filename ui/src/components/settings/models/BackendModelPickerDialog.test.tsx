// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { ChosenCandidate } from './backendCatalog';
import { BackendModelPickerDialog } from './BackendModelPickerDialog';
import { modelsApi } from './modelsApi';
import type { BackendModelCandidates, ModelCandidate } from './types';

const candidate = (id: string, overrides: Partial<ModelCandidate> = {}): ModelCandidate => ({
  id,
  display_name: null,
  reasoning_efforts: [],
  suppliers: [],
  origin: 'provider',
  ...overrides,
});

const read = (groups: Partial<BackendModelCandidates> = {}): BackendModelCandidates => ({
  builtin: groups.builtin ?? [],
  providers: groups.providers ?? [],
  in_list: groups.in_list ?? [],
});

const answers = (groups: Partial<BackendModelCandidates> = {}) =>
  vi.spyOn(modelsApi, 'getAgentModelCandidates').mockResolvedValue(read(groups));

const renderPicker = (overrides: Partial<React.ComponentProps<typeof BackendModelPickerDialog>> = {}) => {
  const onCancel = vi.fn();
  const onAdd = vi.fn();
  const onCustom = vi.fn();
  render(
    <I18nextProvider i18n={i18n}>
      <BackendModelPickerDialog
        open
        backend="claude"
        listedIds={new Set()}
        onCancel={onCancel}
        onAdd={onAdd}
        onCustom={onCustom}
        {...overrides}
      />
    </I18nextProvider>,
  );
  return { onCancel, onAdd, onCustom };
};

const group = (name: string) => within(screen.getByRole('group', { name }));
const BUILT_IN = 'Claude Code built-in';
const PROVIDERS = 'From your providers';
const LISTED = 'Already in the list';
const search = () => screen.getByLabelText('Search models or providers');
// The confirmation, whether it is counting picks or naming the action it would
// perform. `Add custom model…` is a different offer and must not answer to this.
const confirm = () => screen.getByRole('button', { name: /^Add (models|\d+ models?)$/ }) as HTMLButtonElement;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('BackendModelPickerDialog', () => {
  it('shows each group as the server served it, and every row as what it can say', async () => {
    answers({
      builtin: [candidate('gpt-6', { origin: 'builtin' }), candidate('gpt-6-mini', { origin: 'builtin' })],
      providers: [candidate('glm-5.2', {
        display_name: 'GLM 5.2',
        suppliers: [
          { source_id: 'src_a', source_name: 'Primary relay', model_id: 'glm-5.2-air' },
          { source_id: 'src_b', source_name: 'Backup relay', model_id: 'glm-5.2' },
        ],
      })],
    });
    renderPicker();

    expect(await screen.findByRole('heading', { name: 'Add Claude Code models' })).toBeTruthy();
    // A built-in row has nothing but an id, so the id is its label rather than a
    // sub-line under an empty one.
    expect(group(BUILT_IN).getAllByRole('checkbox').map((row) => row.textContent))
      .toEqual(['gpt-6', 'gpt-6-mini']);
    expect(group(BUILT_IN).getByText('2')).toBeTruthy();
    // A provider row names the model, its id, and — in the order the server gave
    // them — who can serve it. That order is the route's own preference, so it is
    // not the client's to sort.
    expect(group(PROVIDERS).getByRole('checkbox').textContent).toBe('GLM 5.2glm-5.2Primary relayBackup relay');
    expect(screen.queryByRole('group', { name: LISTED })).toBeNull();
  });

  it('keeps a pick through the query that hid it, and adds in the order offered', async () => {
    const user = userEvent.setup();
    answers({
      builtin: [candidate('gpt-6', { origin: 'builtin' })],
      providers: [candidate('glm-5.2', { display_name: 'GLM 5.2' })],
    });
    const { onAdd } = renderPicker();

    // Picked in the reverse of the offered order, and through a search that
    // leaves only one of them on screen.
    await user.click(await screen.findByRole('checkbox', { name: /GLM 5\.2/ }));
    await user.type(search(), 'gpt');
    expect(screen.queryByRole('checkbox', { name: /GLM 5\.2/ })).toBeNull();
    await user.click(screen.getByRole('checkbox', { name: 'gpt-6' }));
    await user.clear(search());

    // A query narrows what is shown, never what is chosen: the pick the search
    // hid is still a pick, and the confirmation still counts it.
    expect(screen.getByRole('checkbox', { name: /GLM 5\.2/ }).getAttribute('aria-checked')).toBe('true');
    await user.click(screen.getByRole('button', { name: 'Add 2 models' }));

    expect(onAdd).toHaveBeenCalledTimes(1);
    // Group order, not click order: additions land where the picker offered
    // them, so the list the user gets back reads the way the one they picked from
    // did.
    const chosen: ChosenCandidate[] = onAdd.mock.calls[0][0];
    expect(chosen.map((entry) => entry.candidate.id)).toEqual(['gpt-6', 'glm-5.2']);
    // The property behind every one of them, whatever was picked and in whatever
    // order: what a pick promises is the projection of the suppliers its own row
    // displayed. Not a fixture of expected pairs — derived from the candidate the
    // object carries, so the two can never describe different models.
    for (const entry of chosen) {
      expect(entry.expected_suppliers).toEqual(
        entry.candidate.suppliers.map(({ source_id, model_id }) => ({ source_id, model_id })),
      );
    }
  });

  it('refuses to confirm nothing, and names what it would do instead', async () => {
    const user = userEvent.setup();
    answers({ builtin: [candidate('gpt-6', { origin: 'builtin' })] });
    const { onAdd } = renderPicker();

    await waitFor(() => expect(confirm().textContent).toBe('Add models'));
    expect(confirm().disabled).toBe(true);

    await user.click(screen.getByRole('checkbox', { name: 'gpt-6' }));
    expect(confirm().textContent).toBe('Add 1 model');
    expect(confirm().disabled).toBe(false);

    // A pick is a toggle, so the way back out is the way in.
    await user.click(screen.getByRole('checkbox', { name: 'gpt-6' }));
    expect(confirm().textContent).toBe('Add models');
    expect(confirm().disabled).toBe(true);
    expect(onAdd).not.toHaveBeenCalled();
  });

  it('finds an already-added model by its supplier and shows it as already added', async () => {
    const user = userEvent.setup();
    // Searching is how a long list is used, so a search that finds nothing has to
    // distinguish 「no such model」 from 「you already have it」 — otherwise the
    // answer is 「add a custom model」 for a model that is already in the list.
    answers({
      builtin: [candidate('gpt-6', { origin: 'builtin' })],
      in_list: [candidate('glm-5.2', {
        display_name: 'GLM 5.2',
        suppliers: [{ source_id: 'src_a', source_name: 'Primary relay', model_id: 'glm-5.2-air' }],
      })],
    });
    const { onAdd } = renderPicker({ listedIds: new Set(['glm-5.2']) });

    // With no query the list shows only what can be added.
    expect(await screen.findByRole('checkbox', { name: 'gpt-6' })).toBeTruthy();
    expect(screen.queryByRole('group', { name: LISTED })).toBeNull();

    await user.type(search(), 'primary');
    const row = group(LISTED).getByRole('checkbox') as HTMLButtonElement;
    // Its chips are what the query matched, so they stay on screen.
    expect(row.textContent).toBe('GLM 5.2glm-5.2Primary relay');
    // Checked because it is in the list, disabled because that is not an offer.
    expect(row.getAttribute('aria-checked')).toBe('true');
    expect(row.disabled).toBe(true);
    await user.click(row);
    expect(screen.getByRole('button', { name: 'Add models' }).hasAttribute('disabled')).toBe(true);

    // And the search did not dead-end: there is no 「no model matches」 to answer
    // with a duplicate.
    expect(screen.queryByText('No model matches.')).toBeNull();
    expect(onAdd).not.toHaveBeenCalled();
  });

  it('hands an unmatched query to the editor, and hands over nothing when there is none', async () => {
    const user = userEvent.setup();
    answers({ builtin: [candidate('gpt-6', { origin: 'builtin' })] });
    const { onCustom } = renderPicker();

    await user.type(await screen.findByLabelText('Search models or providers'), '  gemini-4  ');
    expect(screen.getByText('No model matches.')).toBeTruthy();
    // The query is the id the user has already typed once. Asking for it again
    // in the next dialog would be this one forgetting.
    await user.click(screen.getByRole('button', { name: 'Add "gemini-4" as a custom model…' }));
    expect(onCustom).toHaveBeenLastCalledWith('gemini-4');

    await user.clear(search());
    // With no query there is nothing to quote, and the footer already offers the
    // same editor.
    expect(screen.queryByRole('button', { name: /as a custom model/ })).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Add custom model…' }));
    expect(onCustom).toHaveBeenLastCalledWith('');
  });

  it('offers a retry when the models it can add cannot be read', async () => {
    const user = userEvent.setup();
    const candidates = vi.spyOn(modelsApi, 'getAgentModelCandidates')
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(read({ builtin: [candidate('gpt-6', { origin: 'builtin' })] }));
    renderPicker();

    expect(await screen.findByText('The models you can add could not be read.')).toBeTruthy();
    // Nothing to search while there is nothing read.
    expect((search() as HTMLInputElement).disabled).toBe(true);

    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('checkbox', { name: 'gpt-6' })).toBeTruthy();
    expect(candidates).toHaveBeenCalledTimes(2);
  });

  it('re-asks with a seeded pick, and counts only what the current read still offers', async () => {
    const user = userEvent.setup();
    // The re-ask after a stale-supplier refusal (C1): the same models come back
    // picked, with the suppliers the server matched this time. One of them is no
    // longer offered at all — and a label that counted it would add fewer models
    // than it named.
    answers({
      providers: [candidate('glm-5.2', {
        display_name: 'GLM 5.2',
        suppliers: [{ source_id: 'src_b', source_name: 'Backup relay', model_id: 'glm-5.2' }],
      })],
    });
    const { onAdd } = renderPicker({ seedPicked: new Set(['glm-5.2', 'withdrawn']) });

    const row = await screen.findByRole('checkbox', { name: /GLM 5\.2/ });
    expect(row.getAttribute('aria-checked')).toBe('true');
    expect(row.textContent).toContain('Backup relay');
    expect(screen.getByRole('button', { name: 'Add 1 model' })).toBeTruthy();

    await user.click(screen.getByRole('button', { name: 'Add 1 model' }));
    // One object per pick, carrying exactly the suppliers its row displayed —
    // the agreement and the id it is about cannot come apart. And the seeded id
    // this read no longer offers is not in it at all: a withdrawn candidate is
    // dropped from the selection, never re-sent under an expectation nobody
    // could see (C1).
    expect(onAdd.mock.calls[0][0]).toEqual([{
      candidate: expect.objectContaining({ id: 'glm-5.2' }),
      expected_suppliers: [{ source_id: 'src_b', model_id: 'glm-5.2' }],
    }]);
  });

  it('cancels without choosing anything', async () => {
    const user = userEvent.setup();
    answers({ builtin: [candidate('gpt-6', { origin: 'builtin' })] });
    const { onCancel, onAdd } = renderPicker();

    await user.click(await screen.findByRole('checkbox', { name: 'gpt-6' }));
    const exits = screen.getAllByRole('button', { name: 'Cancel' });
    for (const exit of exits) await user.click(exit);

    // Every way out is the same way out: the corner control borrows the
    // footer's label rather than inventing a second word for it, and neither
    // one takes the picks with it.
    expect(onCancel).toHaveBeenCalledTimes(exits.length);
    expect(onAdd).not.toHaveBeenCalled();
  });
});
