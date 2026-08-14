/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';

import { RemoteAccess } from './RemoteAccess';
import type { RemoteAccessStatus } from '../context/ApiContext';

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

function renderPage() {
  return render(<RemoteAccess />);
}

describe('RemoteAccess', () => {
  beforeEach(() => {
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
});
