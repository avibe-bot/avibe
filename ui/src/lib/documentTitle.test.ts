import { describe, expect, it } from 'vitest';

import { computeAppTitle } from './documentTitle';

describe('computeAppTitle', () => {
  it('prefers the configured instance name', () => {
    expect(
      computeAppTitle({
        ui: {
          instance_name: 'Desk',
          default_instance_name: 'alex',
          system_hostname: 'macbook',
        },
      }),
    ).toBe('Avibe - Desk');
  });

  it('uses the server-computed default when instance_name is blank', () => {
    expect(
      computeAppTitle({
        ui: {
          instance_name: '',
          default_instance_name: 'alex',
          system_hostname: 'macbook',
        },
      }),
    ).toBe('Avibe - alex');
  });

  it('falls back to system_hostname for older config payloads', () => {
    expect(
      computeAppTitle({
        ui: {
          instance_name: '',
          system_hostname: 'macbook',
        },
      }),
    ).toBe('Avibe - macbook');
  });
});
