/* @vitest-environment jsdom */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { ToastProvider } from '../../context/ToastProvider';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { SettingsMemoryPage } from './SettingsMemoryPage';

const translate = vi.hoisted(() =>
  (key: string, options?: { returnObjects?: boolean }) =>
    options?.returnObjects && key === 'memory.clear.removes'
      ? ['metadata', 'provider', 'native processing data', 'attachments']
      : options?.returnObjects && key === 'memory.clear.keeps'
        ? ['memory root', 'logs']
        : key,
);

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: translate }),
}));
vi.mock('../../context/ApiContext', () => ({
  useApi: (() => {
    const api = {
      getMemorySettings: vi.fn().mockResolvedValue({ status: 'failed', error: 'unavailable' }),
      getMemoryProcessingRecord: vi.fn().mockResolvedValue({ status: 'failed', error: 'unavailable' }),
      getMemoryMaintenance: vi.fn().mockResolvedValue({ status: 'failed', error: 'unavailable' }),
      listDependencies: vi.fn().mockResolvedValue({ deps: [] }),
    };
    return () => api;
  })(),
}));

describe('SettingsMemoryPage', () => {
  it('mounts the memory settings surface for an instance owner', () => {
    render(
      <InstanceAuthorizationContext.Provider value={{
        remote: false,
        instanceKind: null,
        instanceRole: 'owner',
        capabilities: OWNER_INSTANCE_CAPABILITIES,
      }}>
        <ToastProvider>
          <SettingsMemoryPage />
        </ToastProvider>
      </InstanceAuthorizationContext.Provider>,
    );
    expect(screen.getByRole('heading', { name: 'memory.title' })).toBeTruthy();
  });
});
