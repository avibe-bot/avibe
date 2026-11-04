import { afterEach, describe, expect, it } from 'vitest';

import { cleanVoiceVocabulary, setVoiceVocabulary, voiceVocabulary } from './voiceVocabulary';

afterEach(() => setVoiceVocabulary([]));

describe('voiceVocabulary', () => {
  it("keeps only names avibe.bot accepts, in order, once each, and never cuts one", () => {
    expect(cleanVoiceVocabulary([' 歪比 ', 'Security Lead', '歪比', 'security lead', '', '   ', 'a'.repeat(65), 'Pull\nRequest', '😀'.repeat(32)]))
      .toEqual(['歪比', 'Security Lead', '😀'.repeat(32)]);
    expect(cleanVoiceVocabulary(Array.from({ length: 25 }, (_, index) => `Bot ${index}`))).toHaveLength(20);
  });

  it('holds what was published last', () => {
    expect(voiceVocabulary()).toEqual([]);
    setVoiceVocabulary(['歪比', 'Product']);
    expect(voiceVocabulary()).toEqual(['歪比', 'Product']);
    setVoiceVocabulary(['歪比 2']);
    expect(voiceVocabulary()).toEqual(['歪比 2']);
  });
});
