import { describe, expect, it } from 'vitest';

import {
  BACKEND_IDENTITY_ACCENT,
  backendVisual,
  SOURCE_IDENTITY_ACCENT,
  sourceAccent,
} from './vendorMeta';

describe('Model Hub semantic identity palette', () => {
  it('keeps every source authority row connected to the source consumer', () => {
    for (const [identity, accent] of Object.entries(SOURCE_IDENTITY_ACCENT)) {
      const source = identity === 'native_cli'
        ? { kind: 'subscription' as const, supply_channel: 'native_cli' as const }
        : { kind: identity as 'subscription' | 'api_key', supply_channel: 'hub' as const };
      expect(sourceAccent(source)).toBe(accent);
    }
  });

  it('keeps every backend authority row connected to the backend consumer', () => {
    for (const [backend, accent] of Object.entries(BACKEND_IDENTITY_ACCENT)) {
      expect(backendVisual(backend as keyof typeof BACKEND_IDENTITY_ACCENT).accent).toBe(accent);
    }
  });
});
