import { describe, expect, it } from 'vitest';

import { classifyObservation, protocolOrderWithHint } from './addApiKeyState';
import { CONTRACT_VERSION, SOURCE_PROTOCOLS, type SourceObservation } from './types';

const observation = (patch: Partial<SourceObservation>): SourceObservation => ({
  contract_version: CONTRACT_VERSION,
  outcome: 'observed',
  reachable: true,
  authenticated: 'authenticated',
  protocol: 'openai_chat',
  discovery: 'succeeded',
  models: ['model-a'],
  ...patch,
});

describe('Add API key observation state', () => {
  it('turns a one-time hint into a complete probe order without duplicating the protocol table', () => {
    const order = protocolOrderWithHint('openai_responses');
    expect(order?.[0]).toBe('openai_responses');
    expect(new Set(order)).toEqual(new Set(SOURCE_PROTOCOLS));
    expect(order).toHaveLength(SOURCE_PROTOCOLS.length);
  });

  it('separates proven inventory failure from unknown protocol and connectivity failures', () => {
    const inventory = observation({ discovery: 'failed', models: [] });
    expect(classifyObservation(inventory)).toEqual({ kind: 'inventory', observation: inventory });
    expect(classifyObservation(observation({
      outcome: 'ambiguous',
      authenticated: 'unknown',
      protocol: null,
      discovery: 'not_attempted',
      models: [],
    })).kind).toBe('undetermined');
    expect(classifyObservation(observation({
      outcome: 'authentication_failed',
      authenticated: 'rejected',
      protocol: null,
      discovery: 'not_attempted',
      models: [],
    }))).toEqual({ kind: 'failure', cause: 'auth' });
    expect(classifyObservation(observation({
      outcome: 'timeout',
      reachable: null,
      authenticated: 'unknown',
      protocol: null,
      discovery: 'not_attempted',
      models: [],
    }))).toEqual({ kind: 'failure', cause: 'network' });
    expect(classifyObservation(observation({
      outcome: 'unreachable',
      reachable: false,
      authenticated: 'unknown',
      protocol: null,
      discovery: 'not_attempted',
      models: [],
    }))).toEqual({ kind: 'failure', cause: 'network' });
  });
});
