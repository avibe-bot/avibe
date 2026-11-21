import { afterEach, beforeEach, vi } from 'vitest';

// Duplicate siblings can orphan DOM nodes on a route change even when the page
// appears correct on its first render. Every ChatPage suite treats this as a failure.
let restore: (() => void) | undefined;
beforeEach(() => {
  const original = console.error;
  const spy = vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    if (args.some((arg) => String(arg).includes('same key'))) {
      throw new Error(`React duplicate key: ${args.map(String).join(' ')}`);
    }
    original(...args);
  });
  restore = () => spy.mockRestore();
});
afterEach(() => restore?.());
