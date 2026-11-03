import { webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { vi } from 'vitest';
import {
  VAULT_SANDBOX_ORIGIN, VAULT_SANDBOX_RELEASE_PATH,
} from '../vaultSandboxManifest';

// Actual source-built sandbox artifact, not a mock implementation of integrity.
// The fixture is also loaded by Chromium in e2e/vault-integrity.
export function releaseBytes(url: string): Uint8Array {
  const path = new URL(url).pathname;
  if (!path.startsWith(VAULT_SANDBOX_RELEASE_PATH)) throw new Error(`Unexpected test fetch: ${url}`);
  return new Uint8Array(readFileSync(resolve(process.cwd(), 'e2e/vault-integrity/release', path.slice(VAULT_SANDBOX_RELEASE_PATH.length))));
}

export function releaseResponse(url: string, bytes = releaseBytes(url)): Response {
  const response = new Response(bytes as Uint8Array<ArrayBuffer>);
  Object.defineProperties(response, { url: { value: url }, type: { value: 'cors' } });
  return response;
}

export function stubReleaseFetch() {
  // jsdom has no secure-context algorithm; these release transport fixtures run
  // at its loopback URL. Entry regressions override this explicitly.
  vi.stubGlobal('isSecureContext', true);
  vi.stubGlobal('crypto', webcrypto);
  const fetch = vi.fn(async (input: string | URL | Request) => releaseResponse(String(input)));
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

export function stubBlankPopup() {
  const popup = { closed: false, location: { replace: vi.fn() }, close: vi.fn(function () { popup.closed = true; }) };
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
  return popup;
}

export function sandboxMessage(iframe: HTMLIFrameElement, data: unknown): void {
  window.dispatchEvent(new MessageEvent('message', { origin: VAULT_SANDBOX_ORIGIN, source: iframe.contentWindow, data }));
}
