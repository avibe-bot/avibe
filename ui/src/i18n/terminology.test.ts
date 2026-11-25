import { describe, expect, it } from 'vitest';
import { desktopZh as zh } from './desktop';


describe('Chinese product terminology', () => {
  it('uses the localized app bar name throughout user-visible copy', () => {
    expect(JSON.stringify(zh)).not.toMatch(/\bDock\b/);
  });
});
