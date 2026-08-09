/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { MemoryFailureLogEntry } from '../../../context/ApiContext';
import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { MemoryStatusPanel } from './MemoryStatusPanel';

const renderAnomaly = (language: 'en' | 'zh', errorCode: string) => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng: language,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  const failure: MemoryFailureLogEntry = {
    kind: 'result_unknown',
    state: 'manual_required',
    operation: 'add',
    occurred_at: '2026-08-09T12:00:00Z',
    error_code: errorCode,
    attempts: 1,
    generation: 2,
    request_id: 'request-i18n',
  };

  render(
    <I18nextProvider i18n={i18n}>
      <MemoryStatusPanel
        status={null}
        failures={[failure]}
        recovery={null}
        logSections={null}
        statusLoading={false}
        failuresLoading={false}
        statusError={null}
        failuresError={null}
        refreshPending={false}
        recoveryAction={null}
        onRefresh={vi.fn()}
        onResumeClear={vi.fn()}
        onAbortClear={vi.fn()}
      />
    </I18nextProvider>,
  );
};

afterEach(cleanup);

describe('MemoryStatusPanel anomaly error localization', () => {
  it.each([
    ['en', 'memory_provider_timeout', en.errors.memory_provider_timeout],
    ['zh', 'memory_processing_failed', zh.errors.memory_processing_failed],
    ['zh', 'future_provider_failure', 'future_provider_failure'],
  ] as const)('renders %s error %s as %s', (language, errorCode, expected) => {
    renderAnomaly(language, errorCode);

    expect(screen.getByText(expected)).toBeTruthy();
  });
});
