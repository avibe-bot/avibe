/* @vitest-environment jsdom */

import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation, useNavigate, useNavigationType } from 'react-router-dom';
import { createInstance } from 'i18next';
import { initReactI18next } from 'react-i18next';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import type { VaultGrant, VaultSecret } from '../../context/ApiContext';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';

import { InstanceAuthorizationContext, type InstanceAuthorizationValue } from '../../context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { capabilitiesFor } from '../../lib/testing/instanceRoleCapabilities';
import { VaultsPage } from './VaultsPage';

type WorkbenchEventHandlers = {
  onVaultsUpdated?: (data: unknown) => void;
  onEventBridgeStatus?: (data: { connected: boolean }) => void;
};

const eventHandlers = vi.hoisted(() => [] as WorkbenchEventHandlers[]);
const api = vi.hoisted(() => ({
  connectWorkbenchEvents: vi.fn(() => () => undefined),
  createVaultRevealContext: vi.fn(),
  getVaultAudit: vi.fn(),
  getVaultGrants: vi.fn(),
  getVaultRequests: vi.fn(),
  listVaultSecrets: vi.fn(),
  listDependencies: vi.fn(),
  listSkills: vi.fn(),
}));
const showToast = vi.hoisted(() => vi.fn());
const revealProtectedValue = vi.hoisted(() => vi.fn());
const useVaultSandboxWarm = vi.hoisted(() => vi.fn((_enabled: boolean) => true));
const remoteOwner = {
  remote: true,
  instanceKind: 'organization' as const,
  instanceRole: 'owner' as const,
  capabilities: {
    ...OWNER_INSTANCE_CAPABILITIES,
    can_use_system: false,
  },
};

const editor: InstanceAuthorizationValue = {
  remote: true,
  instanceKind: 'organization',
  instanceRole: 'editor',
  capabilities: capabilitiesFor('editor'),
};

const member: InstanceAuthorizationValue = {
  remote: true,
  instanceKind: 'organization',
  instanceRole: 'member',
  capabilities: capabilitiesFor('member'),
};

vi.mock('../../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../../context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('../../context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));

vi.mock('../../lib/useProtectedVault', () => ({
  useProtectedVault: () => ({ revealProtectedValue, status: 'locked', refresh: vi.fn(), hasPasskey: () => false }),
  webauthnAvailable: () => false,
  useVaultSandboxWarm,
  useVaultLock: () => ({ unlocked: false, remainingMs: 0, lockNow: vi.fn() }),
}));

const testI18n = createInstance();
void testI18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en }, zh: { translation: zh } }, initAsync: false });
vi.mock('react-i18next', async (loadOriginal) => ({
  ...await loadOriginal<typeof import('react-i18next')>(),
  useTranslation: () => ({ t: testI18n.t }),
}));

vi.mock('./CapabilityTabs', () => ({
  CapabilityTabs: () => <div data-testid="capability-tabs" />,
}));

vi.mock('./WorkbenchPageHeader', () => ({
  WorkbenchPageHeader: ({ title, actions }: { title: ReactNode; actions?: ReactNode }) => (
    <div>
      <h1>{title}</h1>
      <div>{actions}</div>
    </div>
  ),
}));

function LocationProbe() {
  const location = useLocation();
  const navigation = useNavigationType();
  const navigate = useNavigate();
  return <>
    <output data-testid="location">{location.search}</output>
    <output data-testid="navigation">{navigation}</output>
    <button onClick={() => navigate('/vaults?q=beta&tag=shared&sort=added')}>Follow saved view</button>
  </>;
}

function renderPage(context: InstanceAuthorizationValue = remoteOwner, entry = '/vaults', active = true) {
  return render(
    <InstanceAuthorizationContext.Provider value={context}>
      <MemoryRouter initialEntries={[entry]}>
        <RouteSurfaceActiveContext.Provider value={active}><VaultsPage /></RouteSurfaceActiveContext.Provider>
        <LocationProbe />
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

beforeEach(async () => {
  await testI18n.changeLanguage('en');
  eventHandlers.length = 0;
  api.connectWorkbenchEvents.mockImplementation((handlers: WorkbenchEventHandlers) => {
    eventHandlers.push(handlers);
    return () => {
      const at = eventHandlers.indexOf(handlers);
      if (at >= 0) eventHandlers.splice(at, 1);
    };
  });
  api.listDependencies.mockResolvedValue({ deps: [] });
  api.listSkills.mockResolvedValue({ ok: true, skills: [] });
  api.getVaultRequests.mockResolvedValue({ requests: [] });
  api.listVaultSecrets.mockResolvedValue({ secrets: [] });
  api.getVaultGrants.mockResolvedValue({ grants: [] });
  api.getVaultAudit.mockResolvedValue({
    events: [{ id: 'audit-1', event: 'grant_created', secret_name: 'alpha', ts: '2026-08-11T00:00:00Z' }],
  });
});

afterEach(async () => {
  await testI18n.changeLanguage('en');
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe('VaultsPage remote audit history', () => {
  it('keeps the History button reachable for a remote owner and loads audit rows', async () => {
    const user = userEvent.setup();

    renderPage();

    const history = await screen.findByRole('button', { name: 'History' });
    expect(screen.getByRole('button', { name: 'Add secret' })).toBeTruthy();

    await user.click(history);

    await waitFor(() => expect(api.getVaultAudit).toHaveBeenCalledWith({ limit: 50 }));
    expect(await screen.findByText('Recent activity')).toBeTruthy();
    expect(await screen.findByText('grant_created')).toBeTruthy();
    expect(await screen.findByText('alpha')).toBeTruthy();
  });

  it('keeps Add secret available to an Editor', async () => {
    renderPage(editor);

    expect(await screen.findByRole('button', { name: 'Add secret' })).toBeTruthy();
    expect(screen.queryByText('Read-only')).toBeNull();
  });
});

describe('PERMISSIONS-013 VaultsPage capability-gated management actions', () => {
  const protectedSecret: VaultSecret = {
    name: 'protected-secret', description: 'protected deployment', tags: [], kind: 'static', protection: 'protected',
    signer_kind: null, source: 'user', policy: {}, last_used_at: null, use_count: 0,
    created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
  };
  const standardSecret: VaultSecret = {
    name: 'standard-secret', description: 'standard deployment', tags: [], kind: 'static', protection: 'standard',
    signer_kind: null, source: 'user', policy: {}, last_used_at: null, use_count: 0,
    created_at: '2026-10-02T00:00:00Z', updated_at: '2026-10-02T00:00:00Z',
  };
  const keypairSecret: VaultSecret = { ...protectedSecret, name: 'keypair-secret', kind: 'keypair' };
  const activeGrant: VaultGrant = {
    id: 'grant-1', source_selector: { env: ['PROD_KEY'] }, session_id: null, purpose: 'run', status: 'active',
    request_id: null, created_at: '2026-10-01T00:00:00Z', expires_at: '2026-12-31T00:00:00Z', revoked_at: null,
    member_snapshot: ['protected-secret'], member_count: 1, runtime_member_count: 1,
  };
  const cases = [
    {
      label: 'Editor', context: editor, canRevoke: true, hasSettings: false,
      protectedMenu: ['Show value', 'Copy value'], standardMenu: null,
    },
    {
      label: 'Member', context: member, canRevoke: true, hasSettings: true,
      protectedMenu: ['Show value', 'Copy value', 'Edit secret', 'Delete'], standardMenu: ['Edit secret', 'Delete'],
    },
  ] as const;

  const rowFor = (name: string) => {
    const row = screen.getByText(name, { exact: true }).closest('li');
    if (!row) throw new Error(`missing row for ${name}`);
    return row as HTMLElement;
  };

  it.each(cases)('$label sees only the Vault controls its capability tier permits', async ({ context, canRevoke, hasSettings, protectedMenu, standardMenu }) => {
    const user = userEvent.setup();
    api.listVaultSecrets.mockResolvedValue({ secrets: [protectedSecret, standardSecret, keypairSecret] });
    api.getVaultGrants.mockResolvedValue({ grants: [activeGrant] });
    renderPage(context);

    expect(await screen.findByRole('button', { name: 'Add secret' })).toBeTruthy();
    expect(Boolean(screen.queryByRole('button', { name: 'Vault settings' }))).toBe(hasSettings);
    await screen.findByText('PROD_KEY');
    // _require_secret_names_access admits Editors before its management branch.
    expect(Boolean(screen.queryByRole('button', { name: 'Revoke' }))).toBe(canRevoke);

    const protectedRow = rowFor('protected-secret');
    const protectedTrigger = within(protectedRow).queryByRole('button', { name: 'More actions' });
    expect(protectedTrigger).toBeTruthy();
    await user.click(protectedTrigger!);
    expect(screen.getAllByRole('menuitem').map((item) => item.textContent?.trim())).toEqual(protectedMenu);
    await user.keyboard('{Escape}');

    for (const name of ['standard-secret', 'keypair-secret']) {
      const trigger = within(rowFor(name)).queryByRole('button', { name: 'More actions' });
      if (standardMenu === null) {
        expect(trigger).toBeNull();
      } else {
        expect(trigger).toBeTruthy();
        await user.click(trigger!);
        expect(screen.getAllByRole('menuitem').map((item) => item.textContent?.trim())).toEqual(standardMenu);
        await user.keyboard('{Escape}');
      }
    }
  });

  it('keeps an Editor edit handoff in the list instead of opening a manager-only dialog', async () => {
    const editorSecret = { ...protectedSecret };
    api.listVaultSecrets.mockResolvedValue({ secrets: [editorSecret] });
    renderPage(editor, '/vaults?edit=protected-secret');
    await screen.findAllByText('protected-secret');

    // Wait for the handoff effect, not the initially absent dialog, before checking its result.
    await waitFor(() => expect(screen.getByTestId('location').textContent).not.toContain('edit='));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('list', { name: 'Vaults' })).toBeTruthy();
  });

  it('keeps long secret names inside the delete confirmation and leaves actions reachable', async () => {
    const user = userEvent.setup();
    const longName = 'AVIBE_UI_REGRESSION_STANDARD_20260810_1234567890_abcdefghijklmnopqrstuvwxyz';
    api.listVaultSecrets.mockResolvedValue({ secrets: [{ ...protectedSecret, name: longName }] });
    renderPage(member);

    const row = (await screen.findByText(longName, { exact: true })).closest('li') as HTMLElement;
    await user.click(within(row).getByRole('button', { name: 'More actions' }));
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }));

    const dialog = await screen.findByRole('dialog');
    const title = within(dialog).getByRole('heading', { name: `Delete "${longName}"?` });
    expect(dialog.className).toContain('min-w-0');
    expect(title.className).toContain('break-words');
    expect(title.className).toContain('[overflow-wrap:anywhere]');
    expect(within(dialog).getByRole('button', { name: /^Delete/ })).toBeTruthy();
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeTruthy();
  });
});

describe('VaultsPage list discovery', () => {
  const makeSecret = (name: string, overrides: Partial<VaultSecret> = {}): VaultSecret => ({
    name, description: `${name} deployment`, tags: [], kind: 'static', protection: 'standard',
    signer_kind: null, source: 'user', policy: {}, last_used_at: null, use_count: 0,
    created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', ...overrides,
  });
  const inventory = [
    makeSecret('beta', { kind: 'keypair', protection: 'protected', tags: ['shared', 'prod', 'skill:sign'], last_used_at: '2026-10-13T11:00:00Z', created_at: '2026-05-01T00:00:00Z' }),
    makeSecret('zeta', { tags: ['shared', 'z-only', 'skill:release', 'third'], created_at: '2026-09-01T00:00:00Z', policy: { allowed_hosts: ['api.example.test'] } }),
    makeSecret('alpha', { description: 'Alpha 支付 [prod]', protection: 'protected', tags: ['shared', 'shared', 'prod', 'skill:release'], last_used_at: '2026-10-10T12:00:00Z', use_count: 7 }),
  ];
  const rows = () => within(screen.getByRole('list', { name: 'Vaults' })).getAllByRole('listitem');
  const names = () => rows().map((row) => row.querySelector('.font-mono')?.textContent);
  const params = () => new URLSearchParams(screen.getByTestId('location').textContent ?? '');
  const count = () => screen.getAllByRole('status').find((element) => element.tagName === 'SPAN')?.textContent;

  beforeEach(() => {
    api.listVaultSecrets.mockResolvedValue({ secrets: inventory });
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-10-13T12:00:00Z'));
  });

  // Existing tests cover audit and reveal, not any of the list's discovery controls.
  // These assert the page's actual rendered rows rather than duplicating the matcher.
  it('searches and highlights literal name/description terms, reports counts, and clears from keyboard and button', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('alpha');
    const search = screen.getByRole('textbox', { name: 'Search secrets' });
    expect(count()).toBe('3 secrets');
    await user.keyboard('/');
    expect(document.activeElement).toBe(search);
    await user.type(search, 'ALPHA 支付');
    expect(names()).toEqual(['alpha']);
    expect(count()).toBe('1 / 3 secrets');
    expect([...rows()[0].querySelectorAll('mark')].map((mark) => mark.textContent)).toEqual(['alpha', 'Alpha', '支付']);
    expect(rows()[0].textContent).toContain('used 7× · 3d ago');
    await user.keyboard('{Escape}');
    expect(count()).toBe('3 secrets');
    await user.paste('[prod]');
    expect(rows()[0].querySelector('mark')?.textContent).toBe('[prod]');
    await user.click(screen.getByRole('button', { name: 'Clear search' }));
    expect(params().has('q')).toBe(false);
    expect(names()).toEqual(['alpha', 'beta', 'zeta']);
  });

  it('pluralizes the unfiltered singular count in English', async () => {
    api.listVaultSecrets.mockResolvedValue({ secrets: [inventory[0]] });
    renderPage();
    await screen.findByText('beta');
    expect(count()).toBe('1 secret');
  });

  it('renders unit-bearing counts in Chinese for both unfiltered and filtered states', async () => {
    await testI18n.changeLanguage('zh');
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('alpha');
    expect(count()).toBe('3 个密钥');
    await user.type(screen.getByRole('textbox', { name: '搜索密钥' }), 'alpha');
    expect(count()).toBe('1 / 3 个密钥');
  });
  it('leaves slash in editable controls and does not steal focus from a hidden route', async () => {
    const user = userEvent.setup();
    const view = renderPage();
    await screen.findByText('alpha');
    await user.click(screen.getByRole('button', { name: 'Tags' }));
    const tagSearch = screen.getByRole('textbox', { name: 'Search tags' });
    await user.type(tagSearch, '/');
    expect(document.activeElement).toBe(tagSearch);
    expect((tagSearch as HTMLInputElement).value).toBe('/');
    view.unmount();
    renderPage(remoteOwner, '/vaults', false);
    await screen.findByText('alpha');
    await user.keyboard('/');
    expect(document.activeElement).not.toBe(screen.getByRole('textbox', { name: 'Search secrets' }));
  });

  it('groups and sorts dropdown inventory counts, searches tags, and intersects multi-selection without closing', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('alpha');
    await user.click(screen.getByRole('button', { name: 'Tags' }));
    expect(screen.getAllByRole('checkbox').map((item) => item.textContent)).toEqual(['release2', 'sign1', 'shared3', 'prod2', 'third1', 'z-only1']);
    await user.click(screen.getByRole('checkbox', { name: 'release · 2' }));
    await user.click(screen.getByRole('checkbox', { name: 'prod · 2' }));
    expect(names()).toEqual(['alpha']);
    expect(screen.getByRole('checkbox', { name: 'release · 2' }).getAttribute('aria-checked')).toBe('true');
    expect(count()).toBe('1 / 3 secrets');
    const tagSearch = screen.getByRole('textbox', { name: 'Search tags' });
    await user.type(tagSearch, 'SHAR');
    expect(screen.getAllByRole('checkbox').map((item) => item.textContent)).toEqual(['shared3']);
    await user.clear(tagSearch);
    await user.type(tagSearch, 'no-such-tag');
    expect(screen.getByText('No matching tags')).toBeTruthy();
    await user.keyboard('{Escape}');
    expect(screen.getByRole('button', { name: 'Tags (2)' })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Remove filter: prod' }));
    expect(names()).toEqual(['alpha', 'zeta']);
    await user.click(screen.getByRole('button', { name: 'Clear all' }));
    expect(count()).toBe('3 secrets');
  });

  it.each(['hover', 'click', 'focus'] as const)('caps inline tags at two with skills first and reveals actionable overflow on %s', async (method) => {
    api.listVaultSecrets.mockResolvedValue({ secrets: [inventory[1]] });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('zeta');
    const row = rows()[0];
    expect(within(row).getAllByRole('button').map((button) => button.textContent)).toEqual(['release', 'shared', '+2', '']);
    expect(screen.queryByRole('button', { name: 'z-only' })).toBeNull();
    const more = within(row).getByRole('button', { name: 'Show 2 more tags' });
    if (method === 'hover') await user.hover(more);
    if (method === 'click') await user.click(more);
    if (method === 'focus') {
      act(() => more.focus());
      await user.keyboard('{Tab}');
      expect(document.activeElement).toBe(screen.getByRole('button', { name: 'z-only' }));
    }
    await user.click(screen.getByRole('button', { name: 'z-only' }));
    expect(params().getAll('tag')).toEqual(['z-only']);
    await user.keyboard('{Escape}');
    expect(document.activeElement).toBe(more);
    expect(screen.queryByRole('button', { name: 'z-only' })).toBeNull();
    await user.click(within(row).getByRole('button', { name: 'release' }));
    expect(params().getAll('skill')).toEqual(['release']);
    expect(count()).toBe('1 / 1 secrets');
  });

  it('composes quick filters and sorting, keeping unused last and dates relative even beyond a week', async () => {
    api.listVaultSecrets.mockResolvedValue({ secrets: [...inventory, makeSecret('aardvark', { last_used_at: '2026-09-30T12:00:00Z' })] });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('alpha');
    expect(names()).toEqual(['aardvark', 'alpha', 'beta', 'zeta']);
    expect(rows()[0].textContent).toContain('13d ago');
    const sort = screen.getByRole('combobox', { name: 'Sort' });
    await user.selectOptions(sort, 'recent');
    expect(names()).toEqual(['beta', 'alpha', 'aardvark', 'zeta']);
    await user.selectOptions(sort, 'added');
    expect(names()).toEqual(['zeta', 'beta', 'aardvark', 'alpha']);
    await user.click(screen.getByRole('radio', { name: 'Protected' }));
    expect(names()).toEqual(['beta', 'alpha']);
    await user.click(screen.getByRole('radio', { name: 'Signing keys' }));
    expect(names()).toEqual(['beta']);
    await user.click(screen.getByRole('radio', { name: 'Never used' }));
    expect(names()).toEqual(['zeta']);
    expect(count()).toBe('1 / 4 secrets');
    await user.click(screen.getByRole('radio', { name: 'All', exact: true }));
    expect(count()).toBe('4 secrets');
  });

  // With SSE connected there is no fallback polling, and an inventory without
  // grants has no countdown. Relative labels still need to advance: the earlier
  // static-label tests could not detect coupling the clock to grant lifetime.
  it('advances last-use labels with a connected bridge and no grants, without refetching', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    try {
      api.listVaultSecrets.mockResolvedValue({ secrets: [makeSecret('alpha', { last_used_at: '2026-10-13T11:55:00Z' })] });
      renderPage();
      await screen.findByText('alpha');
      await act(async () => {
        for (const handler of eventHandlers) handler.onEventBridgeStatus?.({ connected: true });
      });
      expect(rows()[0].textContent).toContain('5m ago');
      const fetches = api.listVaultSecrets.mock.calls.length;
      vi.mocked(Date.now).mockReturnValue(Date.parse('2026-10-13T12:01:00Z'));
      act(() => vi.advanceTimersByTime(60_000));
      expect(rows()[0].textContent).toContain('6m ago');
      expect(api.listVaultSecrets).toHaveBeenCalledTimes(fetches);
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it('round-trips repeatable URL filters with replace, preserving pending handoff and unrelated parameters', async () => {
    const user = userEvent.setup();
    const entry = '/vaults?q=ALPHA&tag=shared&tag=prod&skill=release&filter=protected&sort=added&request_id=waiting&protection=protected&other=keep';
    const view = renderPage(remoteOwner, entry);
    await screen.findByText('alpha');
    expect(names()).toEqual(['alpha']);
    expect((screen.getByRole('textbox', { name: 'Search secrets' }) as HTMLInputElement).value).toBe('ALPHA');
    expect((screen.getByRole('combobox', { name: 'Sort' }) as HTMLSelectElement).value).toBe('added');
    await user.click(screen.getByRole('button', { name: 'Remove filter: prod' }));
    expect(params().getAll('tag')).toEqual(['shared']);
    expect(params().get('request_id')).toBe('waiting');
    expect(screen.getByTestId('navigation').textContent).toBe('REPLACE');
    const saved = `/vaults${screen.getByTestId('location').textContent}`;
    view.unmount();
    renderPage(remoteOwner, saved);
    await screen.findByText('alpha');
    expect(names()).toEqual(['alpha']);
    await user.click(screen.getByRole('button', { name: 'Clear all' }));
    expect(Object.fromEntries(params())).toEqual({ sort: 'added', request_id: 'waiting', protection: 'protected', other: 'keep' });
    await user.click(screen.getByRole('button', { name: 'Follow saved view' }));
    expect(names()).toEqual(['beta']);
    expect((screen.getByRole('textbox', { name: 'Search secrets' }) as HTMLInputElement).value).toBe('beta');
  });

  it('consumes a reveal handoff without dropping the saved view', async () => {
    api.createVaultRevealContext.mockResolvedValue({ ok: true, context: {} });
    renderPage(remoteOwner, '/vaults?q=alpha&tag=shared&skill=release&filter=protected&sort=recent&reveal=alpha&other=keep');
    await screen.findByRole('menuitem', { name: 'Show value' });
    await waitFor(() => expect(Object.fromEntries(params())).toEqual({ q: 'alpha', tag: 'shared', skill: 'release', filter: 'protected', sort: 'recent', other: 'keep' }));
    expect(names()).toEqual(['alpha']);
  });

  it('opens a reveal target hidden by saved filters, clearing those filters but keeping sort', async () => {
    api.createVaultRevealContext.mockResolvedValue({ ok: true, context: {} });
    renderPage(remoteOwner, '/vaults?q=zeta&tag=z-only&skill=missing&filter=unused&sort=added&reveal=alpha&other=keep');
    await screen.findByRole('menuitem', { name: 'Show value' });
    await waitFor(() => expect(Object.fromEntries(params())).toEqual({ sort: 'added', other: 'keep' }));
    expect(names()).toEqual(['zeta', 'beta', 'alpha']);
  });

  it('distinguishes no matches from empty inventory and resets search/filters but not sorting', async () => {
    const user = userEvent.setup();
    const view = renderPage(remoteOwner, '/vaults?q=not-found&tag=shared&filter=protected&sort=added');
    await screen.findByText('No matching secrets');
    expect(count()).toBe('0 / 3 secrets');
    await user.click(screen.getByRole('button', { name: 'Clear search and filters' }));
    expect(names()).toEqual(['zeta', 'beta', 'alpha']);
    expect(Object.fromEntries(params())).toEqual({ sort: 'added' });
    view.unmount();
    api.listVaultSecrets.mockResolvedValue({ secrets: [] });
    renderPage();
    await screen.findByText(en.vaults.empty);
    expect(screen.queryByText('No matching secrets')).toBeNull();
  });
});

// PERMISSIONS-016 — `vaults.updated` is admitted at the Editor tier that owns
// the endpoint it announces, and below runtime management the controller reduces
// the frame to its type alone. The pending-requests list is the consumer that
// has to wake on it, so it is driven here through the real page rather than
// asserted on the stream stub.
describe('PERMISSIONS-016 VaultsPage wakes an Editor on the bare vaults.updated frame', () => {
  it('refetches pending requests from the signal alone', async () => {
    renderPage(editor);
    await waitFor(() => expect(api.getVaultRequests).toHaveBeenCalled());
    const before = api.getVaultRequests.mock.calls.length;

    await act(async () => {
      for (const handler of [...eventHandlers]) handler.onVaultsUpdated?.({});
    });

    await waitFor(() => expect(api.getVaultRequests.mock.calls.length).toBeGreaterThan(before));
    // The frame carried no secret name; the refetch still asks for this Editor's
    // own pending scope.
    const [query] = api.getVaultRequests.mock.calls.at(-1) ?? [];
    expect(query).toMatchObject({ status: 'pending' });
  });
});

// A popup opened after an await has lost the click's user activation and is blocked, which would
// silently bring back the sandbox's own launcher card and its second approval click. A Home Screen
// app on iOS is also frozen about two seconds after it opens a window, so nothing that window needs
// may be fetched after the click: the open row menu keeps a fresh reveal context, Show and Copy wait
// for it and for the sandbox client, and one secret's context never waits on another's. A menu
// fetch that failed is not kept, or the click would replay that failure, and a menu held open past
// the context's freshness bound refreshes it rather than fetching after the click.
describe('VaultsPage protected reveal', () => {
  const protectedSecret = (name: string) => ({
    name,
    tags: [],
    kind: 'static',
    protection: 'protected',
    signer_kind: null,
    source: 'user',
    policy: {},
    last_used_at: null,
    use_count: 0,
    created_at: '2026-08-11T00:00:00Z',
    updated_at: '2026-08-11T00:00:00Z',
  });
  const envelope = { ciphertext: 'c', nonce: 'n', wrap_meta: 'w' };

  beforeEach(() => {
    // Only the menu's refresh interval is faked, so promises and Testing Library keep real timers.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    api.listVaultSecrets.mockResolvedValue({ secrets: [protectedSecret('alpha')] });
    revealProtectedValue.mockResolvedValue(undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const showValue = () => screen.getByRole('menuitem', { name: 'Show value' }) as HTMLButtonElement;

  const openMenu = async (index = 0) => {
    const user = userEvent.setup();
    if (index === 0) renderPage();
    const [button] = (await screen.findAllByRole('button', { name: 'More actions' })).slice(index);
    await user.click(button);
  };

  const clickShowValue = async () => {
    const popup = { closed: false, close: vi.fn() };
    const open = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
    await vi.waitFor(() => expect(showValue().disabled).toBe(false));
    fireEvent.click(showValue());
    // Asserted synchronously after the click dispatch, before any awaited work can run.
    expect(open).toHaveBeenCalledOnce();
    const opened = new URL(String(open.mock.calls[0][0]));
    expect(opened.searchParams.get('mode')).toBe('authorize');
    await vi.waitFor(() => expect(revealProtectedValue).toHaveBeenCalledOnce());
    expect(revealProtectedValue.mock.calls[0][2]).toBe(opened.searchParams.get('id'));
    await vi.waitFor(() => expect(popup.close).toHaveBeenCalledOnce());
    return { open };
  };

  it.each([
    { menuFetch: 'succeeded', menuReply: { ok: true, context: { secret_name: 'alpha', menu: true } }, fetches: 1, revealed: { secret_name: 'alpha', menu: true } },
    { menuFetch: 'failed', menuReply: { ok: false, code: 'vault_error', message: 'transient' }, fetches: 2, revealed: { secret_name: 'alpha' } },
  ])('opens the sandbox authorization window inside the click and hands it a good context when the menu fetch $menuFetch', async ({ menuReply, fetches, revealed }) => {
    api.createVaultRevealContext
      .mockResolvedValueOnce({ ...menuReply, envelope })
      .mockResolvedValue({ ok: true, context: { secret_name: 'alpha' }, envelope });

    await openMenu();
    await clickShowValue();

    expect(revealProtectedValue.mock.calls[0][1]).toEqual(revealed);
    expect(api.createVaultRevealContext).toHaveBeenCalledTimes(fetches);
  });

  it('keeps the fields warning visible and exposes the current named field line', async () => {
    api.listVaultSecrets.mockResolvedValue({
      secrets: [{ ...protectedSecret('alpha'), default_field: 'password', fields: ['password', 'username'], fields_state: 'corrupt' }],
    });
    renderPage();
    expect(await screen.findByText(/password/)).toBeTruthy();
    expect(screen.getByRole('alert')).toBeTruthy();
    await openMenu();
    expect(screen.getByRole('combobox', { name: 'Field to reveal' })).toBeTruthy();
    expect(screen.getByRole('menuitem', { name: 'Show value' })).toBeTruthy();
  });

  it('reveals a selected field using its canonical record name', async () => {
    api.listVaultSecrets.mockResolvedValue({
      secrets: [{ ...protectedSecret('alpha'), default_field: 'password', fields: ['password', 'username'] }],
    });
    api.createVaultRevealContext.mockImplementation(async (_name, payload) => ({ ok: true, context: { field: payload.field }, envelope }));
    await openMenu();
    fireEvent.change(screen.getByRole('combobox', { name: 'Field to reveal' }), { target: { value: 'username' } });
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenLastCalledWith('alpha', { session_label: 'Vaults', field: 'username' }));
    await clickShowValue();
    expect(revealProtectedValue.mock.calls[0][0].name).toBe('alpha.username');
    expect(revealProtectedValue.mock.calls[0][1]).toEqual({ field: 'username' });
  });

  it('keeps a stale field handoff visible without preparing a default field', async () => {
    api.listVaultSecrets.mockResolvedValue({
      secrets: [{ ...protectedSecret('alpha'), default_field: 'password', fields: ['password'] }],
    });
    renderPage(remoteOwner, '/vaults?reveal=alpha&field=username&sort=recent');
    expect((await screen.findByRole('alert')).textContent).toBe('This link points to a field that no longer exists. Open the secret and choose a current field.');
    expect(api.createVaultRevealContext).not.toHaveBeenCalled();
    expect(revealProtectedValue).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByTestId('location').textContent).toContain('sort=recent'));
  });
  it('keeps Show value waiting while the context or the sandbox client are not ready', async () => {
    let answer: () => void = () => undefined;
    api.createVaultRevealContext.mockImplementation(
      () => new Promise((resolve) => (answer = () => resolve({ ok: true, context: { secret_name: 'alpha' }, envelope }))),
    );
    useVaultSandboxWarm.mockImplementation((enabled: boolean) => !enabled);

    await openMenu();
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenCalledOnce());
    expect(showValue().disabled).toBe(true);
    answer();
    await vi.waitFor(() => expect(useVaultSandboxWarm).toHaveBeenLastCalledWith(true));
    expect(showValue().disabled).toBe(true);

    useVaultSandboxWarm.mockImplementation(() => true);
    await clickShowValue();
    expect(api.createVaultRevealContext).toHaveBeenCalledOnce();
  });

  it('prepares a second secret without waiting on the first one', async () => {
    api.listVaultSecrets.mockResolvedValue({ secrets: [protectedSecret('alpha'), protectedSecret('beta')] });
    api.createVaultRevealContext.mockImplementation((name: string) =>
      name === 'alpha' ? new Promise(() => undefined) : Promise.resolve({ ok: true, context: { secret_name: name }, envelope }),
    );

    await openMenu();
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenCalledWith('alpha', expect.anything()));
    fireEvent.keyDown(document, { key: 'Escape' });
    await openMenu(1);
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenCalledWith('beta', expect.anything()));
    await clickShowValue();

    expect(revealProtectedValue.mock.calls[0][1]).toEqual({ secret_name: 'beta' });
  });

  it('refreshes the context while the menu stays open, so the click fetches nothing', async () => {
    let fetched = 0;
    api.createVaultRevealContext.mockImplementation(async () => ({ ok: true, context: { fetch: ++fetched }, envelope }));
    const opened = Date.now();
    const now = vi.spyOn(Date, 'now');

    await openMenu();
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenCalledOnce());
    now.mockReturnValue(opened + 20_000);
    vi.advanceTimersByTime(20_000);
    await vi.waitFor(() => expect(api.createVaultRevealContext).toHaveBeenCalledTimes(2));
    now.mockReturnValue(opened + 35_000);
    const { open } = await clickShowValue();

    expect(revealProtectedValue.mock.calls[0][1]).toEqual({ fetch: 2 });
    expect(api.createVaultRevealContext).toHaveBeenCalledTimes(2);
    for (const order of api.createVaultRevealContext.mock.invocationCallOrder) {
      expect(order).toBeLessThan(open.mock.invocationCallOrder[0]);
    }
  });
});
