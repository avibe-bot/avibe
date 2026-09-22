/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import { RemoteAccess } from './RemoteAccess';
import type { RemoteAccessStatus } from '../context/ApiContext';
import { InstanceAuthorizationProvider } from '../context/InstanceAuthorizationProvider';
import { normalizeSessionInfo, OWNER_INSTANCE_CAPABILITIES } from '../lib/sessionInfo';

const api = vi.hoisted(() => ({
  connectWorkbenchEvents: vi.fn(() => () => undefined),
  diagnoseRemoteAccess: vi.fn(),
  getRemoteAccessNetworkInterfaces: vi.fn(),
  optimizeRemoteAccessRoute: vi.fn(),
  pairVibeCloudRemoteAccess: vi.fn(),
  remoteAccessStatus: vi.fn(),
  saveRemoteAccessSettings: vi.fn(),
  startRemoteAccess: vi.fn(),
  stopRemoteAccess: vi.fn(),
}));
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../context/ApiContext', async (loadOriginal) => {
  const original = await loadOriginal<typeof import('../context/ApiContext')>();
  return { ...original, useApi: () => api };
});

vi.mock('../context/ToastContext', () => ({
  useToast: () => ({ showToast }),
}));

vi.mock('react-i18next', () => ({
  Trans: ({ i18nKey }: { i18nKey: string }) => <span>{i18nKey}</span>,
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const runningStatus = (overrides: Partial<RemoteAccessStatus> = {}): RemoteAccessStatus => ({
  ok: true,
  enabled: true,
  paired: true,
  running: true,
  public_url: 'https://alex.avibe.bot',
  pid_state: 'cloudflared',
  transport_protocol: 'http2',
  settings: {
    transport_protocol: 'http2',
    auto_recovery: true,
    optimization_profile: 'balanced',
    edge_ip_version: '4',
    edge_bind_address: '',
  },
  network_path: {
    schema_version: 1,
    provider: 'Cloudflare',
    asn: 13335,
    sampled_at: '2026-08-14T08:00:00Z',
    locations_pending: false,
    client_access: 'remote',
    client_ingress: { colo: 'SIN', location: 'Singapore' },
    connector: {
      locations: [{ id: 'sin09', colo: 'SIN', location: 'Singapore' }],
      edge_ips: ['198.41.192.47'],
    },
    route: { assessment: 'same_metro' },
  },
  ...overrides,
});

function renderPage(role: 'member' | 'owner' = 'owner') {
  const session = normalizeSessionInfo({
    remote: true, authenticated: true, authorization_state: 'current',
    instance_role: role, instance_kind: 'organization',
    capabilities: {
      ...OWNER_INSTANCE_CAPABILITIES,
      is_instance_owner: role === 'owner',
      can_manage_access_members: role === 'owner',
    },
  });
  return render(<InstanceAuthorizationProvider session={session}><RemoteAccess /></InstanceAuthorizationProvider>);
}

describe('RemoteAccess', () => {
  beforeEach(() => {
    api.pairVibeCloudRemoteAccess.mockReset();
    api.connectWorkbenchEvents.mockReturnValue(() => undefined);
    api.getRemoteAccessNetworkInterfaces.mockResolvedValue({ ok: true, interfaces: [] });
    api.remoteAccessStatus.mockResolvedValue(runningStatus());
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('RA-TQ-032 shows technical details when the remote status includes a network path', async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText('remoteAccess.networkTechnicalDetails')).toBeTruthy();
    });
    expect(screen.getByText('remoteAccess.networkPath')).toBeTruthy();
    expect(screen.getByText('remoteAccess.controls')).toBeTruthy();
  });

  it('hides technical details when the remote projection omits the network path', async () => {
    api.remoteAccessStatus.mockResolvedValue(runningStatus({ network_path: undefined }));
    renderPage();

    await waitFor(() => {
      expect(screen.getByText('remoteAccess.controls')).toBeTruthy();
    });
    expect(screen.queryByText('remoteAccess.networkTechnicalDetails')).toBeNull();
    expect(screen.queryByText('remoteAccess.networkPath')).toBeNull();
  });
  it('PERMISSIONS-018 keeps Member transport usable while pairing is disabled', async () => {
    api.stopRemoteAccess.mockResolvedValue(runningStatus({ running: false }));
    api.startRemoteAccess.mockResolvedValue(runningStatus());
    api.optimizeRemoteAccessRoute.mockResolvedValue(runningStatus());
    renderPage('member');
    const repair = await screen.findByRole('button', { name: 'remoteAccess.repair' });
    expect((repair as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(repair);
    expect(screen.queryByLabelText('remoteAccess.pairingKey')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'remoteAccess.optimizeRoute' }));
    await waitFor(() => expect(api.optimizeRemoteAccessRoute).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole('button', { name: 'common.stop' }));
    await waitFor(() => expect(api.stopRemoteAccess).toHaveBeenCalledOnce());
    await waitFor(() => expect((screen.getByRole('button', { name: 'common.start' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'common.start' }));
    await waitFor(() => expect(api.startRemoteAccess).toHaveBeenCalledOnce());
    expect(api.pairVibeCloudRemoteAccess).not.toHaveBeenCalled();
  });

  it('PERMISSIONS-018 explains unpaired Member access without a pair request', async () => {
    api.remoteAccessStatus.mockResolvedValue(runningStatus({ paired: false, running: false }));
    renderPage('member');
    await screen.findByText('remoteAccess.ownerPairingRequired');
    const input = screen.getByLabelText('remoteAccess.pairingKey');
    expect((input as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(input, { target: { value: 'synthetic-key' } });
    const pair = screen.getByRole('button', { name: 'remoteAccess.pair' });
    expect((pair as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(pair);
    expect(api.pairVibeCloudRemoteAccess).not.toHaveBeenCalled();
  });

  it.each([true, false])('PERMISSIONS-018 preserves Owner pairing (paired=%s)', async (paired) => {
    api.remoteAccessStatus.mockResolvedValue(runningStatus({ paired, running: paired }));
    api.pairVibeCloudRemoteAccess.mockResolvedValue(runningStatus());
    renderPage('owner');
    if (paired) fireEvent.click(await screen.findByRole('button', { name: 'remoteAccess.repair' }));
    const input = await screen.findByLabelText('remoteAccess.pairingKey');
    fireEvent.change(input, { target: { value: 'synthetic-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'remoteAccess.pair' }));
    await waitFor(() => expect(api.pairVibeCloudRemoteAccess).toHaveBeenCalledWith({
      backend_url: 'https://avibe.bot', pairing_key: 'synthetic-key', device_name: 'avibe',
    }));
  });

  it('recovers a failed save without submitting the consumed key again', async () => {
    const unpaired = runningStatus({ paired: false, running: false });
    api.remoteAccessStatus.mockResolvedValue(unpaired);
    api.pairVibeCloudRemoteAccess.mockImplementationOnce(async () => {
      api.remoteAccessStatus.mockResolvedValue({
        ...unpaired, pending_pairing: { phase: 'redeemed', can_resume: true },
      });
      throw new Error('pairing_save_failed_after_redeem');
    }).mockImplementationOnce(async () => {
      api.remoteAccessStatus.mockResolvedValue(runningStatus());
      return runningStatus();
    });
    renderPage();
    fireEvent.change(await screen.findByLabelText('remoteAccess.pairingKey'), { target: { value: 'one-time-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'remoteAccess.pair' }));
    const resume = await screen.findByRole('button', { name: 'remoteAccess.resumePairing' });
    await waitFor(() => expect((resume as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByLabelText('remoteAccess.pairingKey')).toBeNull();
    fireEvent.click(resume);
    await waitFor(() => expect(api.pairVibeCloudRemoteAccess).toHaveBeenCalledTimes(2));
    expect(api.pairVibeCloudRemoteAccess.mock.calls.map(([payload]) => payload.pairing_key)).toEqual(['one-time-key', '']);
    await waitFor(() => expect(screen.queryByRole('button', { name: 'remoteAccess.resumePairing' })).toBeNull());
  });

  it.each(['redeemed', 'applied', 'retirement_pending'] as const)(
    'offers keyless %s recovery on a fresh page even when already paired',
    async (phase) => {
      api.remoteAccessStatus.mockResolvedValue(runningStatus({ pending_pairing: { phase, can_resume: true } }));
      api.pairVibeCloudRemoteAccess.mockResolvedValue(runningStatus());
      renderPage();
      fireEvent.click(await screen.findByRole('button', {
        name: phase === 'retirement_pending' ? 'remoteAccess.clearFailedPairing' : 'remoteAccess.resumePairing',
      }));
      await waitFor(() => expect(api.pairVibeCloudRemoteAccess).toHaveBeenCalledWith({
        pairing_key: '', backend_url: 'https://avibe.bot', device_name: 'avibe',
      }));
    },
  );

  it('requires an explicit new-key action to replace recoverable credentials', async () => {
    api.remoteAccessStatus.mockResolvedValue(runningStatus({
      paired: false, running: false, pending_pairing: { phase: 'redeemed', can_resume: true },
    }));
    api.pairVibeCloudRemoteAccess.mockResolvedValue(runningStatus());
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'remoteAccess.replacePairing' }));
    expect(screen.getByText('remoteAccess.replacePairingWarning')).toBeTruthy();
    const input = screen.getByLabelText('remoteAccess.pairingKey');
    expect((input as HTMLInputElement).value).toBe('');
    fireEvent.change(input, { target: { value: 'fresh-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'remoteAccess.pair' }));
    await waitFor(() => expect(api.pairVibeCloudRemoteAccess.mock.calls[0][0].pairing_key).toBe('fresh-key'));
  });

  it.each(['prepared', 'revoked', 'invalid'] as const)('does not offer local replay for %s', async (phase) => {
    api.remoteAccessStatus.mockResolvedValue(runningStatus({ pending_pairing: { phase, can_resume: false } }));
    renderPage();
    await screen.findByText('remoteAccess.pendingIndeterminate');
    expect(screen.queryByRole('button', { name: 'remoteAccess.resumePairing' })).toBeNull();
    expect((screen.getByLabelText('remoteAccess.pairingKey') as HTMLInputElement).disabled).toBe(false);
  });

  it('clears the submitted key and blocks pairing until a failed status refresh is repaired', async () => {
    const unpaired = runningStatus({ paired: false, running: false });
    api.remoteAccessStatus.mockResolvedValue(unpaired);
    api.pairVibeCloudRemoteAccess.mockImplementationOnce(async () => {
      api.remoteAccessStatus.mockRejectedValue(new Error('status unavailable'));
      throw new Error('request interrupted');
    });
    renderPage();
    fireEvent.change(await screen.findByLabelText('remoteAccess.pairingKey'), { target: { value: 'consumed-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'remoteAccess.pair' }));
    await screen.findByText('remoteAccess.pairingStatusUnavailable');
    const input = screen.getByLabelText('remoteAccess.pairingKey') as HTMLInputElement;
    expect(input.value).toBe('');
    expect(input.disabled).toBe(true);
    expect(api.pairVibeCloudRemoteAccess).toHaveBeenCalledOnce();
    api.remoteAccessStatus.mockResolvedValue({
      ...unpaired, pending_pairing: { phase: 'redeemed', can_resume: true },
    });
    fireEvent.click(screen.getByRole('button', { name: 'common.refresh' }));
    await screen.findByRole('button', { name: 'remoteAccess.resumePairing' });
  });

  it('never exposes recovery controls to Members even with an owner-shaped payload', async () => {
    api.remoteAccessStatus.mockResolvedValue(runningStatus({
      pending_pairing: { phase: 'redeemed', can_resume: true },
    }));
    renderPage('member');
    await screen.findByRole('button', { name: 'remoteAccess.repair' });
    expect(screen.queryByRole('button', { name: 'remoteAccess.resumePairing' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'remoteAccess.replacePairing' })).toBeNull();
    expect(api.pairVibeCloudRemoteAccess).not.toHaveBeenCalled();
  });

});
