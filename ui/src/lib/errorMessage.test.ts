import { describe, expect, it } from 'vitest';
import { errorMessage } from './errorMessage';

describe('errorMessage', () => {
  it('reads the message off an Error', () => {
    expect(errorMessage(new Error('boom'))).toBe('boom');
  });

  it('reads the message off any object that carries one', () => {
    expect(errorMessage({ message: 'rejected by server' })).toBe('rejected by server');
  });

  it('keeps an empty message so callers can choose between ?? and ||', () => {
    // `?? fallback` must keep '' and `|| fallback` must replace it — that choice
    // belongs to the call site, so the helper must not collapse the two.
    expect(errorMessage(new Error(''))).toBe('');
  });

  it('returns undefined for a thrown value with no message', () => {
    expect(errorMessage({ code: 'E_NOPE' })).toBeUndefined();
    expect(errorMessage('boom')).toBeUndefined();
    expect(errorMessage(42)).toBeUndefined();
  });

  it('returns undefined for nullish throws instead of dereferencing them', () => {
    expect(errorMessage(null)).toBeUndefined();
    expect(errorMessage(undefined)).toBeUndefined();
  });

  it('returns undefined when message is present but not a string', () => {
    // The old `catch (err: any)` sites would have assigned a number straight
    // into string state; falling back to the caller's message is safer.
    expect(errorMessage({ message: 42 })).toBeUndefined();
    expect(errorMessage({ message: { nested: true } })).toBeUndefined();
  });
});
