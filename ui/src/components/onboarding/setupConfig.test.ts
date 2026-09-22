// The one config read setup is allowed to act on, stated as what it refuses to
// conclude. Every case here is a body that would pass a careless check and then be
// indexed into as if it had said something: an envelope, an error carried alongside a
// plausible payload, a field of the wrong type. The distinction the whole flow rests
// on is that none of those are `false` — they are unread.
import { describe, expect, it, vi } from 'vitest';

import { fetchSetupConfig, readSetupConfig } from './setupConfig';

const CONFIG = {
  version: 'v2',
  setup_completed: false,
  platforms: { primary: 'avibe', enabled: [] },
  runtime: {},
  agents: {},
  capabilities: { model_hub: { enabled: true } },
  model_hub: { enabled: true },
};

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('readSetupConfig', () => {
  it('accepts the top-level config object the handlers really return', () => {
    expect(readSetupConfig(CONFIG)).toMatchObject({
      version: 'v2',
      capabilityEnabled: true,
      savedIntentEnabled: true,
      primaryPlatform: 'avibe',
      enabledPlatforms: [],
      raw: CONFIG,
    });
  });

  it('rejects the envelope shape nobody sends', () => {
    // `{ok:true, config:{...}}` reads as a success to a careless check and carries
    // none of the fields the caller then indexes into.
    expect(readSetupConfig({ ok: true, config: CONFIG })).toBeNull();
  });

  it('rejects a body that carries an error alongside a plausible payload', () => {
    expect(readSetupConfig({ ...CONFIG, error: 'config load failed' })).toBeNull();
    expect(readSetupConfig({ ...CONFIG, ok: false })).toBeNull();
  });

  it.each([
    ['version', { version: 'v1' }],
    ['setup_completed', { setup_completed: 'no' }],
    ['platforms.primary', { platforms: { enabled: [] } }],
    ['platforms.enabled', { platforms: { primary: 'avibe', enabled: 'slack' } }],
    ['runtime', { runtime: null }],
    ['agents', { agents: [] }],
    ['capabilities.model_hub', { capabilities: {} }],
    ['model_hub', { model_hub: {} }],
  ])('treats a malformed %s as unread rather than as a value', (_field, over) => {
    expect(readSetupConfig({ ...CONFIG, ...over })).toBeNull();
  });

  it('reads a disabled gateway as disabled, which is the point of the distinction', () => {
    expect(readSetupConfig({ ...CONFIG, model_hub: { enabled: false } })).toMatchObject({
      capabilityEnabled: true,
      savedIntentEnabled: false,
    });
  });
});

describe('fetchSetupConfig', () => {
  it('asks for the config as it is right now', async () => {
    // A cached read is the failure this function exists to prevent: it would let setup
    // finish against a prerequisite somebody already turned off in Settings.
    const fetch = vi.fn(async () => json(CONFIG));

    await expect(fetchSetupConfig(fetch)).resolves.toMatchObject({ state: 'read' });
    expect(fetch).toHaveBeenCalledWith('/api/config', { cache: 'no-store' });
  });

  it('reports a transport failure as unread', async () => {
    const read = await fetchSetupConfig(vi.fn(async () => { throw new TypeError('network'); }));

    expect(read).toMatchObject({ state: 'unread' });
    expect((read as { status?: number }).status).toBeUndefined();
  });

  it("keeps the server's own words about a failed read", async () => {
    const read = await fetchSetupConfig(vi.fn(async () => json({ error: 'config load failed' }, 500)));

    expect(read).toMatchObject({ state: 'unread', status: 500, detail: 'config load failed' });
  });

  it('reports a body that arrived and said nothing without a status to explain it', async () => {
    // HTTP 200 explains nothing about a 200 whose body is unusable, and quoting it
    // would point a person at the transport instead of the answer.
    const read = await fetchSetupConfig(vi.fn(async () => json({ version: 'v1' })));

    expect(read).toEqual({ state: 'unread', detail: undefined });
  });

  it('treats an unparseable body as unread rather than as an empty config', async () => {
    const read = await fetchSetupConfig(vi.fn(async () => new Response('{"version":', { status: 200 })));

    expect(read).toMatchObject({ state: 'unread' });
  });
});
