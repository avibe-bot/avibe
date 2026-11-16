// @vitest-environment jsdom

import { webcrypto } from 'node:crypto';
import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { VaultAgentBindingsBatchResult, VaultRequest, VaultRevealContextResult, VaultSecret, VaultSignedOperationContext } from '@/context/ApiContext';
import { InstanceAuthorizationContext } from '@/context/InstanceAuthorizationContext';
import { capabilitiesFor } from '@/lib/testing/instanceRoleCapabilities';
import { VaultApprovalCard } from '@/components/ui/vault-approval-card';
import { VaultRequestCard } from '@/components/ui/vault-request-card';
import { VaultSecretForm } from '@/components/ui/vault-secret-form';
import { VaultsPage } from '@/components/workbench/VaultsPage';
import en from '@/i18n/en.json';
import { vaultPasskeyHandoff } from '@/lib/vaultBrowserHandoff';
import { VaultSandboxError } from '@/lib/vaultSandboxClient';

const api = vi.hoisted(() => ({
  connectWorkbenchEvents: vi.fn(),
  createVaultAgentBindingsBatch: vi.fn(),
  createVaultGrant: vi.fn(),
  createVaultRevealContext: vi.fn(),
  denyVaultRequest: vi.fn(),
  fulfillVaultAccessRequest: vi.fn(),
  getAvaultAuthorization: vi.fn(async () => ({ ok: true, required: false, detected_at: null, attempt: null })),
  getVaultGrants: vi.fn(),
  getVaultRequests: vi.fn(),
  getVaultRequest: vi.fn(),
  getVaultSettings: vi.fn(),
  listDependencies: vi.fn(),
  listSkills: vi.fn(),
  listVaultSecrets: vi.fn(),
  signVaultDigest: vi.fn(),
}));
const useVaultSandboxWarm = vi.hoisted(() => vi.fn((_enabled: boolean) => true));
const vault = vi.hoisted(() => ({
  status: 'needs-setup',
  error: null,
  setError: vi.fn(),
  refresh: vi.fn(),
  lock: vi.fn(),
  hasPasskey: vi.fn(),
  passkeyUsableHere: vi.fn(),
  setupPasskey: vi.fn(),
  unlockPasskey: vi.fn(),
  sealValue: vi.fn(),
  approveProtectedRelease: vi.fn(),
  signProtectedRequest: vi.fn(),
  revealProtectedValue: vi.fn(),
}));

vi.mock('@/context/ApiContext', () => ({ useApi: () => api }));
const toast = vi.hoisted(() => vi.fn());
vi.mock('@/context/ToastContext', () => ({ useToast: () => ({ showToast: toast }) }));
vi.mock('@/lib/useProtectedVault', () => ({
  useProtectedVault: () => vault,
  useVaultLock: () => ({ unlocked: false, remainingMs: 0, lockNow: vi.fn() }),
  useVaultSandboxWarm,
  useProtectedHdSupported: () => null,
}));
// Workbench navigation, not part of the Vaults step (and it scrolls, which jsdom lacks).
vi.mock('@/components/workbench/CapabilityTabs', () => ({ CapabilityTabs: () => null }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const envelope = { ciphertext: 'c2VhbGVk', nonce: 'bm9uY2U=', wrap_meta: '{}' };
const pending = { requester: {}, status: 'pending', message_id: null, created_at: '2026-09-30T00:00:00Z', decided_at: null, expires_at: null };

const protectedAccess = {
  ...pending,
  id: 'vrq_access',
  request_type: 'access',
  secret_name: 'OPENAI_API_KEY',
  delivery: {},
  card: {
    request_type: 'access',
    protection: 'protected',
    secret_names: ['OPENAI_API_KEY'],
    protected_secret_names: ['OPENAI_API_KEY'],
    grant_options: [{ grant_id: 'grant_1', unlock_material: [{ name: 'OPENAI_API_KEY', kind: 'static', envelope }] }],
  },
} satisfies VaultRequest;

const protectedSign = {
  ...pending,
  id: 'vrq_sign',
  request_type: 'sign',
  secret_name: 'TREASURY_KEY',
  delivery: {
    digest: 'ab'.repeat(32),
    scheme: 'ecdsa-secp256k1-recoverable',
    signing_context: { chain_id: 1 },
    operation_context: { payload: 'e30', signature: 'c2ln' },
  },
  card: {
    request_type: 'sign',
    kind: 'keypair',
    protection: 'protected',
    secret_name: 'TREASURY_KEY',
    secret_unlock_material: { name: 'TREASURY_KEY', kind: 'keypair', envelope },
  },
} satisfies VaultRequest;

// The agent suggested Standard; the user answers on Protected. Shaped like the backend's
// `_secure_input_card`, which derives `default_protection` from the spec it also carries.
const standardSpec = { protection: 'standard' as const };
const standardProvision = {
  ...pending,
  id: 'vrq_provision',
  request_type: 'provision',
  secret_name: 'WALLET_PASSWORD',
  delivery: {},
  card: {
    card_type: 'secure_input',
    request_id: 'vrq_provision',
    secret_name: 'WALLET_PASSWORD',
    protection_options: ['standard', 'protected'],
    default_protection: 'standard',
    value: null,
    spec: standardSpec,
  },
} satisfies VaultRequest;

const protectedSecret: VaultSecret = {
  name: 'SEED_PHRASE',
  tags: [],
  kind: 'static',
  protection: 'protected',
  signer_kind: null,
  source: 'user',
  policy: {},
  last_used_at: null,
  use_count: 0,
  created_at: '2026-09-30T00:00:00Z',
  updated_at: '2026-09-30T00:00:00Z',
};

type Click = { role: 'button' | 'menuitem' | 'checkbox'; name: RegExp };
const approve: Click = { role: 'button', name: /^(approve|sign|continue in browser)$/i };
const protectedTier: Click = { role: 'button', name: /^protected/i };
const openInBrowser: Click = { role: 'button', name: /add a passkey|unlock with passkey|open in browser/i };
const acknowledgeLoss: Click = { role: 'checkbox', name: /if i lose this passkey/i };
const addPasskey: Click = { role: 'button', name: /^add a passkey$/i };
const unlockWithPasskey: Click = { role: 'button', name: /^unlock with passkey$/i };
const review: Click = { role: 'button', name: /^review$/i };
const moreActions: Click = { role: 'button', name: /^more actions$/i };
// What a shell without the capability says, and a shell with it must never say.
const cannotUsePasskeys = /desktop app can't use your passkey/i;
const approvalFallbackNote = en.vaults.approval.browserFallbackNote;
const unlockFallbackNote = en.vaults.protectedUnlock.browserFallbackNote;

const resumedDialog = () => screen.findByRole('dialog');
const expectApprovalButton = async (name: RegExp) => {
  const button = within(await resumedDialog()).getByRole('button', { name }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false));
  expect(useVaultSandboxWarm).toHaveBeenCalledWith(true);
  expect(vault.refresh).toHaveBeenCalled();
  if (name.test('approve')) {
    expect(api.createVaultAgentBindingsBatch).toHaveBeenCalledWith({ request_id: 'vrq_access', grant_duration: 300 });
  }
};
const expectPasskeyReady = async () => {
  const dialog = within(await resumedDialog());
  if (vault.status === 'needs-setup') {
    const add = dialog.getByRole('button', { name: /^add a passkey$/i }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    // Acknowledging loss risk enables setup, but is not the ceremony gesture.
    fireEvent.click(dialog.getByRole('checkbox'));
    expect(add.disabled).toBe(false);
  } else {
    expect((dialog.getByRole('button', { name: /^unlock with passkey$/i }) as HTMLButtonElement).disabled).toBe(false);
  }
  expect(vault.refresh).toHaveBeenCalled();
};
const expectProtectedForm = async (fixedName?: string) => {
  const dialog = within(await resumedDialog());
  expect(dialog.getByRole('button', { name: protectedTier.name }).getAttribute('aria-pressed')).toBe('true');
  if (fixedName) expect(dialog.getAllByText(fixedName).length).toBeGreaterThan(0);
  await expectPasskeyReady();
};
const expectRevealReady = async (field = 'value') => {
  const button = await screen.findByRole('menuitem', { name: /^show value$/i }) as HTMLButtonElement;
  await waitFor(() => expect(button.disabled).toBe(false));
  expect(api.createVaultRevealContext).toHaveBeenCalledWith('SEED_PHRASE', { session_label: 'Vaults', field });
  expect(useVaultSandboxWarm).toHaveBeenCalledWith(true);
};

// A new Bitcoin signing request as a list carries it (rev0.4 §4.3): no name, card, delivery or session.
const signSummary = {
  ...protectedSign,
  secret_name: null, requester: null, delivery: null, message_id: null, card: null, session: null,
} as unknown as VaultRequest;

// The surfaces under test. A page that must start from its own state renders a fresh copy of them.
type Ui = {
  VaultApprovalCard: typeof VaultApprovalCard;
  VaultRequestCard: typeof VaultRequestCard;
  VaultSecretForm: typeof VaultSecretForm;
  VaultsPage: typeof VaultsPage;
};
const staticUi: Ui = { VaultApprovalCard, VaultRequestCard, VaultSecretForm, VaultsPage };

const ceremonies = [vault.setupPasskey, vault.unlockPasskey, vault.sealValue, vault.approveProtectedRelease, vault.signProtectedRequest, vault.revealProtectedValue];

const cases: Array<{
  surface: string;
  desktop: (ui: Ui) => ReactElement;
  clicks: Click[];
  requests?: VaultRequest[];
  /** What the full read by id returns, when it differs from the listed request. */
  full?: VaultRequest[];
  secrets?: VaultSecret[];
  vaultStatus?: string;
  role?: 'editor' | 'member';
  resumed: () => Promise<void>;
  /** In a shell that runs passkey steps itself: the clicks to the step, and the ceremony they start. */
  inApp: { clicks: Click[]; ceremony: (typeof ceremonies)[number] };
  /** After that step failed in the app: the sentence the surface shows (a reveal says it in a toast)
   *  and the clicks to the surface's browser action for the same step. */
  fallbackNote?: string;
  afterFailure: Click[];
}> = [
  {
    surface: 'protected access approval',
    desktop: ({ VaultApprovalCard }) => <VaultApprovalCard request={protectedAccess} onResolved={vi.fn()} onCancel={vi.fn()} />,
    clicks: [approve],
    requests: [protectedAccess],
    resumed: () => expectApprovalButton(/^approve$/i),
    inApp: { clicks: [approve], ceremony: vault.approveProtectedRelease },
    afterFailure: [{ role: 'button', name: /^continue in browser$/i }],
    fallbackNote: approvalFallbackNote,
  },
  {
    surface: 'protected sign approval',
    desktop: ({ VaultApprovalCard }) => <VaultApprovalCard request={protectedSign} onResolved={vi.fn()} onCancel={vi.fn()} />,
    clicks: [approve],
    requests: [protectedSign],
    resumed: () => expectApprovalButton(/^sign$/i),
    inApp: { clicks: [approve], ceremony: vault.signProtectedRequest },
    afterFailure: [{ role: 'button', name: /^continue in browser$/i }],
    fallbackNote: approvalFallbackNote,
  },
  {
    // The summary itself says nothing about protection: Review reads it in full, and only then
    // does the dialog hand the protected request to the browser.
    surface: 'chat card for a proof-free signing summary',
    desktop: ({ VaultRequestCard }) => <VaultRequestCard request={signSummary} onResolved={vi.fn()} />,
    clicks: [{ role: 'button', name: /^review$/i }, { role: 'button', name: /^review in browser/i }],
    requests: [signSummary],
    full: [protectedSign as unknown as VaultRequest],
    resumed: () => expectApprovalButton(/^sign$/i),
    inApp: { clicks: [review, approve], ceremony: vault.signProtectedRequest },
    afterFailure: [{ role: 'button', name: /^review in browser/i }],
    fallbackNote: approvalFallbackNote,
  },
  {
    surface: 'chat request card review',
    desktop: ({ VaultRequestCard }) => <VaultRequestCard request={protectedAccess} onResolved={vi.fn()} />,
    clicks: [{ role: 'button', name: /^review/i }],
    requests: [protectedAccess],
    resumed: () => expectApprovalButton(/^approve$/i),
    inApp: { clicks: [review, approve], ceremony: vault.approveProtectedRelease },
    afterFailure: [{ role: 'button', name: /^review in browser/i }],
    fallbackNote: approvalFallbackNote,
  },
  {
    surface: 'Add switched to Protected',
    desktop: ({ VaultSecretForm }) => <VaultSecretForm onCancel={vi.fn()} onCreated={vi.fn()} />,
    clicks: [protectedTier, openInBrowser],
    resumed: () => expectProtectedForm(),
    inApp: { clicks: [protectedTier, acknowledgeLoss, addPasskey], ceremony: vault.setupPasskey },
    afterFailure: [{ role: 'button', name: /^open in browser$/i }],
    fallbackNote: unlockFallbackNote,
  },
  {
    surface: 'provision request answered on Protected over its Standard default',
    desktop: ({ VaultSecretForm }) => (
      <VaultSecretForm
        fixedName="WALLET_PASSWORD"
        provisionRequestId="vrq_provision"
        requestSpec={standardSpec}
        defaultProtection="standard"
        onCancel={vi.fn()}
        onCreated={vi.fn()}
      />
    ),
    clicks: [protectedTier, openInBrowser],
    requests: [standardProvision],
    vaultStatus: 'locked',
    resumed: () => expectProtectedForm('WALLET_PASSWORD'),
    inApp: { clicks: [protectedTier, unlockWithPasskey], ceremony: vault.unlockPasskey },
    afterFailure: [{ role: 'button', name: /^open in browser$/i }],
    fallbackNote: unlockFallbackNote,
  },
  {
    // An agent's `$WALLET_PASSWORD` ask whose request lookup found nothing: only the name identifies it.
    surface: 'secret an agent asked for by name, with no pending request',
    desktop: ({ VaultSecretForm }) => <VaultSecretForm fixedName="WALLET_PASSWORD" onCancel={vi.fn()} onCreated={vi.fn()} />,
    clicks: [protectedTier, openInBrowser],
    resumed: () => expectProtectedForm('WALLET_PASSWORD'),
    inApp: { clicks: [protectedTier, acknowledgeLoss, addPasskey], ceremony: vault.setupPasskey },
    afterFailure: [{ role: 'button', name: /^open in browser$/i }],
    fallbackNote: unlockFallbackNote,
  },
  {
    surface: 'protected secret reveal',
    desktop: ({ VaultsPage }) => <VaultsPage />,
    clicks: [
      { role: 'button', name: /^more actions$/i },
      { role: 'menuitem', name: /^show in browser$/i },
    ],
    secrets: [protectedSecret],
    resumed: () => expectRevealReady(),
    inApp: { clicks: [moreActions, { role: 'menuitem', name: /^show value$/i }], ceremony: vault.revealProtectedValue },
    afterFailure: [moreActions, { role: 'menuitem', name: /^show in browser$/i }],
  },
  {
    surface: 'protected field edit',
    role: 'member', // Editing is management-only; other handoffs remain Editor use.
    desktop: ({ VaultSecretForm }) => <VaultSecretForm editSecret={protectedSecret} onCancel={vi.fn()} onCreated={vi.fn()} />,
    clicks: [openInBrowser],
    secrets: [protectedSecret],
    vaultStatus: 'locked',
    resumed: async () => {
      expect(within(await resumedDialog()).getByLabelText('Value for value')).toBeTruthy();
      await expectPasskeyReady();
    },
    inApp: { clicks: [unlockWithPasskey], ceremony: vault.unlockPasskey },
    afterFailure: [{ role: 'button', name: /^open in browser$/i }],
    fallbackNote: unlockFallbackNote,
  },
];

const expectNoProtectedOperations = () => {
  for (const ceremony of ceremonies) expect(ceremony).not.toHaveBeenCalled();
  expect(api.fulfillVaultAccessRequest).not.toHaveBeenCalled();
  expect(api.signVaultDigest).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
};

// Shape-correct public fixtures, deliberately unsigned and never sent to a sandbox.
// Preparation must succeed so an enabled action, not a blocked entry, proves the wait for a click.
const fixtureContext = (purpose: 'agent-deliver' | 'reveal', name: string): VaultSignedOperationContext => ({
  v: 2, purpose, requestId: purpose === 'agent-deliver' ? protectedAccess.id : 'fixture-reveal',
  display: { secrets: [{ name, kind: 'static' }] },
  expiresAt: '2099-01-01T00:00:00Z',
  signature: { alg: 'ed25519', keyId: 'inert-fixture', value: 'not-a-signature' },
});

function renderAt(path: string, ui: ReactElement, role: 'editor' | 'member' = 'editor', Authorization = InstanceAuthorizationContext) {
  return render(
    <Authorization.Provider value={{
      remote: true, instanceKind: 'organization', instanceRole: role, capabilities: capabilitiesFor(role),
    }}>
      <I18nextProvider i18n={i18n}>
        <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
      </I18nextProvider>
    </Authorization.Provider>,
  );
}

beforeEach(() => {
  // Real entry assessment, supported loopback browser: never stub its verdict.
  vi.stubGlobal('location', new URL('http://127.0.0.1:5123'));
  vi.stubGlobal('isSecureContext', true);
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('fetch', vi.fn(() => { throw new Error('No network in a browser handoff test'); }));
  vault.status = 'needs-setup';
  vault.hasPasskey.mockReturnValue(true);
  vault.passkeyUsableHere.mockReturnValue(true);
  for (const call of Object.values(api)) call.mockResolvedValue({ ok: true, items: [], secrets: [], deps: [], skills: [] });
  api.connectWorkbenchEvents.mockReturnValue(() => undefined);
  api.getVaultGrants.mockResolvedValue({ grants: [] });
  api.getVaultSettings.mockResolvedValue({ ok: true, settings: { last_grant_ttl: 300 } });
  api.createVaultAgentBindingsBatch.mockResolvedValue({
    ok: true, request_id: protectedAccess.id, grant_id: 'grant_1', grant_duration: 300,
    agent_pubkey: { public_key: 'inert-public-key', fingerprint: 'inert-fingerprint' },
    items: [{ name: 'OPENAI_API_KEY', context: fixtureContext('agent-deliver', 'OPENAI_API_KEY'),
      approval: { nonce: 'inert-nonce', expires_at_unix: 4070908800 } }],
  } satisfies VaultAgentBindingsBatchResult);
  api.createVaultRevealContext.mockImplementation(async (name: string, { field }: { field: string }): Promise<VaultRevealContextResult> => ({
    ok: true, context: fixtureContext('reveal', `${name}.${field}`), envelope,
  }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __AVIBE_DESKTOP_VAULT_PASSKEY__?: true }).__AVIBE_DESKTOP_VAULT_PASSKEY__;
});

type Case = (typeof cases)[number];

function serve(c: Case) {
  vault.status = c.vaultStatus ?? 'needs-setup';
  api.getVaultRequests.mockResolvedValue({ requests: c.requests ?? [] });
  // A handed-off request is read in full by its id (rev0.4 §4.3).
  api.getVaultRequest.mockImplementation(async (id: string) => {
    const request = (c.full ?? c.requests ?? []).find((r: { id: string }) => r.id === id);
    return request ? { ok: true, request } : { ok: false, code: 'request_not_found' };
  });
  api.listVaultSecrets.mockResolvedValue({ secrets: c.secrets ?? [] });
}

function enterDesktopShell(inApp: boolean) {
  Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
  if (inApp) Object.defineProperty(window, '__AVIBE_DESKTOP_VAULT_PASSKEY__', { configurable: true, value: true });
}

function leaveDesktopShell() {
  delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
  delete (window as { __AVIBE_DESKTOP_VAULT_PASSKEY__?: true }).__AVIBE_DESKTOP_VAULT_PASSKEY__;
}

/** A desktop app without the passkey capability hands the step to the browser, where it resumes. */
async function handOffToBrowser(c: Case) {
  const user = userEvent.setup();
  serve(c);
  const open = vi.spyOn(window, 'open').mockReturnValue(null);

  enterDesktopShell(false);
  expect(vaultPasskeyHandoff()).toBe('desktop');
  renderAt('/vaults', c.desktop(staticUi), c.role);
  for (const click of c.clicks) await user.click(await screen.findByRole(click.role, { name: click.name }));

  expect(open).toHaveBeenCalledTimes(1);
  const target = new URL(String(open.mock.calls[0][0]));
  expect(target.origin).toBe(window.location.origin);
  expect(target.pathname).toBe('/vaults');
  expect(target.searchParams.get('from')).toBe('desktop');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(api.createVaultAgentBindingsBatch).not.toHaveBeenCalled();
  expect(api.createVaultRevealContext).not.toHaveBeenCalled();
  expect(useVaultSandboxWarm).not.toHaveBeenCalledWith(true);
  // The desktop shell must not even warm or refresh the protected sandbox before handing the
  // flow to the browser; WKWebView cannot pair with the sandbox authorization surface.
  expect(vault.refresh).not.toHaveBeenCalled();

  cleanup();
  leaveDesktopShell();
  renderAt(`${target.pathname}${target.search}`, <VaultsPage />, c.role);
  await c.resumed();

  expect(open).toHaveBeenCalledTimes(1);
  expectNoProtectedOperations();
}

// Contract: in the desktop shell, whose WKWebView can't run WebAuthn and whose new windows never
// pair with the sandbox, every surface that would start a protected passkey step instead opens the
// same Workbench's Vaults page in the browser and starts nothing here; the browser's Vaults page
// resumes that same step (the request, a Protected Add for the same name, or the secret's reveal)
// and stops at the click that begins it: it may prepare the step's signed contexts, but no passkey
// ceremony, release, or signature runs before that click. Each surface encodes its own step while Vaults decodes
// them all, so a step the page can't resume, or resumes with a lost part (a provision answer back
// on the request's Standard default, a reveal on a bare list, an agent's name dropped from Add),
// strands the user in the browser; only a round trip through both ends catches that.
// Positive controls must reach enabled setup/unlock/approval/reveal and its permitted preparation.
// Presence plus no ceremony used to pass even when the real entry assessment blocked every resume.
describe('desktop shell protected-vault browser handoff', () => {
  // Field labels must survive the same round trip as the secret name; reopening just the
  // secret silently changes an extra-field reveal into a default-field reveal.
  it('resumes the selected extra field in the browser without revealing anything automatically', async () => {
    const user = userEvent.setup();
    api.getVaultRequests.mockResolvedValue({ requests: [] });
    api.listVaultSecrets.mockResolvedValue({
      secrets: [{ ...protectedSecret, default_field: 'password', fields: ['password', 'username'] }],
    });
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    renderAt('/vaults', <VaultsPage />);
    await user.click(await screen.findByRole('button', { name: /^more actions$/i }));
    fireEvent.change(screen.getByRole('combobox', { name: 'Field to reveal' }), { target: { value: 'username' } });
    await user.click(screen.getByRole('menuitem', { name: /^show in browser$/i }));
    const target = new URL(String(open.mock.calls[0][0]));
    expect(target.searchParams.get('field')).toBe('username');
    expect(api.createVaultRevealContext).not.toHaveBeenCalled();
    expect(useVaultSandboxWarm).not.toHaveBeenCalledWith(true);
    expectNoProtectedOperations();
    cleanup();
    delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
    renderAt(`${target.pathname}${target.search}`, <VaultsPage />);
    expect((await screen.findByRole('combobox', { name: 'Field to reveal' }) as HTMLSelectElement).value).toBe('username');
    await expectRevealReady('username');
    expect(open).toHaveBeenCalledTimes(1);
    expectNoProtectedOperations();
  });

  it.each(cases)('$surface resumes in the browser without starting a passkey step', (c) => handOffToBrowser(c));

  // The browser tab is where the user lands after the step: once it is done there, the tab says so
  // and links back to the conversation the app handed it off from. An IM link is not a hand-off.
  it.each([
    { from: 'the desktop app', handoff: true },
    { from: 'an IM link', handoff: false },
  ])('a request answered in a tab opened from $from says the tab can be closed: $handoff', async ({ handoff }) => {
    const user = userEvent.setup();
    vault.status = 'locked';
    api.getVaultRequests.mockResolvedValue({ requests: [protectedAccess] });
    api.getVaultRequest.mockResolvedValue({ ok: true, request: protectedAccess });
    api.listVaultSecrets.mockResolvedValue({ secrets: [] });
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    vi.stubGlobal('location', new URL('http://127.0.0.1:5123/chat/ses_A-1'));
    Object.defineProperty(window, '__AVIBE_DESKTOP_SHELL__', { configurable: true, value: true });
    renderAt('/chat/ses_A-1', <VaultApprovalCard request={protectedAccess} onResolved={vi.fn()} onCancel={vi.fn()} />);
    await user.click(await screen.findByRole(approve.role, { name: approve.name }));
    const target = new URL(String(open.mock.calls[0][0]));
    expect(target.searchParams.get('session')).toBe('ses_A-1');
    cleanup();
    delete (window as { __AVIBE_DESKTOP_SHELL__?: true }).__AVIBE_DESKTOP_SHELL__;
    vi.stubGlobal('location', new URL('http://127.0.0.1:5123'));

    const path = handoff ? `${target.pathname}${target.search}` : `/vaults?request_id=${protectedAccess.id}`;
    renderAt(path, <VaultsPage />);
    await user.click(within(await resumedDialog()).getByRole('button', { name: /^deny$/i }));
    await waitFor(() => expect(api.denyVaultRequest).toHaveBeenCalledWith(protectedAccess.id));
    if (handoff) {
      const done = (await screen.findByText(en.vaults.handoff.done)).closest('[role="status"]') as HTMLElement;
      expect(done).toBeTruthy();
      expect(within(done).getByRole('link', { name: en.vaults.handoff.openApp }).getAttribute('href')).toBe('avibe://session/ses_A-1');
    } else {
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
      expect(screen.queryByText(en.vaults.handoff.done)).toBeNull();
    }
    expectNoProtectedOperations();
  });
});

// Contract: a shell that says it runs the sandbox's passkey steps itself gets the ordinary Web flow.
// Each surface starts its ceremony here, on the click that begins it, and opens nothing but the
// sandbox's own blank window (approvals, signatures and reveals; setup and unlock open none).
describe('desktop shell that runs protected-vault passkey steps in the app', () => {
  // The shell's contract: a blank window the sandbox may load, as in a browser.
  const openSandboxWindows = () => {
    const popup = { closed: false, location: { replace: vi.fn() }, close: vi.fn(() => { popup.closed = true; }) };
    return vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
  };
  const press = async (user: ReturnType<typeof userEvent.setup>, click: Click) => {
    const element = await screen.findByRole(click.role, { name: click.name }) as HTMLButtonElement | HTMLInputElement;
    await waitFor(() => expect(element.disabled).toBe(false));
    await user.click(element);
  };

  beforeEach(() => {
    vault.approveProtectedRelease.mockResolvedValue([{}]);
    vault.signProtectedRequest.mockResolvedValue({ signature: 'inert-signature' });
    vault.revealProtectedValue.mockResolvedValue({ completed: true });
    vault.setupPasskey.mockResolvedValue(undefined);
    vault.unlockPasskey.mockResolvedValue(undefined);
  });

  it.each(cases)('$surface starts its passkey step in the app, opening no browser', async (c) => {
    const user = userEvent.setup();
    serve(c);
    const open = openSandboxWindows();
    enterDesktopShell(true);
    expect(vaultPasskeyHandoff()).toBeNull();
    renderAt('/vaults', c.desktop(staticUi), c.role);
    for (const click of c.inApp.clicks) await press(user, click);

    await waitFor(() => expect(c.inApp.ceremony).toHaveBeenCalledTimes(1));
    expect(open.mock.calls.every(([url]) => url === 'about:blank')).toBe(true);
    expect(screen.queryByText(cannotUsePasskeys)).toBeNull();
  });

  // A window the user closed, or one the app declined to show because it was not in front, is not
  // something the user did wrong: the card says what happened and keeps its action for a retry.
  it('tells the user a closed secure window approved nothing and leaves Approve to try again', async () => {
    const user = userEvent.setup();
    const access = cases[0];
    serve(access);
    openSandboxWindows();
    enterDesktopShell(true);
    vault.approveProtectedRelease.mockRejectedValue(new VaultSandboxError('authorization_window_closed', 'authorization window was closed', true));
    renderAt('/vaults', access.desktop(staticUi), access.role);
    await press(user, approve);

    expect(await screen.findByText(en.errors.authorization_window_closed)).toBeTruthy();
    expect(screen.queryByText('authorization window was closed')).toBeNull();
    expect(screen.getByRole('button', { name: /^approve$/i })).toBeTruthy();
    expect(vaultPasskeyHandoff()).toBeNull();
  });

  // The hand-off after a failure lives for the page, so each case below is a new page: a fresh copy
  // of the surfaces and of the hand-off state they read.
  async function freshPage() {
    vi.resetModules();
    const [card, request, form, vaults, authorization, handoff, client] = await Promise.all([
      import('@/components/ui/vault-approval-card'),
      import('@/components/ui/vault-request-card'),
      import('@/components/ui/vault-secret-form'),
      import('@/components/workbench/VaultsPage'),
      import('@/context/InstanceAuthorizationContext'),
      import('@/lib/vaultBrowserHandoff'),
      import('@/lib/vaultSandboxClient'),
    ]);
    return {
      ui: { VaultApprovalCard: card.VaultApprovalCard, VaultRequestCard: request.VaultRequestCard, VaultSecretForm: form.VaultSecretForm, VaultsPage: vaults.VaultsPage },
      Authorization: authorization.InstanceAuthorizationContext,
      handoff: handoff.vaultPasskeyHandoff,
      // What the sandbox client does with how a step ended, before its caller sees the error.
      record: handoff.recordVaultPasskeyOutcome,
      VaultSandboxError: client.VaultSandboxError,
    };
  }
  type Page = Awaited<ReturnType<typeof freshPage>>;
  // The surface's ceremony ends as the client ends it: recorded, then thrown to the surface.
  const endsWith = (page: Page, ceremony: Case['inApp']['ceremony'], code: string) => {
    const error = new page.VaultSandboxError(code, 'sandbox failure');
    ceremony.mockImplementation(async () => {
      page.record(error);
      throw error;
    });
  };

  // Any failure other than a cancel: the surface says so in one sentence, offers its browser action
  // for the same step, and the browser resumes that step as it does for a shell without the capability.
  it.each(cases)('$surface continues in the browser on the same step after its passkey step fails in the app', async (c) => {
    const page = await freshPage();
    const user = userEvent.setup();
    serve(c);
    const open = openSandboxWindows();
    enterDesktopShell(true);
    endsWith(page, c.inApp.ceremony, 'authorization_popup_blocked');
    renderAt('/vaults', c.desktop(page.ui), c.role, page.Authorization);
    for (const click of c.inApp.clicks) await press(user, click);
    await waitFor(() => expect(c.inApp.ceremony).toHaveBeenCalledTimes(1));
    expect(page.handoff()).toBe('fallback');

    if (c.fallbackNote) expect(await screen.findByText(c.fallbackNote)).toBeTruthy();
    else await waitFor(() => expect(toast).toHaveBeenCalledWith(en.vaults.reveal.browserFallback, 'warning'));
    // The sentence is the message: the sandbox error is not shown beside it, here or in the vault's own error line.
    expect(screen.queryByText('sandbox failure')).toBeNull();
    expect(vault.setError.mock.calls.filter(([message]) => message != null)).toEqual([]);
    expect(screen.queryByText(cannotUsePasskeys)).toBeNull();

    // From here on nothing more may start in the app.
    for (const ceremony of ceremonies) ceremony.mockClear();
    vi.mocked(fetch).mockClear();
    for (const click of c.afterFailure) await press(user, click);
    const handedOff = open.mock.calls.map(([url]) => String(url)).filter((url) => url !== 'about:blank');
    expect(handedOff).toHaveLength(1);
    const target = new URL(handedOff[0]);
    expect(target.origin).toBe(window.location.origin);
    expect(target.pathname).toBe('/vaults');
    expect(target.searchParams.get('from')).toBe('desktop');
    expectNoProtectedOperations();

    cleanup();
    leaveDesktopShell();
    renderAt(`${target.pathname}${target.search}`, <VaultsPage />, c.role);
    await c.resumed();
    expectNoProtectedOperations();
  });

  it('keeps a cancelled step in the app, with nothing sent to the browser', async () => {
    const page = await freshPage();
    const user = userEvent.setup();
    const access = cases[0];
    serve(access);
    const open = openSandboxWindows();
    enterDesktopShell(true);
    endsWith(page, vault.approveProtectedRelease, 'authorization_cancelled');
    renderAt('/vaults', access.desktop(page.ui), access.role, page.Authorization);
    await press(user, approve);
    await waitFor(() => expect(vault.approveProtectedRelease).toHaveBeenCalledTimes(1));

    expect(page.handoff()).toBeNull();
    expect(await screen.findByRole('button', { name: /^approve$/i })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /continue in browser/i })).toBeNull();
    expect(screen.queryByText(approvalFallbackNote)).toBeNull();
    expect(open.mock.calls.every(([url]) => url === 'about:blank')).toBe(true);
  });

  // Approvals that never ask for the passkey still run here, and their own errors still show.
  it('keeps showing errors of approvals that need no passkey', async () => {
    const page = await freshPage();
    const user = userEvent.setup();
    const standardAccess = {
      ...protectedAccess,
      id: 'vrq_standard',
      card: { request_type: 'access', protection: 'standard', secret_names: ['GITHUB_TOKEN'], grant_options: [{ grant_id: 'grant_std' }] },
    } as unknown as VaultRequest;
    api.createVaultGrant.mockResolvedValue({ ok: false, code: 'grant_refused', message: 'grant refused' });
    enterDesktopShell(true);
    page.record(new page.VaultSandboxError('authorization_popup_blocked', 'sandbox failure'));
    expect(page.handoff()).toBe('fallback');
    renderAt('/vaults', <page.ui.VaultApprovalCard request={standardAccess} onResolved={vi.fn()} onCancel={vi.fn()} />, 'editor', page.Authorization);
    await press(user, { role: 'button', name: /^approve$/i });
    expect(await screen.findByText('grant refused')).toBeTruthy();
  });
});
