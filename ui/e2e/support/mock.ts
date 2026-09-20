// The mock upstream provider's control plane, reached ONLY over HTTP.
//
// `tests/e2e/drivers/mock_llm_upstream.py` belongs to the pytest lane; this
// lane never imports it. The contract used here is the frozen one from test
// plan §5a: `POST /__control/config` sets behavior, `GET /__control/requests`
// returns `{"requests": [...]}`, `DELETE /__control/requests` resets.
import type { APIRequestContext } from '@playwright/test';

import { MOCK_UPSTREAM_URL } from './env';

export type MockAuthMode = 'ok' | '401' | '403_banned' | '402' | '429' | 'quota_message' | '5xx';
/** Declared as values, not only as a union, so a caller can ask at runtime
 *  whether some protocol it read from elsewhere is one this mock can serve. */
export const MOCK_PROTOCOLS = ['anthropic', 'openai_responses', 'openai_chat'] as const;
export type MockProtocol = (typeof MOCK_PROTOCOLS)[number];
export type MockModelsEndpoint = 'ok' | 'http_404' | 'http_500' | 'timeout' | 'malformed_json';

export type MockConfig = {
  auth?: MockAuthMode;
  stream?: 'healthy' | 'interrupt_after_first_output' | 'pause_after_first_output';
  models?: unknown[];
  protocol?: MockProtocol;
  models_endpoint?: MockModelsEndpoint;
  model_errors?: Record<string, 'model_not_found'>;
};

export type CapturedRequest = {
  method: string;
  path: string;
  headers?: Record<string, string>;
  body?: unknown;
};

export class MockUpstream {
  private readonly request: APIRequestContext;

  constructor(request: APIRequestContext) {
    this.request = request;
  }

  private url(path: string): string {
    if (!MOCK_UPSTREAM_URL) throw new Error('mock upstream URL is not configured');
    return `${MOCK_UPSTREAM_URL}${path}`;
  }

  async configure(config: MockConfig): Promise<void> {
    const response = await this.request.post(this.url('/__control/config'), { data: config });
    if (!response.ok()) {
      throw new Error(`mock upstream refused config (${response.status()}): ${await response.text()}`);
    }
  }

  async resetRequests(): Promise<void> {
    const response = await this.request.delete(this.url('/__control/requests'));
    // A refused reset leaves the previous attempt's history in place, and B6
    // would then read a stale `/v1/` hit as this attempt's — suppressing the
    // #1818 classification the settle is trying to establish. Failing here
    // names the mock as the problem, before any verdict is read from it.
    if (!response.ok()) {
      throw new Error(`mock upstream refused request-log reset (${response.status()}): ${await response.text()}`);
    }
  }

  async requests(): Promise<CapturedRequest[]> {
    const response = await this.request.get(this.url('/__control/requests'));
    if (!response.ok()) throw new Error(`mock upstream request log unavailable (${response.status()})`);
    const payload = (await response.json()) as { requests?: CapturedRequest[] };
    return payload.requests ?? [];
  }

  /** True when the mock answers its control plane — the liveness probe a spec
   *  runs before trusting `VIBE_E2E_MOCK_UPSTREAM_URL`. */
  async reachable(): Promise<boolean> {
    if (!MOCK_UPSTREAM_URL) return false;
    try {
      return (await this.request.get(this.url('/__control/requests'), { timeout: 5_000 })).ok();
    } catch {
      return false;
    }
  }
}

/** An inventory in the Anthropic `GET /v1/models` shape, carrying the relay
 *  extension fields whose drop scenario B3 documents. The `id` stays typed
 *  because B3 reads it back out of storage and compares. */
export const anthropicInventory = (ids: string[]): { id: string; [key: string]: unknown }[] =>
  ids.map((id) => ({
    id,
    type: 'model',
    display_name: `${id} (upstream label)`,
    context_length: 200_000,
    pricing: { input: 3, output: 15 },
  }));
