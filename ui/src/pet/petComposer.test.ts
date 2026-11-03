import { beforeEach, describe, expect, it, vi } from 'vitest';

type Composer = typeof import('./petComposer')['petComposer'];

// The store is one per document; each case starts from a fresh one.
let petComposer: Composer;
beforeEach(async () => {
  vi.resetModules();
  ({ petComposer } = await import('./petComposer'));
  petComposer.bind('S');
});

// What PetPage does when a dictation finishes: note it, then put it in the draft.
const dictate = (before: string, heard: string) => {
  const value = [before.trim(), heard].filter(Boolean).join(' ');
  petComposer.noteDictation(before, value);
  petComposer.setDraft(value, 'S');
  return value;
};

describe('petComposer dictated drafts', () => {
  it('sends words dictated into an empty draft, and dictated again, as spoken', () => {
    const first = dictate('', 'check the release');
    expect(petComposer.isDictated(first)).toBe(true);
    const second = dictate(first, 'and the docs');
    expect(second).toBe('check the release and the docs');
    expect(petComposer.isDictated(second)).toBe(true);
    // Only surrounding whitespace is ignored: the send trims it.
    expect(petComposer.isDictated(` ${second} `)).toBe(true);
  });

  it('sends a dictated draft the person changed as typed', () => {
    const heard = dictate('', 'check the release');
    expect(petComposer.isDictated('check the releases')).toBe(false);
    expect(petComposer.isDictated(heard.slice(0, -1))).toBe(false);
    // A dictation added after an edit leaves typed words in the draft.
    expect(petComposer.isDictated(dictate('check the releases', 'now'))).toBe(false);
  });

  it('sends words dictated onto typed text as typed', () => {
    petComposer.setDraft('typed first', 'S');
    expect(petComposer.isDictated(dictate('typed first', 'then spoken'))).toBe(false);
  });

  it('forgets the dictation once the draft is sent or belongs to another session', () => {
    const heard = dictate('', 'check the release');
    expect(petComposer.clearDraftIf(heard)).toBe(true);
    expect(petComposer.isDictated(heard)).toBe(false);

    const again = dictate('', 'check the release');
    petComposer.bind('T');
    expect(petComposer.isDictated(again)).toBe(false);

    petComposer.bind('S');
    const spoken = dictate('', 'one more');
    petComposer.setDraft(spoken, 'T');
    expect(petComposer.isDictated(spoken)).toBe(false);
  });

  it('never calls an empty message spoken', () => {
    petComposer.noteDictation('', '');
    expect(petComposer.isDictated('')).toBe(false);
  });
});
