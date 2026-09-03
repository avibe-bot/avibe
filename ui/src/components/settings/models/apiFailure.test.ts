// Which failures the ROUTE named, as the transport itself reports it.
//
// The regression these pin: a caught error was treated as server-named whenever
// it was one of ours (`apiFailure(err) !== null`), and two of ours are not. `call`
// mints `bad_response` when the body will not parse and `http_<n>` when it parses
// with no `error` in it — both of which mean the answer never arrived intact, and
// are entirely consistent with the request having been carried out first. Callers
// that skip a corrective re-read because 「the route said what happened」 then skip
// it in exactly the case the re-read exists for: a probe that ran, wrote its
// cooldown, and lost the reply on the way back.
import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiFailure, modelsApi } from './modelsApi';

/** The failure `listSources` comes back with for a given response. */
const failureFor = async (respond: () => Response) => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => respond()),
  );
  try {
    await modelsApi.listSources();
  } catch (err) {
    return apiFailure(err);
  }
  throw new Error('expected the call to reject');
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('apiFailure — whether the route named the failure', () => {
  it('posts installation to the dedicated runtime route', async () => {
    const runtime = {
      contract_version: 7,
      manifest: { name: 'cliproxyapi', resolution: 'resolved', version: '1', source_sha: 'sha', assets: [] },
      status: { installed_version: null, verified: false, listening: null, health: 'installing', last_check: null },
    } as const;
    let installInit: RequestInit | undefined;
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/csrf-token') return Response.json({ csrf_token: 'csrf' });
      if (input === '/api/models/runtime/install') installInit = init;
      return Response.json({ runtime });
    });
    vi.stubGlobal('fetch', fetch);

    await expect(modelsApi.installRuntime()).resolves.toEqual(runtime);
    expect(installInit?.method).toBe('POST');
  });

  it('reads a route-declared code as named', async () => {
    const failure = await failureFor(() => Response.json({ error: 'discovery_failed' }, { status: 409 }));
    expect(failure).toMatchObject({ code: 'discovery_failed', serverNamed: true, responseStatus: 409 });
  });

  it('reads a body that would not parse as UNNAMED', async () => {
    // The server may well have written before this response was truncated.
    const failure = await failureFor(() => new Response('<html>502</html>', { status: 502 }));
    expect(failure).toMatchObject({ code: 'bad_response', serverNamed: false, responseStatus: 502 });
  });

  it('reads a status this client had to summarize itself as UNNAMED', async () => {
    // Parsed, and said nothing about what happened. `http_502` is this client's
    // sentence, not a route outcome, even though it arrives as the same error type.
    const failure = await failureFor(() => Response.json({}, { status: 502 }));
    expect(failure).toMatchObject({ code: 'http_502', serverNamed: false });
  });

  it('reads an ok:false envelope by the code it carries', async () => {
    const failure = await failureFor(() => Response.json({ ok: false, error: 'engine_down' }));
    expect(failure).toMatchObject({ code: 'engine_down', serverNamed: true });
  });

  it('is not one of ours at all when the failure came from our own code', async () => {
    // Unchanged, and the reason `serverNamed` could not simply be dropped in
    // favour of a null check: a TypeError from the render path is not a supply
    // refusal, and callers still have to be able to tell.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    await expect(modelsApi.listSources()).rejects.toThrow(TypeError);
    expect(apiFailure(new TypeError('Failed to fetch'))).toBeNull();
  });
});
