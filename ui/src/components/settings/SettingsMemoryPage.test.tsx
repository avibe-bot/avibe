/* @vitest-environment jsdom */

import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import { ToastProvider } from '../../context/ToastProvider';
import { SettingsMemoryPage } from './SettingsMemoryPage';

const translate = vi.hoisted(() =>
  (key: string, options?: { returnObjects?: boolean }) =>
    options?.returnObjects && key === 'memory.clear.removes'
      ? ['queue', 'provider', 'call log', 'attachments']
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
  it('mounts the memory settings surface', () => {
    render(
      <ToastProvider>
        <SettingsMemoryPage />
      </ToastProvider>,
    );
    expect(screen.getByRole('heading', { name: 'memory.title' })).toBeTruthy();
  });
});
