// @vitest-environment jsdom
//
// The setup import entry, driven through the real dialog it opens. Only the two
// boundaries are stubbed — the HTTP calls and the capability probe — so what is
// under test is the thing that actually matters here: that the number in the
// sentence, the rows a person is offered, and the ids finally submitted are all
// the same set. A count computed from one rule and rows rendered from another is
// the failure this fixture exists to catch.
//
// The scan fixture carries the shapes `migration.py` really produces, including
// a row with no presentation metadata at all (an older server) and non-ASCII
// detail text.
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { I18nextProvider } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { MigrationItem } from '@/components/settings/models/types';
import { isMigrationDismissed, writeMigrationDismissed } from '@/lib/modelHubMigrationDismiss';
import { RouteSurfaceActiveContext } from '@/lib/routeSurfaceActivity';

const showToast = vi.hoisted(() => vi.fn());
const capability = vi.hoisted(() => ({ value: true as boolean | null }));

vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('@/components/settings/models/useModelHubCapability', () => ({
  useModelHubCapability: () => capability.value,
}));

import { modelsApi } from '@/components/settings/models/modelsApi';
import { ImportKeysNotice } from './ImportKeysNotice';

const CLAUDE_KEY: MigrationItem = {
  id: 'mig_claude_key',
  backend: 'claude',
  kind: 'api_key',
  masked_detail: 'sk-…dd3c',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'anthropic',
  display_name: 'Anthropic',
  masked_credential: 'sk-…dd3c',
};

// A native subscription. Legitimately applied by the settings migration; never
// part of this entry.
const CLAUDE_SUBSCRIPTION: MigrationItem = {
  id: 'mig_claude_oauth',
  backend: 'claude',
  kind: 'oauth_native',
  masked_detail: 'Claude 账号登录（OAuth）',
  proposed_action: 'keep_native',
  selected: true,
  notes_key: null,
  vendor: 'anthropic',
  display_name: 'Anthropic',
  masked_credential: null,
};

// Key-shaped, but needs the interactive browser flow — not batchable.
const CODEX_REAUTH: MigrationItem = {
  id: 'mig_codex_reauth',
  backend: 'codex',
  kind: 'api_key',
  masked_detail: 'sk-…9f21',
  proposed_action: 'reauth',
  selected: true,
  notes_key: null,
  vendor: 'openai',
  display_name: 'OpenAI',
  masked_credential: 'sk-…9f21',
};

// The same backend without the blocker beside it: a key this entry really may take
// over, and the second consent group that makes a partial selection possible at all.
const CODEX_KEY: MigrationItem = {
  id: 'mig_codex_key',
  backend: 'codex',
  kind: 'api_key',
  masked_detail: 'sk-…5e77',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'openai',
  display_name: 'OpenAI',
  masked_credential: 'sk-…5e77',
};

// An OpenCode key: `opencode_provider`, never `api_key`, and its display_name is
// only the provider id echoed back.
const OPENCODE_ZHIPU: MigrationItem = {
  id: 'mig_opencode_zhipu',
  backend: 'opencode',
  kind: 'opencode_provider',
  masked_detail: 'zhipu · sk-…3456',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
  vendor: 'zhipu',
  display_name: 'zhipu',
  masked_credential: 'sk-…3456',
};

// An older server, or a provider nothing recognises: no presentation metadata at
// all, so the row has only its composed detail to show.
const OPENCODE_LEGACY: MigrationItem = {
  id: 'mig_opencode_legacy',
  backend: 'opencode',
  kind: 'opencode_provider',
  masked_detail: '自建中转 · sk-…abcd',
  proposed_action: 'import',
  selected: true,
  notes_key: null,
};

const FULL_SCAN = [CLAUDE_KEY, CLAUDE_SUBSCRIPTION, CODEX_REAUTH, OPENCODE_ZHIPU, OPENCODE_LEGACY];

// A fake server rather than a queue of canned replies: the notice and the dialog
// it opens each scan on their own schedule, so a fixed reply order would decide
// which surface saw what. Applying removes the imported rows, which is what makes
// the notice's post-import remainder a real answer instead of arithmetic.
let stored: MigrationItem[] = [];
const applied: string[][] = [];

const serve = (items: MigrationItem[]) => {
  stored = items.map((i) => ({ ...i }));
  vi.spyOn(modelsApi, 'scanMigration').mockImplementation(async () => ({
    items: stored.map((i) => ({ ...i })),
  }));
  vi.spyOn(modelsApi, 'applyMigration').mockImplementation(async (ids) => {
    applied.push([...ids]);
    stored = stored.filter((i) => !ids.includes(i.id));
    return { applied: ids.length, sources: [] };
  });
};

const renderNotice = (onApplied?: (applied: number) => void) =>
  render(
    <I18nextProvider i18n={i18n}>
      <ImportKeysNotice onApplied={onApplied} />
    </I18nextProvider>,
  );

const openDialog = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(await screen.findByRole('button', { name: 'Review migration' }));
  return screen.findByRole('dialog');
};

beforeEach(async () => {
  capability.value = true;
  applied.length = 0;
  window.localStorage.clear();
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  showToast.mockReset();
});

describe('ImportKeysNotice', () => {
  it('rescans on return from Settings without remounting the setup notice', async () => {
    serve([CLAUDE_KEY]);
    const notice = (active: boolean) => (
      <I18nextProvider i18n={i18n}>
        <RouteSurfaceActiveContext.Provider value={active}>
          <ImportKeysNotice />
        </RouteSurfaceActiveContext.Provider>
      </I18nextProvider>
    );
    const view = render(notice(true));
    expect(await screen.findByText('Found 1 API key to import into Model Hub')).toBeTruthy();
    view.rerender(notice(false));
    stored = [];
    expect(modelsApi.scanMigration).toHaveBeenCalledTimes(1);
    view.rerender(notice(true));
    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Review migration' })).toBeNull());
    expect(modelsApi.applyMigration).not.toHaveBeenCalled();
  });

  it('counts the keys its own review can act on, not every importable row', async () => {
    serve(FULL_SCAN);
    renderNotice();
    const user = userEvent.setup();

    // Two of five. The subscription and the reauth row are not offers; and the
    // Claude key beside that subscription is not one either, because the dialog this
    // sentence opens takes a backend whole and will not submit a batch that omits
    // the sign-in sitting in the same file. Counting it would advertise a key whose
    // own review has nothing to press.
    expect(await screen.findByText('Found 2 API keys to import into Model Hub')).toBeTruthy();

    // The same number, read off the dialog: what it counted is what can be ticked.
    const dialog = await openDialog(user);
    const offered = within(dialog)
      .getAllByRole('checkbox')
      .filter((box) => !(box as HTMLButtonElement).disabled);
    expect(offered).toHaveLength(2);
  });

  it('offers exactly the rows it counted, and no subscription or reauth row', async () => {
    serve(FULL_SCAN);
    renderNotice();
    const user = userEvent.setup();

    const dialog = await openDialog(user);

    expect(within(dialog).getAllByText('Anthropic').length).toBeGreaterThan(0);
    // `display_name` was only the provider id; the shared brand mapping names it.
    expect(within(dialog).getByText('Zhipu AI')).toBeTruthy();
    // No metadata at all: the composed detail stays the title rather than a
    // provider guessed from the backend that held the key.
    expect(within(dialog).getByText('自建中转 · sk-…abcd')).toBeTruthy();

    expect(within(dialog).getByText(/Claude 账号登录/)).toBeTruthy();
    expect(
      (within(dialog).getByRole('checkbox', { name: /Claude 账号登录/ }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(within(dialog).queryByText(/Re-authorize/)).toBeNull();
    expect(within(dialog).getByRole('button', { name: 'Start migration' })).toBeTruthy();
  });

  it('MH-MIG-004: submits only the rows still ticked', async () => {
    serve(FULL_SCAN);
    const onApplied = vi.fn();
    renderNotice(onApplied);
    const user = userEvent.setup();

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole('checkbox', { name: /sk-…dd3c/ }));
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    await waitFor(() => expect(applied).toHaveLength(1));
    expect(applied[0]).toEqual(['mig_opencode_zhipu', 'mig_opencode_legacy']);
    expect(onApplied).toHaveBeenCalledWith(2);
  });

  it('stays to report what was imported and what is still available', async () => {
    // Two independent groups, so half the offer can be left for later.
    serve([CODEX_KEY, OPENCODE_ZHIPU, OPENCODE_LEGACY]);
    renderNotice();
    const user = userEvent.setup();

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole('checkbox', { name: /sk-…5e77/ }));
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    // The remainder is the server's answer after the rescan, not a subtraction.
    expect(await screen.findByText('Migrated 2 · 1 API key still available')).toBeTruthy();
    // A partial selection can still be finished from here.
    expect(screen.getByRole('button', { name: 'Review migration' })).toBeTruthy();
  });

  it('reports the outcome without offering a review of what it may not take', async () => {
    serve(FULL_SCAN);
    renderNotice();
    const user = userEvent.setup();

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole('button', { name: 'Start migration' }));

    // What is left on the machine is a Claude key nothing here may take and a reauth
    // row that was never an offer. The receipt says what happened; it does not invite
    // a second look at a review with nothing to press. Settings still has both.
    expect(await screen.findByText('Migrated 2 API keys into Model Hub')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Review migration' })).toBeNull();
    expect(stored.map((i) => i.id)).toEqual([
      'mig_claude_key',
      'mig_claude_oauth',
      'mig_codex_reauth',
    ]);
  });

  it('remembers a dismissal so the same keys do not ask again', async () => {
    serve(FULL_SCAN);
    const { unmount } = renderNotice();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Dismiss import notice' }));
    expect(screen.queryByText(/API keys to import/)).toBeNull();

    unmount();
    cleanup();
    renderNotice();
    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/API keys to import/)).toBeNull();
  });

  it('asks again when a genuinely new key appears', async () => {
    serve(FULL_SCAN);
    const { unmount } = renderNotice();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Dismiss import notice' }));
    unmount();
    cleanup();
    stored = [...stored, { ...OPENCODE_ZHIPU, id: 'mig_opencode_new', masked_detail: 'kimi · sk-…7a10' }];
    renderNotice();

    expect(await screen.findByText('Found 3 API keys to import into Model Hub')).toBeTruthy();
  });

  it('does not ask again for a key its review could not act on', async () => {
    serve(FULL_SCAN);
    const { unmount } = renderNotice();
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: 'Dismiss import notice' }));
    unmount();
    cleanup();
    // A second Claude key, in the group the sign-in beside it blocks. Nothing this
    // entry can offer has changed, so reappearing would be a notice about keys it
    // would then decline to import.
    stored = [...stored, { ...CLAUDE_KEY, id: 'mig_claude_key_2', masked_detail: 'sk-…7a10' }];
    renderNotice();

    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/API keys? to import/)).toBeNull();
  });

  it('says nothing when there is nothing to import', async () => {
    serve([CLAUDE_SUBSCRIPTION, CODEX_REAUTH]);
    renderNotice();

    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalled());
    expect(screen.queryByText(/Model Gateway/)).toBeNull();
  });

  it('says nothing when the scan fails', async () => {
    // Advertising rows after a failed scan would open a dialog that rescans,
    // fails again and shows nothing.
    vi.spyOn(modelsApi, 'scanMigration').mockRejectedValue(new Error('offline'));
    renderNotice();

    await waitFor(() => expect(modelsApi.scanMigration).toHaveBeenCalled());
    expect(screen.queryByText(/Model Gateway/)).toBeNull();
  });

  it('does not scan at all when the gateway capability is off', async () => {
    capability.value = false;
    serve(FULL_SCAN);
    renderNotice();

    await Promise.resolve();
    expect(modelsApi.scanMigration).not.toHaveBeenCalled();
    expect(screen.queryByText(/Model Gateway/)).toBeNull();
  });
});

// The providers screen holds one scan for the stage, the footer action and this
// sentence at once. Hosting hands the capsule the rows rather than letting it take
// a second scan, because two reads of the same machine are how three surfaces end
// up advertising different numbers of the same keys. What the capsule keeps is what
// it always owned: the sentence, the help, and the dismissal signature.
describe('ImportKeysNotice — hosted by a screen that already scanned', () => {
  const renderHosted = (props: { candidates: MigrationItem[]; imported?: number; onReview?: () => void }) => {
    const element = ({ candidates, imported = 0, onReview }: typeof props) => (
      <I18nextProvider i18n={i18n}>
        <ImportKeysNotice candidates={candidates} imported={imported} onReview={onReview} />
      </I18nextProvider>
    );
    const view = render(element(props));
    return { ...view, show: (next: typeof props) => view.rerender(element(next)) };
  };

  it('takes no scan of its own and says what the host gave it', async () => {
    serve(FULL_SCAN);
    const onReview = vi.fn();
    renderHosted({ candidates: [CLAUDE_KEY, OPENCODE_ZHIPU], onReview });
    const user = userEvent.setup();

    expect(screen.getByText('Found 2 API keys to import into Model Hub')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Review migration' }));

    // The host's dialog, over the host's rows.
    expect(onReview).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(modelsApi.scanMigration).not.toHaveBeenCalled();
  });

  it('does not let its own capability probe overrule a host that has real evidence', async () => {
    // The probe resolves its own failures to `false`. A host holding rows has
    // better evidence than that, and hiding a real offer on a blip is the worse
    // failure of the two.
    capability.value = false;
    serve(FULL_SCAN);
    renderHosted({ candidates: [CLAUDE_KEY] });

    expect(screen.getByText('Found 1 API key to import into Model Hub')).toBeTruthy();
  });

  it('honours a dismissal the first batch is recognised by', async () => {
    writeMigrationDismissed([CLAUDE_KEY, OPENCODE_ZHIPU]);
    serve(FULL_SCAN);
    // The host is still scanning: an empty batch is not the first batch.
    const { show } = renderHosted({ candidates: [] });

    show({ candidates: [CLAUDE_KEY, OPENCODE_ZHIPU] });

    expect(screen.queryByText(/API keys to import/)).toBeNull();
  });

  it('reports what the host just imported even against a remembered dismissal', async () => {
    // These two were dismissed on some earlier visit.
    writeMigrationDismissed([CLAUDE_KEY, OPENCODE_ZHIPU]);
    serve(FULL_SCAN);
    const { show } = renderHosted({ candidates: [OPENCODE_LEGACY] });
    expect(screen.getByText('Found 1 API key to import into Model Hub')).toBeTruthy();

    // The host's rescan after an import someone just asked for. Its remainder is
    // a subset of that old dismissal, and consulting it again here would hide the
    // person's own result.
    show({ candidates: [OPENCODE_ZHIPU], imported: 1 });

    expect(screen.getByText('Migrated 1 · 1 API key still available')).toBeTruthy();
  });

  it('brings the receipt back when the host imports against a dismissal it honoured', async () => {
    // Dismissed on an earlier visit, so the capsule opens hidden — and the footer
    // CTA can still submit that very batch, because the stage and the action read
    // the scan, not this signature. Once it lands, `imported` is the answer to
    // something the person just asked for; a remembered opinion about the offer
    // cannot outrank it, and the remainder has to come back with it.
    writeMigrationDismissed([CLAUDE_KEY, OPENCODE_ZHIPU]);
    serve(FULL_SCAN);
    const { show } = renderHosted({ candidates: [CLAUDE_KEY, OPENCODE_ZHIPU] });
    expect(screen.queryByText(/API keys to import/)).toBeNull();

    show({ candidates: [OPENCODE_ZHIPU], imported: 1 });

    expect(screen.getByText('Migrated 1 · 1 API key still available')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Review migration' })).toBeTruthy();
  });

  it('keeps a dismissed receipt closed until something new lands', async () => {
    serve(FULL_SCAN);
    const { show } = renderHosted({ candidates: [OPENCODE_ZHIPU], imported: 1 });
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Dismiss import notice' }));
    expect(screen.queryByText(/Migrated/)).toBeNull();

    // The host's ordinary re-renders — a rescan that found the same remainder —
    // are not news, and a capsule returning on each one is the nag the dismissal
    // exists to prevent.
    show({ candidates: [OPENCODE_ZHIPU], imported: 1 });
    expect(screen.queryByText(/Migrated/)).toBeNull();

    // A second batch landing is news.
    show({ candidates: [], imported: 2 });
    expect(screen.getByText('Migrated 2 API keys into Model Hub')).toBeTruthy();
  });

  it('closes the receipt without remembering an empty set', async () => {
    serve(FULL_SCAN);
    renderHosted({ candidates: [], imported: 2 });
    const user = userEvent.setup();

    expect(screen.getByText('Migrated 2 API keys into Model Hub')).toBeTruthy();
    // Nothing left to review, so nothing to review.
    expect(screen.queryByRole('button', { name: 'Review migration' })).toBeNull();

    await user.click(screen.getByRole('button', { name: 'Dismiss import notice' }));

    expect(screen.queryByText(/Migrated/)).toBeNull();
    // Remembering nothing would overwrite the signature an earlier batch was
    // dismissed by, and those keys would start asking again.
    expect(isMigrationDismissed([CLAUDE_KEY])).toBe(false);
  });

  it('says nothing when the host has neither an offer nor an outcome', async () => {
    serve(FULL_SCAN);
    renderHosted({ candidates: [] });

    expect(screen.queryByText(/Model Hub/)).toBeNull();
  });
});
