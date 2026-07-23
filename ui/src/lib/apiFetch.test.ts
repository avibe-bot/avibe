import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const deferRemoteAuthRedirect = vi.hoisted(() => vi.fn());

vi.mock('./remoteAuth', () => ({ deferRemoteAuthRedirect }));

import { apiFetch } from './apiFetch';

describe('apiFetch remote auth recovery', () => {
  beforeEach(() => {
    deferRemoteAuthRedirect.mockReturnValue(true);
    vi.stubGlobal('window', {
      location: { href: 'https://alex.avibe.bot/inbox', assign: vi.fn() },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('hands an expired remote session to the PWA auth gate', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({ error: 'remote_access_login_required' }, { status: 401 }),
      ),
    );

    const response = await apiFetch('/api/inbox');

    expect(response.status).toBe(401);
    await vi.waitFor(() => expect(deferRemoteAuthRedirect).toHaveBeenCalledOnce());
    expect(window.location.assign).not.toHaveBeenCalled();
  });

  it('does not start remote auth for an unrelated 401', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ error: 'not_allowed' }, { status: 401 })),
    );

    await apiFetch('/api/inbox');

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(deferRemoteAuthRedirect).not.toHaveBeenCalled();
  });
});
