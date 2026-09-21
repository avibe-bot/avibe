// The bootstrap sequence, stated as the things it must refuse to do.
//
// Almost every case here is a plausible shortcut: retry the write that might have
// landed, believe a GET that is really an in-memory default, start a controller whose
// state nobody confirmed, install an engine the server's own recovery is already
// installing. Each one produces a green happy path and a wrong answer somewhere else.
import { describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';

import type { BackendConnectionState } from '@/context/ApiContext';
import type { RuntimeDependency } from '@/components/settings/models/types';

import { GatewayBootstrapError, bootstrapGateway, readSetupConfig } from './gatewayBootstrap';
import type { GatewayBootstrapDeps } from './gatewayBootstrap';

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

const connection = (over: Partial<BackendConnectionState> = {}): BackendConnectionState => ({
  ok: true,
  backend: 'claude',
  installed: true,
  enabled: true,
  auth: 'none',
  application: 'applied',
  ready: false,
  entry_eligible: false,
  ...over,
});

const RUNTIME = {
  contract_version: 'v3',
  manifest: {},
  status: { verified: true, health: 'ok' },
} as unknown as RuntimeDependency;

function deps(over: Partial<GatewayBootstrapDeps> = {}) {
  const fetch = vi.fn(async (_input: string, init?: RequestInit) =>
    (init?.method === 'POST' ? json({ ...CONFIG }) : json({ ...CONFIG })));
  return {
    fetch,
    getBackendConnection: vi.fn(async () => connection()),
    control: vi.fn(async () => ({ ok: true, action: 'start' })),
    getRuntimeStatus: vi.fn(async () => RUNTIME),
    ...over,
  } satisfies GatewayBootstrapDeps;
}

/** Request arguments, from a dependency the caller declared as a plain function. */
const requests = (fn: GatewayBootstrapDeps['fetch']): [string, RequestInit?][] =>
  (fn as unknown as Mock).mock.calls as [string, RequestInit?][];

/** The writes only. Every case below has something to say about how many there were. */
const writes = (fn: GatewayBootstrapDeps['fetch']): [string, RequestInit?][] =>
  requests(fn).filter(([, init]) => init?.method === 'POST');

/**
 * A server whose write outcome and read answer are set separately.
 *
 * Which is the whole subject here: a write nobody can account for and a config that
 * reads back perfectly well is not a contradiction, it is the ordinary case after a
 * dropped reply, and the two halves have to be expressible apart.
 */
const split = (
  post: () => Promise<Response>,
  get: () => Promise<Response> = async () => json(CONFIG),
) => vi.fn(async (_url: string, init?: RequestInit) => (init?.method === 'POST' ? post() : get()));

/** The two ways a write ends up unaccounted for, as a server does them. */
const DROPPED: [string, () => Promise<Response>][] = [
  ['a transport failure', async () => { throw new TypeError('network'); }],
  ['a server fault', async () => json({ error: { message: 'boom' } }, 503)],
];

const failure = async (promise: Promise<unknown>): Promise<GatewayBootstrapError> => {
  const error = await promise.then(() => null, (thrown: unknown) => thrown);
  expect(error).toBeInstanceOf(GatewayBootstrapError);
  return error as GatewayBootstrapError;
};

describe('readSetupConfig', () => {
  it('accepts the top-level config object the handlers really return', () => {
    expect(readSetupConfig(CONFIG)).toMatchObject({
      version: 'v2',
      capabilityEnabled: true,
      savedIntentEnabled: true,
      primaryPlatform: 'avibe',
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
});

describe('bootstrapGateway', () => {
  it('seeds, reads back uncached, proves persistence, and observes the runtime', async () => {
    const d = deps();

    await expect(bootstrapGateway(d, 'claude')).resolves.toMatchObject({ runtime: RUNTIME });

    const [[seedUrl, seedInit], [readUrl, readInit]] = requests(d.fetch);
    expect(seedUrl).toBe('/api/config');
    expect(seedInit).toMatchObject({ method: 'POST', body: '{}' });
    expect(new Headers(seedInit?.headers).get('Content-Type')).toBe('application/json');
    expect(readUrl).toBe('/api/config');
    expect(readInit).toMatchObject({ cache: 'no-store' });
    // One write per attempt, acknowledged or not.
    expect(writes(d.fetch)).toHaveLength(1);
  });

  it('does not start a controller that is already applied', async () => {
    const d = deps();

    await bootstrapGateway(d, 'claude');

    expect(d.control).not.toHaveBeenCalled();
  });

  it('starts only a controller confirmed stopped, then re-reads it', async () => {
    const getBackendConnection = vi.fn()
      .mockResolvedValueOnce(connection({ application: 'stopped' }))
      .mockResolvedValueOnce(connection({ application: 'applied' }));
    const d = deps({ getBackendConnection });

    await expect(bootstrapGateway(d, 'claude')).resolves.toMatchObject({ runtime: RUNTIME });
    expect(d.control).toHaveBeenCalledWith('start');
    expect(getBackendConnection).toHaveBeenCalledTimes(2);
  });

  it.each(['draining', 'failed', 'unknown'] as const)(
    'never infers a start from %s',
    async (application) => {
      const d = deps({ getBackendConnection: vi.fn(async () => connection({ application })) });

      const error = await failure(bootstrapGateway(d, 'claude'));

      expect(d.control).not.toHaveBeenCalled();
      expect(error).toMatchObject({ step: 'start', reason: 'unknown' });
    },
  );

  it('never ensures or starts the engine itself', async () => {
    // Controller startup owns the first recovery, with the installer admission that
    // belongs on the server. A browser-side ensure here would race it.
    const d = deps();

    await bootstrapGateway(d, 'claude');

    expect(d.getRuntimeStatus).toHaveBeenCalledTimes(1);
    expect(requests(d.fetch).map(([url]) => url)).toEqual(['/api/config', '/api/config']);
  });

  it('treats a refusal as definitive and writes nothing more', async () => {
    const fetch = vi.fn(async () => json({ error: 'config is locked' }, 403));
    const d = deps({ fetch });

    const error = await failure(bootstrapGateway(d, 'claude'));

    expect(error).toMatchObject({ reason: 'refused', step: 'seed', status: 403, detail: 'config is locked' });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('keeps a refusal refused even where the config reads back perfectly well', async () => {
    // The read describes what exists; it does not describe what the server declined
    // to do. Letting a healthy GET clear a 403 would report a locked config as
    // prepared, and every later write would be refused the same way with no reason
    // left on screen.
    const fetch = split(async () => json({ error: 'config is locked' }, 403));
    const d = deps({ fetch });

    expect(await failure(bootstrapGateway(d, 'claude')))
      .toMatchObject({ reason: 'refused', step: 'seed', status: 403 });
    expect(d.getBackendConnection).not.toHaveBeenCalled();
    expect(d.control).not.toHaveBeenCalled();
    expect(writes(fetch)).toHaveLength(1);
  });

  it.each(DROPPED)(
    'settles %s by reading, and starts once persistence is proven',
    async (_how, post) => {
      // The write may well have committed and lost its reply. Reading is the only
      // thing that can tell, and it is the same read an acknowledged write takes.
      const fetch = split(post);
      const getBackendConnection = vi.fn()
        .mockResolvedValueOnce(connection({ application: 'stopped' }))
        .mockResolvedValueOnce(connection({ application: 'applied' }));
      const d = deps({ fetch, getBackendConnection });

      await expect(bootstrapGateway(d, 'claude')).resolves.toMatchObject({ runtime: RUNTIME });

      // One write, and the reads that account for it.
      expect(writes(fetch)).toHaveLength(1);
      expect(requests(fetch)[1]).toMatchObject(['/api/config', { cache: 'no-store' }]);
      expect(getBackendConnection).toHaveBeenCalledTimes(2);
      expect(d.control).toHaveBeenCalledWith('start');
    },
  );

  it('treats a truncated 2xx as unaccounted for rather than as an acknowledgement', async () => {
    // A 2xx whose body is unreadable says nothing about what was written; the
    // readback is what settles it, and it is the same read either way.
    const fetch = split(async () => new Response('{"version":', { status: 200 }));
    const d = deps({ fetch });

    await expect(bootstrapGateway(d, 'claude')).resolves.toMatchObject({ runtime: RUNTIME });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('reports the write it cannot account for when the reads fail too', async () => {
    const fetch = split(
      async () => json({ error: { message: 'boom' } }, 503),
      async () => json({ error: 'config load failed' }, 500),
    );
    const d = deps({ fetch });

    // The server's own words about the write, kept: a read that failed adds nothing
    // a person can act on, and the write is what is still outstanding.
    expect(await failure(bootstrapGateway(d, 'claude')))
      .toMatchObject({ reason: 'unknown', step: 'seed', status: 503, detail: 'boom' });
    expect(writes(fetch)).toHaveLength(1);
    expect(d.getBackendConnection).not.toHaveBeenCalled();
  });

  it('keeps a dropped write outstanding when the readback cannot be reached', async () => {
    const fetch = split(
      async () => { throw new TypeError('network'); },
      async () => { throw new TypeError('network'); },
    );
    const d = deps({ fetch });

    // `unread` would say no write is outstanding, which is the one thing nobody knows.
    expect(await failure(bootstrapGateway(d, 'claude')))
      .toMatchObject({ reason: 'unknown', step: 'seed' });
    expect(writes(fetch)).toHaveLength(1);
  });

  it('stays blocked when the readback cannot be validated', async () => {
    const fetch = split(async () => json(CONFIG), async () => json({ version: 'v1' }));
    const d = deps({ fetch });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'unread', step: 'readback' });
    expect(d.getBackendConnection).not.toHaveBeenCalled();
  });

  it.each([
    ['the capability', { capabilities: { model_hub: { enabled: false } } }],
    ['saved intent', { model_hub: { enabled: false } }],
  ])('stops at the prerequisite boundary when %s is off, changing nothing', async (_which, over) => {
    const fetch = split(async () => json(CONFIG), async () => json({ ...CONFIG, ...over }));
    const d = deps({ fetch });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'disabled', step: 'capability' });
    expect(d.control).not.toHaveBeenCalled();
    expect(d.getRuntimeStatus).not.toHaveBeenCalled();
    // One write and no second: the preference is left exactly as it was.
    expect(writes(fetch)).toHaveLength(1);
  });

  it('stops at that boundary even holding a write it cannot account for', async () => {
    // The gateway is off by somebody's decision. What became of an empty patch has
    // stopped mattering, and reporting it would send a person to retry a write when
    // what they are owed is the setting.
    const fetch = split(
      async () => { throw new TypeError('network'); },
      async () => json({ ...CONFIG, model_hub: { enabled: false } }),
    );
    const d = deps({ fetch });

    expect(await failure(bootstrapGateway(d, 'claude')))
      .toMatchObject({ reason: 'disabled', step: 'capability' });
    expect(writes(fetch)).toHaveLength(1);
    expect(d.control).not.toHaveBeenCalled();
  });

  it('does not accept a valid GET as proof the config file exists', async () => {
    // The server answers with an in-memory default when there is no file, so the
    // connection read — whose handler really calls load_config() — is the proof.
    const d = deps({ getBackendConnection: vi.fn(async () => connection({ ok: false })) });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'unknown', step: 'persistence' });
    expect(d.control).not.toHaveBeenCalled();
  });

  it('stays blocked when the persistence read itself cannot be made', async () => {
    const d = deps({
      getBackendConnection: vi.fn(async () => { throw new Error('socket closed'); }),
    });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'unknown', step: 'persistence' });
    expect(d.control).not.toHaveBeenCalled();
  });

  it('keeps a dropped write outstanding when persistence is not proven', async () => {
    // A valid GET and an unproven config file: nothing here has settled the write,
    // so the server's own account of it is still the most specific thing to report.
    const fetch = split(async () => json({ error: { message: 'boom' } }, 503));
    const d = deps({ fetch, getBackendConnection: vi.fn(async () => connection({ ok: false })) });

    expect(await failure(bootstrapGateway(d, 'claude')))
      .toMatchObject({ reason: 'unknown', step: 'seed', status: 503 });
    expect(writes(fetch)).toHaveLength(1);
    expect(d.control).not.toHaveBeenCalled();
  });

  it('retires a dropped write once persistence is proven, and reports what fails next', async () => {
    // The empty patch's only intended effect was a config file the server can load.
    // A handler that just loaded one is that effect, observed — so a later failure is
    // its own, and reporting the seed here would blame a write that is accounted for.
    const fetch = split(async () => { throw new TypeError('network'); });
    const d = deps({ fetch, getRuntimeStatus: vi.fn(async () => ({}) as RuntimeDependency) });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'unread', step: 'runtime' });
    expect(writes(fetch)).toHaveLength(1);
  });

  it('keeps a rejected start unknown rather than reporting the controller up', async () => {
    const d = deps({
      getBackendConnection: vi.fn(async () => connection({ application: 'stopped' })),
      control: vi.fn(async () => { throw new Error('restart_in_progress'); }),
    });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'unknown', step: 'start' });
  });

  it('does not take a 2xx control body at its word', async () => {
    const d = deps({
      getBackendConnection: vi.fn(async () => connection({ application: 'stopped' })),
      control: vi.fn(async () => ({ ok: true, action: 'restart' })),
    });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'unknown', step: 'start' });
  });

  it('reports an unreadable runtime instead of inventing a status for it', async () => {
    const d = deps({ getRuntimeStatus: vi.fn(async () => ({}) as RuntimeDependency) });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'unread', step: 'runtime' });
  });

  it('asks the backend the caller named, not one of its own choosing', async () => {
    const d = deps();

    await bootstrapGateway(d, 'opencode');

    expect(d.getBackendConnection).toHaveBeenCalledWith('opencode');
  });
});
