/** @vitest-environment jsdom */

import { act, cleanup, renderHook } from '@testing-library/react';
import { createInstance } from 'i18next';
import { createElement, type ReactNode } from 'react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ModelRecoveryState } from '../context/ApiContext';
import en from '../i18n/en.json';
import zh from '../i18n/zh.json';
import { useModelHubRecovery } from './modelHubRecovery';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  resources: { en: { translation: en }, zh: { translation: zh } },
  interpolation: { escapeValue: false },
});
const wrapper = ({ children }: { children: ReactNode }) => createElement(I18nextProvider, { i18n }, children);
const start = Date.parse('2026-09-09T04:00:00Z');
const progress = (overrides: Partial<ModelRecoveryState> = {}): ModelRecoveryState => ({
  request_id: 'request-1',
  phase: 'waiting',
  attempt_count: 1,
  started_at: new Date(start).toISOString(),
  window_end: new Date(start + 120_000).toISOString(),
  source_id: 'private-source',
  reason: 'https://private-host.invalid/diagnostic',
  next_eligible_at: new Date(start + 30_000).toISOString(),
  ...overrides,
});
const mount = (snapshot: unknown = [progress()], working = true) => renderHook(
  ({ snapshot, working }) => useModelHubRecovery(snapshot, working),
  { initialProps: { snapshot, working }, wrapper },
);

beforeEach(async () => {
  vi.useFakeTimers();
  vi.setSystemTime(start);
  await i18n.changeLanguage('en');
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('quiet Model Hub recovery label', () => {
  it('keeps the generic indicator for five seconds and clears a short recovery silently', () => {
    const { result, rerender } = mount();
    expect(result.current).toEqual({ active: true, label: null });
    act(() => vi.advanceTimersByTime(4999));
    expect(result.current.label).toBeNull();
    rerender({ snapshot: [], working: true });
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current).toEqual({ active: false, label: null });
  });

  it('reveals at server start + 5s when a reload lands between display ticks', () => {
    vi.setSystemTime(start + 3250);
    const { result } = mount();
    act(() => vi.advanceTimersByTime(1749));
    expect(result.current.label).toBeNull();
    act(() => vi.advanceTimersByTime(1));
    expect(result.current.label).toBe('Waiting to retry in about 25s');
  });

  it('restores a mature wait immediately on reload and keeps its debounce through updates', () => {
    vi.setSystemTime(start + 6000);
    const { result, rerender } = mount();
    expect(result.current.label).toBe('Waiting to retry in about 24s');
    rerender({
      snapshot: [progress({ attempt_count: 2, next_eligible_at: new Date(start + 40_000).toISOString() })],
      working: true,
    });
    expect(result.current.label).toBe('Waiting to retry in about 34s');
  });

  it('MH-RETRY-WEB-001: never infers an attempt, recovery, or end from either deadline expiring', () => {
    vi.setSystemTime(start + 29_000);
    const { result, rerender } = mount();
    expect(result.current.label).toBe('Waiting to retry in about 1s');
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current.label).toBe('Waiting to retry…');
    act(() => vi.advanceTimersByTime(100_000));
    expect(result.current).toEqual({ active: true, label: 'Waiting to retry…' });
    rerender({ snapshot: [progress({ phase: 'attempting' })], working: true });
    expect(result.current.label).toBe('Trying again…');
    act(() => vi.advanceTimersByTime(120_000));
    expect(result.current.label).toBe('Trying again…');
  });

  it('clears immediately on server clear or idle and debounces a new request from its own start', () => {
    vi.setSystemTime(start + 6000);
    const { result, rerender } = mount();
    rerender({ snapshot: [], working: true });
    expect(result.current.label).toBeNull();
    rerender({ snapshot: [progress()], working: false });
    expect(result.current).toEqual({ active: false, label: null });
    rerender({ snapshot: [progress({ request_id: 'new', started_at: new Date(Date.now()).toISOString() })], working: true });
    expect(result.current).toEqual({ active: true, label: null });
    act(() => vi.advanceTimersByTime(5000));
    expect(result.current.label).toBe('Waiting to retry in about 19s');
  });

  it('keeps one stable oldest-request label while peer requests are added, reordered, and removed', () => {
    vi.setSystemTime(start + 10_000);
    const oldest = progress();
    const peer = progress({ request_id: 'peer', phase: 'attempting', started_at: new Date(start + 1000).toISOString() });
    const { result, rerender } = mount([peer, oldest]);
    expect(result.current.label).toBe('Waiting to retry in about 20s');
    rerender({ snapshot: [oldest, peer], working: true });
    expect(result.current.label).toBe('Waiting to retry in about 20s');
    rerender({ snapshot: [peer], working: true });
    expect(result.current.label).toBe('Trying again…');
  });

  it.each([
    undefined, null, {}, [null], ['waiting'],
    [{ request_id: 'incomplete' }],
    [progress({ phase: 'recovered' as 'waiting' })],
    [progress({ request_id: '' })],
    [progress({ attempt_count: -1 })],
    [progress({ attempt_count: 1.5 })],
    [progress({ attempt_count: '2' as unknown as number })],
    [progress({ started_at: 'bad' })],
    [progress({ started_at: '2026-02-30T00:00:00Z' })],
    [progress({ started_at: '2026-09-09T04:00:00' })],
    [progress({ window_end: 'bad' })],
    [progress({ window_end: new Date(start - 1000).toISOString() })],
  ])('leaves malformed recovery to the existing generic indicator: %j', (snapshot) => {
    vi.setSystemTime(start + 6000);
    const { result, rerender } = mount();
    rerender({ snapshot, working: true });
    expect(result.current).toEqual({ active: false, label: null });
  });

  it.each([null, 'invalid', '2026-09-09T04:00:00', 100, {}])(
    'uses generic waiting for unavailable/malformed advisory time: %j',
    (next_eligible_at) => {
      vi.setSystemTime(start + 6000);
      const { result } = mount([{ ...progress(), next_eligible_at }]);
      expect(result.current.label).toBe('Waiting to retry…');
    },
  );

  it('accepts UTC offsets and fractional precision from the controller, ignoring malformed peers', () => {
    vi.setSystemTime(start + 6000);
    const { result } = mount([null, progress({
      started_at: '2026-09-09T04:00:00.000000+00:00',
      window_end: '2026-09-09T04:02:00.000000+00:00',
      next_eligible_at: '2026-09-09T04:00:30.000000+00:00',
    })]);
    expect(result.current.label).toBe('Waiting to retry in about 24s');
    expect(result.current.label).not.toMatch(/private|https|2026|UTC/);
  });

  it('renders localized waiting and attempting without diagnostic fields', async () => {
    await i18n.changeLanguage('zh');
    vi.setSystemTime(start + 6000);
    const { result, rerender } = mount();
    expect(result.current.label).toBe('等待重试，预计 24 秒');
    rerender({ snapshot: [progress({ next_eligible_at: null })], working: true });
    expect(result.current.label).toBe('等待重试…');
    rerender({ snapshot: [progress({ phase: 'attempting' })], working: true });
    expect(result.current.label).toBe('正在重试…');
  });

  it('catches up after a suspended tab without restarting the debounce', () => {
    const { result } = mount();
    vi.setSystemTime(start + 8000);
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    expect(result.current.label).toBe('Waiting to retry in about 22s');
  });
});
