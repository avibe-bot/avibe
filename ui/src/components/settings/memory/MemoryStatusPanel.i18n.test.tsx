/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, render, screen } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { MemoryClearInProgress, MemoryFailureLogEntry } from '../../../context/ApiContext';
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
    id: 'ma_3333333333333333333333333333333333333333333333333333333333333333',
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
        clearInProgress={null}
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

const renderUnobservedSource = (language: 'en' | 'zh') => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng: language,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });

  render(
    <I18nextProvider i18n={i18n}>
      <MemoryStatusPanel
        status={null}
        failures={[]}
        clearInProgress={null}
        logSections={{
          everos: { status: 'available', observed_at: null },
          capture: { status: 'unavailable', observed_at: null, reason: 'missing' },
          calls: { status: 'unavailable', observed_at: null, reason: 'missing' },
        }}
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

const renderClearState = (language: 'en' | 'zh', errorCode: string) => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng: language,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  const clearInProgress: MemoryClearInProgress = {
    state: 'failed',
    operation_id: 'clear-i18n',
    occurred_at: '2026-08-09T12:00:00Z',
    error_code: errorCode,
  };
  render(
    <I18nextProvider i18n={i18n}>
      <MemoryStatusPanel
        status={null}
        failures={[]}
        clearInProgress={clearInProgress}
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

describe('MemoryStatusPanel anomaly error localization', () => {
  it.each([
    ['en', 'memory_provider_timeout', en.errors.memory_provider_timeout],
    ['zh', 'memory_processing_failed', zh.errors.memory_processing_failed],
    ['en', 'memory_clear_marker_unreadable', en.errors.memory_clear_marker_unreadable],
    ['zh', 'memory_clear_legacy_state_requires_rerun', zh.errors.memory_clear_legacy_state_requires_rerun],
    ['zh', 'future_provider_failure', 'future_provider_failure'],
  ] as const)('renders %s error %s as %s', (language, errorCode, expected) => {
    renderAnomaly(language, errorCode);

    expect(screen.getByText(expected)).toBeTruthy();
  });

  it.each([
    ['en', en.memory.processingRecord.clearInProgress.explicitRetryDescription],
    ['zh', zh.memory.processingRecord.clearInProgress.explicitRetryDescription],
  ] as const)('describes legacy state recovery as an explicit retry in %s', (language, expected) => {
    renderClearState(language, 'memory_clear_legacy_state_requires_rerun');

    expect(screen.getByText(expected)).toBeTruthy();
  });

  it.each([
    ['en', en.memory.processingRecord.clearInProgress.explicitRetryDescription],
    ['zh', zh.memory.processingRecord.clearInProgress.explicitRetryDescription],
  ] as const)('describes unreadable marker repair as an explicit retry in %s', (language, expected) => {
    renderClearState(language, 'memory_clear_marker_unreadable');

    expect(screen.getByText(expected)).toBeTruthy();
  });

  it.each([
    ['en', en.memory.processingRecord.clearInProgress.explicitRetryDescription],
    ['zh', zh.memory.processingRecord.clearInProgress.explicitRetryDescription],
  ] as const)('describes an ordinary failed Clear as an explicit retry in %s', (language, expected) => {
    renderClearState(language, 'memory_clear_failed');

    expect(screen.getByText(expected)).toBeTruthy();
  });

  it.each([
    ['en', en.memory.processingRecord.sourceState.unknown, en.memory.processingRecord.sourceNotObserved],
    ['zh', zh.memory.processingRecord.sourceState.unknown, zh.memory.processingRecord.sourceNotObserved],
  ] as const)('localizes unobserved source evidence in %s', (language, state, timestamp) => {
    renderUnobservedSource(language);

    expect(screen.getAllByText(state).length).toBeGreaterThan(0);
    expect(screen.getAllByText(timestamp).length).toBeGreaterThan(0);
  });
});
