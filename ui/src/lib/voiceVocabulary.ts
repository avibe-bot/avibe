/**
 * Names recognition should hear exactly: the reader's Bots' display names (the main Bot and every
 * custom Bot), published by `BotsProvider` whenever they change, and read when a dictation starts,
 * so a renamed Bot is heard by its new name from the next dictation on. avibe.bot weighs them
 * above anything taken from the conversation (`start.vocabulary`, a segment's `vocabulary`).
 */

/** avibe.bot's bounds for explicit words, on a realtime start frame and a dictation segment. */
export const MAX_VOICE_VOCABULARY_WORDS = 20;
export const MAX_VOICE_VOCABULARY_WORD_UNITS = 64;

let current: readonly string[] = [];

/** The names avibe.bot accepts, in order: trimmed, 1–64 UTF-16 units on one line, each once
 *  (case-insensitive), at most 20. A name outside the bounds is left out, never cut. */
export const cleanVoiceVocabulary = (names: Iterable<string>): string[] => {
  const words = new Map<string, string>();
  for (const name of names) {
    const word = name.trim();
    if (!word || word.length > MAX_VOICE_VOCABULARY_WORD_UNITS || /[\r\n]/u.test(word)) continue;
    const key = word.toLowerCase();
    if (!words.has(key)) words.set(key, word);
    if (words.size >= MAX_VOICE_VOCABULARY_WORDS) break;
  }
  return [...words.values()];
};

export const setVoiceVocabulary = (names: Iterable<string>): void => {
  current = cleanVoiceVocabulary(names);
};

/** The names as they are now. */
export const voiceVocabulary = (): readonly string[] => current;
