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
  });

  it('keeps a count or unit that does not form a span as a word', () => {
    expect(limitLabelParts('day_pass')).toEqual([{ word: 'Day' }, { word: 'Pass' }]);
    expect(limitLabelParts('gpt_5_mini')).toEqual([{ word: 'Gpt' }, { word: '5' }, { word: 'Mini' }]);
    expect(limitLabelParts('0_day')).toEqual([{ word: '0' }, { word: 'Day' }]);
  });

  it('leaves text, display names, and single words as the vendor wrote them', () => {
    for (const label of ['Opus', 'fable', 'Code review', '非常长的上游额度名称', 'GPT-5 Codex', 'seven day', '', '__', 'a.b_c']) {
      expect(limitLabelParts(label)).toBeNull();
    }
  });
});
