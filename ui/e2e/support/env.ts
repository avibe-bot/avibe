// The frozen environment vocabulary (test plan §5a) and the reasons a spec
// skips. A skip message names what to start and where the instructions are, so
// a skipped run is actionable rather than mysterious.

const trimSlash = (value: string): string => value.replace(/\/+$/, '');

export const BASE_URL = trimSlash(process.env.VIBE_E2E_BASE_URL ?? 'http://127.0.0.1:5123');

/**
 * The mock upstream provider's origin. This lane reaches it ONLY over HTTP —
 * the Python module is the pytest lane's and is never imported here. Specs that
 * need a controllable upstream skip when it is absent.
 */
export const MOCK_UPSTREAM_URL = process.env.VIBE_E2E_MOCK_UPSTREAM_URL
  ? trimSlash(process.env.VIBE_E2E_MOCK_UPSTREAM_URL)
  : null;

export const NO_MOCK_UPSTREAM =
  'VIBE_E2E_MOCK_UPSTREAM_URL is not set. Start the mock upstream provider '
  + '(python3 tests/e2e/drivers/mock_llm_upstream.py --port 9931) and export the env. '
  + 'See ui/e2e/README.md.';

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
 *  find its own leftovers without guessing at the operator's real sources. */
export const E2E_SOURCE_PREFIX = 'e2e-playwright-';
