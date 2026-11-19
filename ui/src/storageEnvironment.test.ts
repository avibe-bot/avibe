/** @vitest-environment jsdom */
import { expect, it } from 'vitest';
declare const jsdom: { window: Pick<Window, 'localStorage' | 'sessionStorage'> };

it('uses the document’s own local and session storage, not the Node worker’s', () => {
  expect(window.localStorage).toBeInstanceOf(window.Storage);
  expect(window.sessionStorage).toBeInstanceOf(window.Storage);
  expect(window.localStorage).toBe(jsdom.window.localStorage);
  expect(window.sessionStorage).toBe(jsdom.window.sessionStorage);
});
