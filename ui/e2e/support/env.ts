// The frozen environment vocabulary (test plan §5a) and the reasons a spec
// skips. A skip message names what to start and where the instructions are, so
// a skipped run is actionable rather than mysterious.

const trimSlash = (value: string): string => value.replace(/\/+$/, '');

const NO_BASE_URL =
  'VIBE_E2E_BASE_URL is not set, and this suite has no default. It creates and '
  + 'deletes sources, rewrites route chains, flips agent modes and stops the gateway '
  + 'on whatever instance it is pointed at, so the instance has to be named on purpose. '
  + 'Point it at a hermetic instance of your own — never at the vibe service you use. '
  + 'See ui/e2e/README.md § "Against a local hermetic instance".';

/**
 * The instance under test.
 *
 * Deliberately has no fallback. A default of `http://127.0.0.1:5123` is the
 * port a developer's real `vibe` listens on, so an accidental `npm run e2e`
 * would mutate production state — and a README warning does not run. Refusing
 * to start does.
 */
export const BASE_URL = ((): string => {
  const raw = process.env.VIBE_E2E_BASE_URL?.trim();
  if (!raw) throw new Error(NO_BASE_URL);
  return trimSlash(raw);
})();

/**
 * The mock upstream provider's origin. This lane reaches it ONLY over HTTP —
 * the Python module is the pytest lane's and is never imported here. Specs that
 * need a controllable upstream skip when it is absent.
 */
export const MOCK_UPSTREAM_URL = process.env.VIBE_E2E_MOCK_UPSTREAM_URL
  ? trimSlash(process.env.VIBE_E2E_MOCK_UPSTREAM_URL)
  : null;

export const NO_MOCK_UPSTREAM =
  'VIBE_E2E_MOCK_UPSTREAM_URL is not set, so no controllable upstream is available. '
  + 'The driver that serves this contract is the pytest lane\'s '
  + '(tests/e2e/drivers/mock_llm_upstream.py) and does not ship in this PR; any server '
  + 'implementing the §5a control plane will do. See ui/e2e/README.md § "The mock upstream".';

/**
 * A source's Base URL as typed into the dialog. The mock serves `/v1/models`,
 * `/v1/messages`, `/v1/responses` and `/v1/chat/completions`, and the dialog's
 * own hint says a bare host resolves against the standard `/v1` path — so the
 * bare origin is what a user would type.
 */
export const mockBaseUrl = (): string => {
  if (!MOCK_UPSTREAM_URL) throw new Error(NO_MOCK_UPSTREAM);
  return MOCK_UPSTREAM_URL;
};

/** Display-name prefix every source this suite creates carries, so cleanup can
 *  find its own leftovers without guessing at the operator's real sources.
 *  Every create path uses it — including the ones that expect to FAIL, because
 *  a failure path that accidentally commits still has to be sweepable. */
export const E2E_SOURCE_PREFIX = 'e2e-playwright-';
