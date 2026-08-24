/* @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { MemoryStatus } from '../../../context/ApiContext';
import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { MemoryStatusPanel } from './MemoryStatusPanel';

const renderStatus = (language: 'en' | 'zh', state: MemoryStatus['state'], reason: string | null) => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng: language,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  const status: MemoryStatus = {
    status: 'ok',
    state,
    reason,
    source: { status: 'unavailable', observed_at: null, reason },
    health: null,
  };

  render(
    <I18nextProvider i18n={i18n}>
      <MemoryStatusPanel
        status={status}
        failures={[]}
        logSections={null}
        statusLoading={false}
        failuresLoading={false}
        statusError={null}
        failuresError={null}
        refreshPending={false}
        onRefresh={vi.fn()}
      />
    </I18nextProvider>,
  );
};

afterEach(cleanup);

describe('MemoryStatusPanel localized recovery contract', () => {
  it.each([
    ['en', 'Needs repair', 'The confined local Memory data root is unusable or incompatible and needs Repair.'],
    ['zh', '需要修复', '受限的本地记忆数据根不可用或不兼容，需要修复。'],
  ] as const)('localizes needs_repair and its confined-root reason in %s', (language, state, reason) => {
    renderStatus(language, 'needs_repair', 'memory_local_data_unusable');
    expect(screen.getByText(state)).toBeTruthy();
    expect(screen.getAllByText(reason).length).toBeGreaterThan(0);
  });

  it.each([
    ['en', 'Memory cannot access its local data path. Fix the path permissions; Repair is not available for this fault.'],
    ['zh', '记忆无法访问本地数据路径。请修复路径权限；此故障不提供修复操作。'],
  ] as const)('keeps permission failures degraded and non-repairable in %s', (language, reason) => {
    renderStatus(language, 'degraded', 'memory_permission_denied');
    expect(screen.getAllByText(reason).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: /Repair Memory|修复记忆/ })).toBeNull();
  });
});
