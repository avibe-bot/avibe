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

  it('treats a server fault as an unknown write, and does not repeat the POST', async () => {
    const fetch = vi.fn(async () => json({ error: { message: 'boom' } }, 500));
    const d = deps({ fetch });

    const error = await failure(bootstrapGateway(d, 'claude'));

    expect(error).toMatchObject({ reason: 'unknown', step: 'seed', detail: 'boom' });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('treats a transport failure as unknown, and does not repeat the POST', async () => {
    const fetch = vi.fn(async () => { throw new TypeError('network'); });
    const d = deps({ fetch });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'unknown', step: 'seed' });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('still reads back after an acknowledged success it cannot otherwise settle', async () => {
    // A 2xx whose body is truncated is not a readable acknowledgement; the readback
    // is what settles it, and it is the same read either way.
    const fetch = vi.fn(async (_url: string, init?: RequestInit) =>
      (init?.method === 'POST'
        ? new Response('{"version":', { status: 200 })
        : json(CONFIG)));
    const d = deps({ fetch });

    await expect(bootstrapGateway(d, 'claude')).resolves.toMatchObject({ runtime: RUNTIME });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('stays blocked when the readback cannot be validated', async () => {
    const fetch = vi.fn(async (_url: string, init?: RequestInit) =>
      (init?.method === 'POST' ? json(CONFIG) : json({ version: 'v1' })));
    const d = deps({ fetch });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'unread', step: 'readback' });
    expect(d.getBackendConnection).not.toHaveBeenCalled();
  });

  it.each([
    ['the capability', { capabilities: { model_hub: { enabled: false } } }],
    ['saved intent', { model_hub: { enabled: false } }],
  ])('stops at the prerequisite boundary when %s is off, changing nothing', async (_which, over) => {
    const fetch = vi.fn(async (_url: string, init?: RequestInit) =>
      (init?.method === 'POST' ? json(CONFIG) : json({ ...CONFIG, ...over })));
    const d = deps({ fetch });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'disabled', step: 'capability' });
    expect(d.control).not.toHaveBeenCalled();
    expect(d.getRuntimeStatus).not.toHaveBeenCalled();
    // One write and no second: the preference is left exactly as it was.
    expect(requests(fetch).filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  });

  it('does not accept a valid GET as proof the config file exists', async () => {
    // The server answers with an in-memory default when there is no file, so the
    // connection read — whose handler really calls load_config() — is the proof.
    const d = deps({ getBackendConnection: vi.fn(async () => connection({ ok: false })) });

    expect(await failure(bootstrapGateway(d, 'claude'))).toMatchObject({ reason: 'unknown', step: 'persistence' });
    expect(d.control).not.toHaveBeenCalled();
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
