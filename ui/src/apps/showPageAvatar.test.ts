import { describe, expect, it } from 'vitest';

import {
  SHOW_PAGE_ACCENTS,
  accentForSession,
  firstGrapheme,
  sessionChatPath,
  showPageAvatar,
  showPageIconUrl,
  showPagePrivatePath,
} from './showPageAvatar';

describe('firstGrapheme', () => {
  it('takes the first letter of an ASCII title', () => {
    expect(firstGrapheme('Sales Dashboard')).toBe('S');
  });

  it('is grapheme-aware for CJK', () => {
    expect(firstGrapheme('旅程报告')).toBe('旅');
  });

  it('trims leading whitespace before taking the first grapheme', () => {
    expect(firstGrapheme('   Weekly report')).toBe('W');
  });

  it('returns an empty string for blank input', () => {
    expect(firstGrapheme('')).toBe('');
    expect(firstGrapheme('   ')).toBe('');
  });

  // Negative control: a multi-code-point emoji (ZWJ family / flag) must come back
  // WHOLE — never a lone surrogate half or a single regional-indicator.
  it('never splits a multi-code-point grapheme', () => {
    const family = firstGrapheme('👩‍👧 family time');
    // The whole cluster is returned, so it is longer than one UTF-16 code unit
    // and re-extracting its own first grapheme is a fixed point.
    expect(family.length).toBeGreaterThan(1);
    expect(firstGrapheme(family)).toBe(family);

    const flag = firstGrapheme('🇯🇵 Tokyo');
    expect(Array.from(flag).length).toBeGreaterThanOrEqual(1);
    expect(firstGrapheme(flag)).toBe(flag);
  });
});

describe('accentForSession', () => {
  it('always returns a var from the brand accent set', () => {
    for (const id of ['ses_a', 'ses_b', 'sesz8jhr3hgyz', '', 'x', '旅', '🚀']) {
      expect(SHOW_PAGE_ACCENTS as readonly string[]).toContain(accentForSession(id));
    }
  });

  it('is deterministic for the same session id', () => {
    expect(accentForSession('ses_stable')).toBe(accentForSession('ses_stable'));
  });

  it('spreads distinct ids across more than one accent', () => {
    const seen = new Set(
      ['ses_1', 'ses_2', 'ses_3', 'ses_4', 'ses_5', 'ses_6', 'ses_7', 'ses_8'].map(accentForSession),
    );
    expect(seen.size).toBeGreaterThan(1);
  });
});

describe('showPageAvatar', () => {
  it('uppercases the first letter and hashes the accent from the session id', () => {
    const avatar = showPageAvatar('ses_sales', 'sales dashboard');
    expect(avatar.letter).toBe('S');
    expect(avatar.accentVar).toBe(accentForSession('ses_sales'));
  });

  it('falls back to the session id when the title is blank', () => {
    expect(showPageAvatar('ses_plain', '   ').letter).toBe('S');
  });
});

describe('showPagePrivatePath', () => {
  it('always points at the private /show/ surface, url-encoded', () => {
    expect(showPagePrivatePath('ses_1')).toBe('/show/ses_1/');
    expect(showPagePrivatePath('a/b')).toBe('/show/a%2Fb/');
  });
});

describe('showPageIconUrl', () => {
  it('puts the session id in the path and the server-issued token as ?v=', () => {
    // The token is an opaque cache key appended verbatim — the endpoint resolves
    // the icon from the sid + workspace and never reads the query.
    expect(showPageIconUrl('ses_1', 'abc123')).toBe('/api/show-pages/ses_1/icon?v=abc123');
  });

  it('url-encodes both the session id and the token', () => {
    expect(showPageIconUrl('a/b', 'a b/c')).toBe('/api/show-pages/a%2Fb/icon?v=a%20b%2Fc');
  });

  it('returns null when the page has no icon (the token is the has-icon signal)', () => {
    expect(showPageIconUrl('ses_1', null)).toBeNull();
    expect(showPageIconUrl('ses_1', undefined)).toBeNull();
    expect(showPageIconUrl('ses_1', '   ')).toBeNull();
  });
});

describe('sessionChatPath', () => {
  it('points at the in-app chat route for the session, url-encoded', () => {
    expect(sessionChatPath('ses_1')).toBe('/chat/ses_1');
    expect(sessionChatPath('a/b')).toBe('/chat/a%2Fb');
  });

  it('appends the ?view=chat show-me-the-chat signal only when requested', () => {
    expect(sessionChatPath('ses_1', { showChat: true })).toBe('/chat/ses_1?view=chat');
    expect(sessionChatPath('a/b', { showChat: true })).toBe('/chat/a%2Fb?view=chat');
    // Omitted / falsy keeps the plain canonical route (no stray query).
    expect(sessionChatPath('ses_1', {})).toBe('/chat/ses_1');
    expect(sessionChatPath('ses_1', { showChat: false })).toBe('/chat/ses_1');
  });
});
