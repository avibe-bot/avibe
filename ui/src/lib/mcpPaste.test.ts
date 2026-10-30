import { describe, expect, it } from 'vitest';

import { pasteMayNeedOAuthLogin, preparedTabNeedsAuthorizeFallback } from './mcpPaste';

describe('pasteMayNeedOAuthLogin', () => {
  it('is true for an HTTP URL server without Authorization', () => {
    expect(
      pasteMayNeedOAuthLogin(
        JSON.stringify({ mcpServers: { suno: { url: 'https://api.example/mcp' } } }),
      ),
    ).toBe(true);
  });

  it('is false for stdio command servers', () => {
    expect(
      pasteMayNeedOAuthLogin(
        JSON.stringify({ mcpServers: { echo: { command: 'npx', args: ['-y', 'echo'] } } }),
      ),
    ).toBe(false);
  });

  it('is false when Authorization is already set', () => {
    expect(
      pasteMayNeedOAuthLogin(
        JSON.stringify({
          mcpServers: {
            github: {
              url: 'https://api.githubcopilot.com/mcp/',
              headers: { Authorization: 'Bearer tok' },
            },
          },
        }),
      ),
    ).toBe(false);
  });

  it('treats authorization header names case-insensitively', () => {
    expect(
      pasteMayNeedOAuthLogin(
        JSON.stringify({
          mcpServers: { github: { url: 'https://api.example/mcp', headers: { authorization: 'Bearer tok' } } },
        }),
      ),
    ).toBe(false);
  });

  it('is true when a batch mixes stdio with one OAuth-eligible URL server', () => {
    expect(
      pasteMayNeedOAuthLogin(
        JSON.stringify({
          mcpServers: {
            echo: { command: 'npx' },
            suno: { url: 'https://api.example/mcp' },
          },
        }),
      ),
    ).toBe(true);
  });

  it('is false when JSON cannot be parsed', () => {
    expect(pasteMayNeedOAuthLogin('{not json')).toBe(false);
  });
});

describe('preparedTabNeedsAuthorizeFallback', () => {
  it('falls back when the popup was blocked', () => {
    expect(preparedTabNeedsAuthorizeFallback(null)).toBe(true);
  });

  it('falls back when the prepared tab was closed before navigation', () => {
    expect(preparedTabNeedsAuthorizeFallback({ closed: true } as Window)).toBe(true);
  });

  it('does not fall back for an open prepared tab', () => {
    expect(preparedTabNeedsAuthorizeFallback({ closed: false } as Window)).toBe(false);
  });
});
