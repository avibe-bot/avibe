import { describe, expect, it } from 'vitest';
import { PREFERRED_TEST_MODEL_IDS, selectTestModel } from './testModelPreference';
import type { SuppliedModel } from './types';

const rows = (...ids: string[]): SuppliedModel[] => ids.map((id) => ({
  id, display_name: null, origin: 'discovered', reasoning_efforts: [], reasoning_efforts_source: null,
}));

describe('test model preference', () => {
  it('intersects the preference list with inventory in preference order', () => {
    const inventory = rows('old-model', ...[...PREFERRED_TEST_MODEL_IDS].reverse());
    expect(selectTestModel(inventory)).toBe(PREFERRED_TEST_MODEL_IDS[0]);
    expect(inventory[0].id).toBe('old-model');
  });

  it('uses the first inventory model without guessing names or sorting upstream rows', () => {
    expect(selectTestModel(rows('z-model', 'a-model'))).toBe('z-model');
    expect(selectTestModel(rows(`${PREFERRED_TEST_MODEL_IDS[0]}-other`, 'a-model')))
      .toBe(`${PREFERRED_TEST_MODEL_IDS[0]}-other`);
  });

  it('preserves any valid user selection and excludes retired rows', () => {
    expect(selectTestModel(rows(PREFERRED_TEST_MODEL_IDS[0], 'chosen'), 'chosen')).toBe('chosen');
    expect(selectTestModel([
      { ...rows(PREFERRED_TEST_MODEL_IDS[0])[0], retired: true },
      ...rows('available'),
    ], PREFERRED_TEST_MODEL_IDS[0])).toBe('available');
    expect(selectTestModel([])).toBe('');
  });
});
