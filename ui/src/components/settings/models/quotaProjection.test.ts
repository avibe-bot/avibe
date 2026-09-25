import { describe, expect, it } from 'vitest';

import { limitLabelParts } from './quotaProjection';

describe('limitLabelParts', () => {
  it('reads an identifier as words, its spans as counts', () => {
    expect(limitLabelParts('seven_day_cowork')).toEqual([{ span: 'days', count: 7 }, { word: 'Cowork' }]);
    expect(limitLabelParts('five_hour')).toEqual([{ span: 'hours', count: 5 }]);
    expect(limitLabelParts('30_day-opus')).toEqual([{ span: 'days', count: 30 }, { word: 'Opus' }]);
    expect(limitLabelParts('codex_12_hours')).toEqual([{ word: 'Codex' }, { span: 'hours', count: 12 }]);
    expect(limitLabelParts('primary_window')).toEqual([{ word: 'Primary' }, { word: 'Window' }]);
    expect(limitLabelParts('Seven_Day')).toEqual([{ span: 'days', count: 7 }]);
    expect(limitLabelParts('seven-day-cowork')).toEqual([{ span: 'days', count: 7 }, { word: 'Cowork' }]);
  });

  it('reads a compound number word as one count', () => {
    expect(limitLabelParts('twenty_four_hour')).toEqual([{ span: 'hours', count: 24 }]);
    expect(limitLabelParts('twenty_eight_day_opus')).toEqual([{ span: 'days', count: 28 }, { word: 'Opus' }]);
    expect(limitLabelParts('thirty_day')).toEqual([{ span: 'days', count: 30 }]);
    expect(limitLabelParts('fourteen_day')).toEqual([{ span: 'days', count: 14 }]);
    expect(limitLabelParts('twenty_twelve_day')).toEqual([{ word: 'Twenty' }, { span: 'days', count: 12 }]);
  });

  it('keeps a count or unit that does not form a span as a word', () => {
    expect(limitLabelParts('day_pass')).toEqual([{ word: 'Day' }, { word: 'Pass' }]);
    expect(limitLabelParts('gpt_5_mini')).toEqual([{ word: 'Gpt' }, { word: '5' }, { word: 'Mini' }]);
    expect(limitLabelParts('0_day')).toEqual([{ word: '0' }, { word: 'Day' }]);
  });

  it('declines a token that is not id-shaped', () => {
    for (const label of ['Opus', 'fable', 'Code review', '非常长的上游额度名称', 'GPT-5 Codex', 'seven day', '', '__', 'a.b_c']) {
      expect(limitLabelParts(label)).toBeNull();
    }
  });
});
