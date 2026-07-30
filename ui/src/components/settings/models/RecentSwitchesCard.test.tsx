// AC-18: the feed is a historical record. Its sentence is whatever was recorded;
// the only thing the UI adds is a render-time 已删除 observation when a source the
// sentence names no longer resolves.
import { createInstance } from 'i18next';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { describe, expect, it } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { eventAccent } from './eventFeed';
import { RecentSwitchesCard } from './RecentSwitchesCard';
import type { ResolutionEvent, Source } from './types';

const instance = (lng: 'en' | 'zh') => {
  const i18n = createInstance();
  void i18n.use(initReactI18next).init({
    lng,
    fallbackLng: 'en',
    resources: { en: { translation: en }, zh: { translation: zh } },
    interpolation: { escapeValue: false },
  });
  return i18n;
};

const source = (id: string): Source => ({
  id,
  kind: 'api_key',
  vendor: 'anthropic',
  display_name: id,
  protocol: 'anthropic',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: [],
});

const event = (over: Partial<ResolutionEvent> = {}): ResolutionEvent => ({
  id: 'evt_1',
  ts: '2026-07-29T09:12:00Z',
  agent: 'claude',
  kind: 'switch',
  model_id: 'claude-opus-4-6',
  from_source: 'src_gone01',
  to_source: 'src_live01',
  reason: 'quota_exhausted',
  human_zh: 'Claude Code 从 ChatGPT Plus 订阅 切到 Anthropic API Key（额度用完）',
  human_en: 'Claude Code switched from ChatGPT Plus to Anthropic API Key (quota used up)',
  ...over,
});

const render = (events: ResolutionEvent[], sources: Source[], lng: 'en' | 'zh' = 'zh') =>
  renderToStaticMarkup(
    <I18nextProvider i18n={instance(lng)}>
      <RecentSwitchesCard events={events} sources={sources} />
    </I18nextProvider>,
  );

describe('RecentSwitchesCard (AC-18)', () => {
  it('renders the recorded sentence verbatim, in the UI language', () => {
    const e = event();
    expect(render([e], [source('src_live01')], 'zh')).toContain(e.human_zh);
    expect(render([e], [source('src_live01')], 'en')).toContain(e.human_en);
  });

  it('keeps the sentence intact when the source it names is gone', () => {
    // The whole point: re-deriving the wording from today's inventory would blank
    // this row out or silently rewrite history.
    const html = render([event()], [source('src_live01')]);
    expect(html).toContain('ChatGPT Plus');
    expect(html).toContain(zh.settings.models.recent.deletedSource);
  });

  it('marks nothing when both endpoints still resolve', () => {
    const html = render([event()], [source('src_gone01'), source('src_live01')]);
    expect(html).not.toContain(zh.settings.models.recent.deletedSource);
  });

  it('marks nothing for an event that names no source at all', () => {
    // A supply interruption is about a backend: both endpoints are null by
    // contract, and null is not a deleted source.
    const html = render(
      [event({ kind: 'supply_interrupted', model_id: null, from_source: null, to_source: null })],
      [source('src_live01')],
    );
    expect(html).not.toContain(zh.settings.models.recent.deletedSource);
  });

  it('offers no action on a deleted source — there is nothing left to act on', () => {
    const html = render([event()], [source('src_live01')]);
    expect(html).not.toContain('<button');
  });

  it('shows three events and expands only when there are more', () => {
    const three = [1, 2, 3].map((n) => event({ id: `evt_${n}` }));
    expect(render(three, [source('src_live01')])).not.toContain(zh.settings.models.recent.viewAll);
    const four = [...three, event({ id: 'evt_4' })];
    expect(render(four, [source('src_live01')])).toContain(zh.settings.models.recent.viewAll);
  });

  it('keeps route configuration out of the user event feed', () => {
    const configured = event({ kind: 'mapping_applied', human_zh: '内部路由配置已变更' });
    const html = render([configured, event({ id: 'evt_2', human_zh: '已自动换到 openai' })], [source('src_live01')]);
    expect(html).not.toContain(configured.human_zh);
    expect(html).toContain('已自动换到 openai');
  });
});

// The contract puts `severity` in the payload as 「Feed and Models-page presentation
// metadata」. The feed used to ignore it and grade rows off `kind`, which left the
// two action_required kinds in the same cyan as an ordinary switch.
describe('eventAccent', () => {
  it('takes the server’s grading over anything it could infer', () => {
    expect(eventAccent(event({ severity: 'action_required' }))).toBe('gold');
    // Even on a kind that would otherwise read as routine traffic.
    expect(eventAccent(event({ kind: 'switch', severity: 'action_required' }))).toBe('gold');
  });

  it('leaves an info-graded event to the kind vocabulary', () => {
    expect(eventAccent(event({ kind: 'switch', severity: 'info' }))).toBe('cyan');
    expect(eventAccent(event({ kind: 'recover', severity: 'info' }))).toBe('mint');
    expect(eventAccent(event({ kind: 'cooldown', severity: 'info' }))).toBe('muted');
    expect(eventAccent(event({ kind: 'skip', severity: 'info' }))).toBe('muted');
  });

  // Only for a journal row written before the field existed: re-grading an outage
  // as ordinary traffic would hide the one row nobody may scroll past.
  it('falls back to the two kinds the contract pins action_required on', () => {
    expect(eventAccent(event({ kind: 'needs_action', severity: null }))).toBe('gold');
    expect(eventAccent(event({ kind: 'supply_interrupted', severity: undefined }))).toBe('gold');
    expect(eventAccent(event({ kind: 'switch', severity: null }))).toBe('cyan');
  });

  it('still golds a metered crossing, which is a billing fact rather than a severity', () => {
    expect(eventAccent(event({ kind: 'switch', severity: 'info', billing_note: 'entered_metered' }))).toBe('gold');
  });
});
