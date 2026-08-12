import { describe, expect, it } from 'vitest';

import { localCalendarRelation } from './localCalendar';

describe('local calendar relation', () => {
  it.each([
    ['spring-forward', '2026-03-09T00:30:00-04:00', '2026-03-08T00:30:00-05:00'],
    ['fall-back', '2026-11-02T00:30:00-05:00', '2026-11-01T00:30:00-04:00'],
  ])('labels the prior New York calendar day across %s', (_name, now, prior) => {
    expect(localCalendarRelation(new Date(prior), new Date(now), 'America/New_York')).toBe('yesterday');
  });
});
