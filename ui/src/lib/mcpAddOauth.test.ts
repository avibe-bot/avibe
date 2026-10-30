import { describe, expect, it } from 'vitest';

import { mcpAddOauthAction } from './mcpAddOauth';

describe('mcpAddOauthAction', () => {
  it('assigns the current tab to the start URL for one direct Web login', () => {
    expect(
      mcpAddOauthAction({ names: ['suno'], failedCount: 0, oauthMode: 'direct', desktopShell: false }),
    ).toEqual({ kind: 'assign', name: 'suno' });
  });

  it('lists login links when more than one server needs login', () => {
    expect(
      mcpAddOauthAction({
        names: ['suno', 'github'],
        failedCount: 0,
        oauthMode: 'direct',
        desktopShell: false,
      }),
    ).toEqual({ kind: 'list', names: ['suno', 'github'] });
  });

  it('lists login links when any add failed, even for a single needs_login server', () => {
    expect(
      mcpAddOauthAction({ names: ['suno'], failedCount: 1, oauthMode: 'direct', desktopShell: false }),
    ).toEqual({ kind: 'list', names: ['suno'] });
  });

  it('does not assign in paste mode or the desktop shell', () => {
    expect(
      mcpAddOauthAction({ names: ['suno'], failedCount: 0, oauthMode: 'paste', desktopShell: false }),
    ).toEqual({ kind: 'list', names: ['suno'] });
    expect(
      mcpAddOauthAction({ names: ['suno'], failedCount: 0, oauthMode: 'direct', desktopShell: true }),
    ).toEqual({ kind: 'list', names: ['suno'] });
  });

  it('does nothing when no added server needs login', () => {
    expect(
      mcpAddOauthAction({ names: [], failedCount: 0, oauthMode: 'direct', desktopShell: false }),
    ).toEqual({ kind: 'none' });
  });
});
