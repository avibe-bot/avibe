// The frozen environment vocabulary (test plan §5a) and the reasons a spec
// skips. A skip message names what to start and where the instructions are, so
// a skipped run is actionable rather than mysterious.

const trimSlash = (value: string): string => value.replace(/\/+$/, '');

const NO_BASE_URL =
  'VIBE_E2E_BASE_URL is not set, and this suite has no default. It creates and '
  + 'deletes sources, rewrites route chains, flips agent modes and stops the gateway '
  + 'on whatever instance it is pointed at, so the instance has to be named on purpose. '
  + 'Point it at a hermetic instance of your own — never at the vibe service you use — '
  + 'and name that same instance in VIBE_E2E_DESTRUCTIVE_TARGET. '
  + 'See ui/e2e/README.md § "Against a local hermetic instance".';

/** The name of the consent variable, quoted often enough below to be worth one
 *  spelling. */
const CONSENT = 'VIBE_E2E_DESTRUCTIVE_TARGET';

/** The port the packaged service listens on unless its operator moved it
 *  (`setup_port`, config/v2_config.py). */
const PACKAGED_PORT = '5123';

const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]', '::1', '0.0.0.0']);

/**
 * The one target refused outright, and the only one the suite can recognize
 * without being told: the packaged default ON THIS MACHINE.
 *
 * Scoped to loopback deliberately. `5123` on this host is the port the `vibe`
 * the operator uses is listening on right now; the same number on someone
 * else's host is just a port, and refusing it would be this file guessing about
 * a machine it cannot see — which is the failure mode the rest of the check
 * exists to avoid.
 */
const isPackagedLocalService = (url: URL): boolean =>
  LOOPBACK.has(url.hostname) && url.port === PACKAGED_PORT;

const REFUSED_PACKAGED_PORT =
  `The target is loopback port ${PACKAGED_PORT}, which is where the packaged service listens `
  + 'unless its operator moved it — so it is almost certainly the vibe you use. This suite '
  + 'force-deletes sources, rewrites route chains, flips agent modes and stops the gateway, '
  + 'and no consent makes that address acceptable: the likeliest way to reach it is pasting '
  + 'the URL of the UI you have open. Start the hermetic instance on any other port. '
  + 'See ui/e2e/README.md § "Against a local hermetic instance".';

const notAUrl = (name: string, raw: string): string =>
  `${name} is not an http(s) URL: ${raw}.`;

/** One spelling for both variables, so `http://host:5199/` and `http://host:5199`
 *  are the same target rather than a mismatch the operator has to debug. */
const normalize = (raw: string): string | null => {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  return trimSlash(url.toString());
};

const withoutConsent = (target: string, state: string): string =>
  `${CONSENT} ${state}, and this suite runs only against the instance it names. `
  + `Set ${CONSENT}=${target} to state that what is at ${target} — its sources, its route `
  + 'chains, its agent modes, its gateway — is disposable. Nothing a running instance '
  + 'reports says whether it is; a hermetic one and the one you work on serve the same '
  + 'API, so the only party who can answer is you. The variable names the target rather '
  + 'than being a flag so that an answer given for one instance does not quietly carry '
  + 'over to the next one VIBE_E2E_BASE_URL is pointed at. '
  + 'See ui/e2e/README.md § "Against a local hermetic instance".';

/**
 * The instance under test, admitted only once it has been named twice.
 *
 * `VIBE_E2E_BASE_URL` deliberately has no fallback: a default of
 * `http://127.0.0.1:5123` is where a developer's real `vibe` listens, so an
 * accidental `npm run e2e` would mutate production state — and a README warning
 * does not run. But a required variable only proves the operator typed A url,
 * not that they typed a disposable one, and this suite is destructive on every
 * axis the Model Hub has. So the target must also be consented to by name, and
 * the one address that is recognizably the local service is refused outright.
 */
export const BASE_URL = ((): string => {
  const raw = process.env.VIBE_E2E_BASE_URL?.trim();
  if (!raw) throw new Error(NO_BASE_URL);
  const target = normalize(raw);
  if (!target) throw new Error(notAUrl('VIBE_E2E_BASE_URL', raw));
  if (isPackagedLocalService(new URL(target))) throw new Error(REFUSED_PACKAGED_PORT);
  const declared = process.env[CONSENT]?.trim();
  const consented = declared ? normalize(declared) : null;
  if (consented !== target) {
    throw new Error(
      withoutConsent(
        target,
        !declared ? 'is not set' : consented ? `names ${consented}` : `is ${declared}, which is not an http(s) URL`,
      ),
    );
  }
  return target;
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
