import type {
  SigningAddresses,
  VaultSignedOperationContext,
  VaultWebAuthnRegistrationPayload,
} from '@/context/ApiContext';
import type { BlindBox, ProtectedRecordEnvelope, SignatureResult, SignatureScheme } from './vaultCrypto';
import {
  VAULT_SANDBOX_EXPECTED_BUILD_HASH,
  VAULT_SANDBOX_VERSION,
  VAULT_SANDBOX_RELEASE_PATH,
  VAULT_SANDBOX_IFRAME_URL,
  VAULT_SANDBOX_IFRAME_RESOURCE_PATH,
  VAULT_SANDBOX_MANIFEST_PATH,
  VAULT_SANDBOX_ORIGIN,
  VAULT_SANDBOX_PINNED_MANIFEST,
  VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS,
} from './vaultSandboxManifest';
import { assertVaultEntryAvailable } from './vaultEntry';
import { openLinkInNewContext } from './pwaNavigation';
import { getVaultSandboxAppearance, type VaultSandboxAppearance } from './vaultSandboxAppearance';
import { getVaultSandboxPolicy, refreshVaultSandboxPolicy, type VaultSessionPolicy } from './vaultSandboxPolicy';
import { buildVaultConfirmSurface, type VaultConfirmSurface } from './vaultConfirmSurface';

const CHANNEL = 'avibe.vault.crypto';
const VERSION = 2;
// Protocol v2 operation surface (vault-sandbox #9): `unseal`→`reveal`, `releaseDEK`→`approveRelease`.
const REQUIRED_OPS = ['status', 'setup', 'unlock', 'lock', 'seal', 'reveal', 'sign', 'approveRelease'] as const;
const DEFAULT_TIMEOUT_MS = 15_000;
// Ops that may raise an in-sandbox card (confirm / passkey / plaintext display) can sit open for
// as long as the user takes to act, so they get the long ceremony timeout regardless of tier.
const INTERACTIVE_TIMEOUT_MS = 5 * 60_000;
// Cadence for refreshing the parent surface attestation while an embedded card is up. This remains
// useful for compatibility and diagnostics, but high-risk authorization itself is completed in a
// top-level sandbox window and does not trust parent hit-test claims.
const SURFACE_REFRESH_INTERVAL_MS = 10_000;

type VaultSandboxOp = (typeof REQUIRED_OPS)[number] | 'handshake';
type ParentOnlySandboxOp = 'set-appearance';
type PendingRequest = {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
  timer: number;
};

type SandboxBuild = {
  sandboxVersion?: string;
  buildHash?: string;
};

type ReadyMessage = {
  type: 'ready';
  channel: typeof CHANNEL;
  version: typeof VERSION;
  build?: SandboxBuild;
  capabilities?: { operations?: string[] };
};

type TerminalMessage =
  | { channel: typeof CHANNEL; version: typeof VERSION; id: string; ok: true; result: unknown }
  | { channel: typeof CHANNEL; version: typeof VERSION; id: string; ok: false; error: { code?: string; message?: string; retryable?: boolean } | string };

/** Sandbox→parent one-way notification (protocol v2 §6.4). */
type EventMessage = {
  channel: typeof CHANNEL;
  version: typeof VERSION;
  kind: 'event';
  event: 'vault.state' | 'ui.show' | 'ui.hide';
  payload?: unknown;
};

export type VaultSandboxState = 'needs-setup' | 'locked' | 'unlocked';

export type VaultStateReason = 'unlock' | 'renew' | 'manual-lock' | 'auto-lock' | 'unload';

/** The `vault.state` event payload — the single clock the parent mirrors (protocol v2 §6.4). */
export type VaultStateEvent = {
  state: VaultSandboxState;
  expiresAt?: number | null;
  reason: VaultStateReason;
};

export type VaultSandboxStatusResult = {
  state: VaultSandboxState;
  expiresAt?: number | null;
  freshSetup?: boolean;
  policy?: VaultSessionPolicy;
  session?: { expiresAt?: number; strict?: boolean };
};

export type VaultSandboxSetupResult = {
  wrapMeta: string;
  rpId: string;
  credentialId: string;
  authzRegistration?: VaultWebAuthnRegistrationPayload;
  state: 'unlocked';
  expiresAt?: number;
  policy?: VaultSessionPolicy;
};

export type VaultSandboxUnlockResult = {
  state: 'unlocked';
  expiresAt?: number;
  wrapMeta?: string;
  policy?: VaultSessionPolicy;
};

export type VaultSandboxSealResult = {
  envelope: ProtectedRecordEnvelope;
  establishingVmk: boolean;
  publicKey?: string;
  addresses?: SigningAddresses;
};

/** One member of a batch `approveRelease` — the sandbox releases one blind box per item. */
export type ApproveReleaseItem = {
  material: ProtectedUnlockMaterialLike;
  context: VaultSignedOperationContext;
};

export type VaultSandboxSigningContext = Record<string, unknown>;

export class VaultSandboxError extends Error {
  readonly code: string;
  readonly retryable: boolean;

  constructor(code: string, message: string, retryable = false) {
    super(message);
    this.name = 'VaultSandboxError';
    this.code = code;
    this.retryable = retryable;
  }
}

let generationController = new AbortController();
const clients = new Set<VaultSandboxClient>();
const authorizationWindows = new Map<string, VaultAuthorizationWindow & { ready: Promise<void>; assertOpen: () => void }>();
let singleton: Promise<VaultSandboxClient> | null = null;
let activeClient: VaultSandboxClient | null = null;
// Bumped on every reset. A create() that started before a reset checks this after its handshake:
// if it advanced, the client handshook under the pre-reset (stale) policy and is discarded instead
// of being adopted, so a reset takes effect even for an in-flight client creation.
let clientGeneration = 0;

// Module-level so subscribers survive client recreation (a singleton reset on error) and can
// register before any client exists — the active client forwards every `vault.state` event here.
const vaultStateListeners = new Set<(event: VaultStateEvent) => void>();

/**
 * Subscribe to sandbox `vault.state` events (unlock / renew / lock transitions). This is the
 * single clock the parent renders off of — {@link useProtectedVault} wires one listener that
 * mirrors state + `expiresAt`. Returns an unsubscribe function.
 */
export function subscribeVaultStateEvents(listener: (event: VaultStateEvent) => void): () => void {
  vaultStateListeners.add(listener);
  return () => {
    vaultStateListeners.delete(listener);
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value != null && !Array.isArray(value);
}

function randomId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export type VaultAuthorizationWindow = {
  id: string;
  close: () => void;
};

/** Reserve user activation without loading protected code. Each popup is inert
 * until its own verification finishes. The RPC awaits that result by id, so a
 * failed/cancelled popup can never silently fall back to an unverified window. */
export function openVaultAuthorizationWindow(): VaultAuthorizationWindow | null {
  assertVaultEntryAvailable();
  const id = randomId();
  const generation = clientGeneration;
  const signal = generationController.signal;
  const popup = openLinkInNewContext('about:blank', 'popup,width=520,height=760');
  if (!popup) return null;
  let closed = false;
  const close = (): void => {
    closed = true;
    authorizationWindows.delete(id);
    try { if (!popup.closed) popup.close(); } catch { /* Already closed. */ }
  };
  const assertOpen = (): void => {
    if (closed || popup.closed || generation !== clientGeneration) {
      throw new VaultSandboxError('sandbox_reset', 'Sandbox authorization was cancelled', true);
    }
  };
  const ready = verifyVaultSandboxIntegrity(signal).then(() => {
    assertOpen();
    const url = new URL(VAULT_SANDBOX_IFRAME_URL);
    url.hash = new URLSearchParams({ mode: 'authorize', id, ...getVaultSandboxAppearance() }).toString();
    popup.location.replace(url.toString());
  }).catch((error: unknown) => {
    // Keep the failed promise until the caller closes the handle: the RPC must
    // receive the integrity error, not reinterpret failure as a blocked popup.
    const cancelled = closed || popup.closed || generation !== clientGeneration;
    try { if (!popup.closed) popup.close(); } catch { /* Already closed. */ }
    if (cancelled) throw new VaultSandboxError('sandbox_reset', 'Sandbox authorization was cancelled', true);
    throw error;
  });
  void ready.catch(() => undefined); // The eventual RPC observes this rejection.
  const handle = { id, close, ready, assertOpen };
  authorizationWindows.set(id, handle);
  return handle;
}

function base64(bytes: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)));
}

async function sha256Subresource(value: ArrayBuffer): Promise<string> {
  return `sha256-${base64(await crypto.subtle.digest('SHA-256', value))}`;
}

function failIntegrity(message: string): never {
  throw new VaultSandboxError('sandbox_integrity_failed', message);
}

/** No successful cache: every new frame or parent-created popup checks again.
 * Hash the manifest bytes too; redirects, CORS/network/parse failures are never
 * an invitation to trust a mutable endpoint or downgrade protected custody. */
export async function verifyVaultSandboxIntegrity(signal?: AbortSignal): Promise<void> {
  const controller = new AbortController();
  const abort = (): void => controller.abort();
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) abort();
  const timer = setTimeout(abort, DEFAULT_TIMEOUT_MS);
  try {
    if (typeof window === 'undefined' || typeof crypto === 'undefined' || !crypto.subtle) {
      failIntegrity('Sandbox integrity requires Web Crypto in a browser context');
    }
    const pin = VAULT_SANDBOX_PINNED_MANIFEST;
    const resources: Record<string, string> = pin.resources;
    if (pin.schema !== 1 || pin.algorithm !== 'sha256' || VAULT_SANDBOX_ORIGIN !== 'https://sandbox.avibe.bot'
      || !/^[a-f0-9]{64}$/.test(pin.buildHash)
      || !VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS.includes(VAULT_SANDBOX_IFRAME_RESOURCE_PATH)
      || VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS.some((path) => !path.startsWith(VAULT_SANDBOX_RELEASE_PATH)
        || !/^sha256-[A-Za-z0-9+/]{43}=$/.test(resources[path]))) {
      failIntegrity('Invalid locally pinned sandbox release');
    }
    const verify = async (path: string, expected: string): Promise<ArrayBuffer> => {
      const url = `${VAULT_SANDBOX_ORIGIN}${path}`;
      const response = await fetch(url, {
        cache: 'no-store', mode: 'cors', credentials: 'omit', redirect: 'error', signal: controller.signal,
      });
      if (response.status !== 200 || response.redirected || response.url !== url || response.type !== 'cors') {
        failIntegrity(`Sandbox resource was missing, redirected, or not CORS-readable: ${path}`);
      }
      const bytes = await response.arrayBuffer();
      if (await sha256Subresource(bytes) !== expected) failIntegrity(`Sandbox resource hash mismatch: ${path}`);
      return bytes;
    };
    const manifestBytes = await verify(VAULT_SANDBOX_MANIFEST_PATH, pin.manifestHash);
    const manifest = JSON.parse(new TextDecoder().decode(manifestBytes)) as unknown;
    if (!isObject(manifest) || manifest.buildHash !== pin.buildHash || manifest.sandboxOrigin !== pin.sandboxOrigin
      || manifest.schema !== pin.schema || manifest.algorithm !== pin.algorithm
      || JSON.stringify(manifest.resources) !== JSON.stringify(pin.resources)) failIntegrity('Sandbox manifest identity mismatch');
    await Promise.all(VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS.map((path) => verify(path, resources[path])));
    if (controller.signal.aborted) failIntegrity('Sandbox verification was cancelled or timed out');
  } catch (error) {
    if (signal?.aborted) throw new VaultSandboxError('sandbox_reset', 'Sandbox verification was cancelled', true);
    if (error instanceof VaultSandboxError) throw error;
    failIntegrity('Unable to verify the pinned sandbox release. Check connectivity and update Avibe if necessary.');
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
    controller.abort();
  }
}

function serializeSandboxError(error: { code?: string; message?: string; retryable?: boolean } | string): VaultSandboxError {
  if (typeof error === 'string') return new VaultSandboxError('sandbox_error', error);
  return new VaultSandboxError(
    typeof error?.code === 'string' ? error.code : 'sandbox_error',
    typeof error?.message === 'string' ? error.message : 'Sandbox request failed',
    Boolean(error?.retryable),
  );
}

function parseReadyMessage(data: unknown): ReadyMessage | null {
  if (!isObject(data)) return null;
  if (data.type !== 'ready' || data.channel !== CHANNEL || data.version !== VERSION) return null;
  return data as ReadyMessage;
}

function parseTerminalMessage(data: unknown): TerminalMessage | null {
  if (!isObject(data)) return null;
  if (data.channel !== CHANNEL || data.version !== VERSION || typeof data.id !== 'string') return null;
  if (data.ok === true && 'result' in data) return data as TerminalMessage;
  if (data.ok === false && 'error' in data) return data as TerminalMessage;
  return null;
}

function parseEventMessage(data: unknown): EventMessage | null {
  if (!isObject(data)) return null;
  if (data.channel !== CHANNEL || data.version !== VERSION || data.kind !== 'event') return null;
  if (data.event !== 'vault.state' && data.event !== 'ui.show' && data.event !== 'ui.hide') return null;
  return data as EventMessage;
}

function parseVaultStateEvent(payload: unknown): VaultStateEvent | null {
  if (!isObject(payload)) return null;
  const state = payload.state;
  if (state !== 'needs-setup' && state !== 'locked' && state !== 'unlocked') return null;
  const reason = payload.reason;
  const validReason =
    reason === 'unlock' || reason === 'renew' || reason === 'manual-lock' || reason === 'auto-lock' || reason === 'unload';
  return {
    state,
    reason: validReason ? reason : 'manual-lock',
    expiresAt: typeof payload.expiresAt === 'number' && Number.isFinite(payload.expiresAt) ? payload.expiresAt : null,
  };
}

/**
 * One-shot IntersectionObserver reading of an element's on-screen visibility, mirroring the sandbox's
 * own self-check (`trackVisibility` occlusion detection where the engine supports it, ratio fallback
 * otherwise) so the parent attests visibility the same way the sandbox measures itself. Resolves on
 * the first observer entry or a short timeout so it never hangs.
 */
function observeElementVisibility(target: Element): Promise<{ ratio: number; visible: boolean }> {
  if (typeof IntersectionObserver === 'undefined') return Promise.resolve({ ratio: 0, visible: false });
  return new Promise((resolve) => {
    let settled = false;
    let timer = 0;
    const finish = (entry?: IntersectionObserverEntry): void => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      observer.disconnect();
      if (!entry) {
        resolve({ ratio: 0, visible: false });
        return;
      }
      const isVisible = (entry as IntersectionObserverEntry & { isVisible?: boolean }).isVisible;
      resolve({
        ratio: entry.intersectionRatio,
        visible: isVisible === undefined ? entry.intersectionRatio >= 0.99 : isVisible === true,
      });
    };
    const observer = new IntersectionObserver((entries) => finish(entries[0]), {
      threshold: [0, 0.99, 1],
      trackVisibility: true,
      delay: 100,
    } as IntersectionObserverInit);
    observer.observe(target);
    timer = window.setTimeout(() => finish(), 250);
  });
}

export class VaultSandboxClient {
  private iframe: HTMLIFrameElement;
  private backdrop: HTMLDivElement | null = null;
  private pending = new Map<string, PendingRequest>();
  private readyPromise: Promise<ReadyMessage>;
  private handshaken = false;
  private modalVisible = false;
  // Ids of in-flight interactive requests whose launcher card keeps the iframe expanded. While the
  // modal is up, optional geometry evidence is refreshed; final high-risk authorization happens in
  // the top-level sandbox window and never trusts a parent hit-test claim.
  private interactiveRequests = new Set<string>();
  private surfaceRefreshTimer: number | null = null;
  private destroyed = false;
  private cancelReady: (() => void) | null = null;

  private constructor(iframe: HTMLIFrameElement) {
    this.iframe = iframe;
    this.readyPromise = this.waitForReady();
    window.addEventListener('message', this.handleMessage);
    clients.add(this);
  }

  static async create(): Promise<VaultSandboxClient> {
    assertVaultEntryAvailable();
    // Snapshot the reset generation so we can discard this client if a reset lands mid-creation.
    const generation = clientGeneration;
    await verifyVaultSandboxIntegrity(generationController.signal);
    if (generation !== clientGeneration) throw new VaultSandboxError('sandbox_reset', 'Sandbox was reset during verification', true);
    const iframe = document.createElement('iframe');
    iframe.title = 'Avibe protected vault sandbox';
    iframe.allow = 'publickey-credentials-get; publickey-credentials-create; clipboard-write';
    iframe.referrerPolicy = 'no-referrer';
    iframe.style.position = 'fixed';
    iframe.style.top = '50%';
    iframe.style.left = '50%';
    iframe.style.transform = 'translate(-50%, -50%)';
    iframe.style.border = '0';
    iframe.style.background = 'transparent';
    iframe.style.zIndex = '2147483647';
    iframe.style.colorScheme = 'normal';
    iframe.style.borderRadius = '16px';
    iframe.style.overflow = 'hidden';
    iframe.style.boxShadow = 'none';
    // At rest the sandbox is a headless RPC worker: keep it in the DOM (so
    // postMessage works) but 0-sized + hidden so it never covers the app. It
    // only expands to a centered modal while the sandbox asks for its slot via
    // a `ui.show` event — see setModalVisible().
    iframe.style.width = '0';
    iframe.style.height = '0';
    iframe.style.visibility = 'hidden';
    iframe.style.pointerEvents = 'none';

    const client = new VaultSandboxClient(iframe);
    iframe.src = VAULT_SANDBOX_IFRAME_URL;
    document.body.appendChild(iframe);
    try {
      await client.handshake();
      if (generation !== clientGeneration) throw new VaultSandboxError('sandbox_reset', 'Sandbox client was reset during creation', true);
      activeClient = client;
      return client;
    } catch (error) {
      client.destroy();
      throw error;
    }
  }

  private get target(): Window {
    this.assertLive();
    const target = this.iframe.contentWindow;
    if (!target) throw new VaultSandboxError('sandbox_unavailable', 'Sandbox iframe is unavailable', true);
    return target;
  }

  private assertLive(): void {
    if (this.destroyed) throw new VaultSandboxError('sandbox_reset', 'Sandbox client was reset', true);
  }

  private ensureBackdrop(): HTMLDivElement {
    if (this.backdrop?.isConnected) return this.backdrop;
    const backdrop = document.createElement('div');
    backdrop.setAttribute('aria-hidden', 'true');
    backdrop.style.position = 'fixed';
    backdrop.style.inset = '0';
    backdrop.style.background = 'rgba(0, 0, 0, 0.5)';
    backdrop.style.zIndex = '2147483646';
    backdrop.style.pointerEvents = 'auto';
    document.body.insertBefore(backdrop, this.iframe);
    this.backdrop = backdrop;
    return backdrop;
  }

  // Expand to a lightweight centered modal while the sandbox holds its slot (`ui.show`), otherwise
  // collapse to a hidden 0-size worker (`ui.hide`) so silent R1/R2 ops never cover the app. Driven
  // by sandbox events, not per-call flags — a silent operation emits no `ui.show` and stays hidden.
  private setModalVisible(visible: boolean): void {
    this.modalVisible = visible;
    if (visible) {
      this.ensureBackdrop();
    } else {
      this.backdrop?.remove();
      this.backdrop = null;
    }
    this.iframe.style.width = visible ? 'min(440px, 92vw)' : '0';
    this.iframe.style.height = visible ? 'min(640px, 88vh)' : '0';
    this.iframe.style.visibility = visible ? 'visible' : 'hidden';
    this.iframe.style.pointerEvents = visible ? 'auto' : 'none';
    this.iframe.style.boxShadow = visible ? '0 24px 80px rgba(15, 23, 42, 0.38)' : 'none';
    // The expanded modal is the sandbox's on-screen confirm surface. Attest it to the sandbox while
    // it's up, and stop once it collapses (protocol v2 §6.6). Refresh starts only after this
    // expansion so the measurement reflects the visible modal, not the 0-size headless worker.
    if (visible) {
      this.startSurfaceRefresh();
    } else {
      this.stopSurfaceRefresh();
    }
  }

  /**
   * Measure the sandbox iframe in the parent (embedder) document and package geometry/observer
   * evidence for compatibility and diagnostics. No parent hit-test claim is sent because the
   * embedder is not a trusted security authority for the final authorization.
   */
  private async measureSurface(): Promise<VaultConfirmSurface | null> {
    if (typeof window === 'undefined' || !this.iframe.isConnected) return null;
    const rect = this.iframe.getBoundingClientRect();
    const style = window.getComputedStyle(this.iframe);
    const observed = await observeElementVisibility(this.iframe);
    return buildVaultConfirmSurface({
      frameWidth: rect.width,
      frameHeight: rect.height,
      intersectionRatio: observed.ratio,
      visibleByIntersectionObserver: observed.visible,
      opacity: style.opacity,
      pointerEvents: style.pointerEvents,
      sampledAt: Date.now(),
    });
  }

  private startSurfaceRefresh(): void {
    this.stopSurfaceRefresh();
    // Emit once immediately (right after the modal expanded) then on a fixed cadence well under the
    // sandbox's 60 s staleness cap, so a fresh attestation is always waiting when the user confirms.
    void this.emitSurfaceAttestation();
    this.surfaceRefreshTimer = window.setInterval(() => {
      void this.emitSurfaceAttestation();
    }, SURFACE_REFRESH_INTERVAL_MS);
  }

  private stopSurfaceRefresh(): void {
    if (this.surfaceRefreshTimer !== null) {
      window.clearInterval(this.surfaceRefreshTimer);
      this.surfaceRefreshTimer = null;
    }
  }

  /**
   * Refresh the parent surface attestation for every in-flight interactive request via a
   * `confirm.surface` event carrying the live rpc request id (protocol v2 §6.6 / §13). One
   * measurement is shared across all active ceremonies (in practice the modal hosts one at a time).
   */
  private async emitSurfaceAttestation(): Promise<void> {
    if (this.interactiveRequests.size === 0) return;
    const surface = await this.measureSurface();
    if (!surface) return;
    // The set may have drained (request settled) while we awaited the observer reading.
    for (const id of [...this.interactiveRequests]) {
      this.postSurfaceEvent(id, surface);
    }
  }

  private postSurfaceEvent(id: string, surface: VaultConfirmSurface): void {
    try {
      this.target.postMessage(
        { channel: CHANNEL, version: VERSION, kind: 'event', event: 'confirm.surface', id, surface },
        VAULT_SANDBOX_ORIGIN,
      );
    } catch {
      // The iframe may be mid-teardown or already removed; the next real request surfaces staleness.
    }
  }

  private waitForReady(): Promise<ReadyMessage> {
    return new Promise((resolve, reject) => {
      const cleanup = (): void => {
        window.clearTimeout(timer);
        window.removeEventListener('message', onMessage);
        this.cancelReady = null;
      };
      const timer = window.setTimeout(() => {
        cleanup();
        reject(new VaultSandboxError('sandbox_ready_timeout', 'Sandbox did not become ready', true));
      }, DEFAULT_TIMEOUT_MS);
      this.cancelReady = () => {
        cleanup();
        reject(new VaultSandboxError('sandbox_reset', 'Sandbox client was reset', true));
      };
      const onMessage = (event: MessageEvent) => {
        if (event.origin !== VAULT_SANDBOX_ORIGIN || event.source !== this.iframe.contentWindow) return;
        const ready = parseReadyMessage(event.data);
        if (!ready) return;
        cleanup();
        const operations = ready.capabilities?.operations ?? [];
        const missing = REQUIRED_OPS.filter((op) => !operations.includes(op));
        if (ready.build?.buildHash !== VAULT_SANDBOX_EXPECTED_BUILD_HASH || ready.build?.sandboxVersion !== VAULT_SANDBOX_VERSION) {
          reject(new VaultSandboxError('sandbox_build_mismatch', 'Sandbox build hash does not match the pinned manifest'));
        } else if (missing.length > 0) {
          reject(new VaultSandboxError('sandbox_capability_mismatch', `Sandbox is missing operations: ${missing.join(', ')}`));
        } else {
          resolve(ready);
        }
      };
      window.addEventListener('message', onMessage);
    });
  }

  private handleMessage = (event: MessageEvent): void => {
    if (event.origin !== VAULT_SANDBOX_ORIGIN || event.source !== this.iframe.contentWindow) return;
    const eventMessage = parseEventMessage(event.data);
    if (eventMessage) {
      if (!this.handshaken) return;
      this.handleEvent(eventMessage);
      return;
    }
    const reply = parseTerminalMessage(event.data);
    if (!reply) return;
    const pending = this.pending.get(reply.id);
    if (!pending) return;
    this.pending.delete(reply.id);
    this.interactiveRequests.delete(reply.id);
    window.clearTimeout(pending.timer);
    // Safety net: if the sandbox errored out of a card without emitting `ui.hide`, collapse once
    // the last in-flight request settles so a stale modal can't strand the app behind a backdrop.
    if (this.pending.size === 0 && this.modalVisible) this.setModalVisible(false);
    if (reply.ok) {
      pending.resolve(reply.result);
    } else {
      pending.reject(serializeSandboxError(reply.error));
    }
  };

  private handleEvent(message: EventMessage): void {
    if (message.event === 'ui.show') {
      this.setModalVisible(true);
      return;
    }
    if (message.event === 'ui.hide') {
      this.setModalVisible(false);
      return;
    }
    const state = parseVaultStateEvent(message.payload);
    if (!state) return;
    for (const listener of [...vaultStateListeners]) listener(state);
  }

  private async handshake(): Promise<void> {
    await this.readyPromise;
    // Pull the daemon-persisted policy so the very first ceremony runs under the configured
    // window/strict values; best-effort — a failure leaves the default policy in place.
    await refreshVaultSandboxPolicy();
    this.assertLive();
    const result = await this.request<unknown>(
      'handshake',
      {
        parentOrigin: window.location.origin,
        nonce: randomId(),
        expectedBuildHash: VAULT_SANDBOX_EXPECTED_BUILD_HASH,
        appearance: getVaultSandboxAppearance(),
        policy: getVaultSandboxPolicy(),
      },
      { timeoutMs: DEFAULT_TIMEOUT_MS },
    );
    if (!isObject(result) || result.accepted !== true || result.sandboxOrigin !== VAULT_SANDBOX_ORIGIN
      || result.channel !== CHANNEL || result.version !== VERSION || !isObject(result.build)
      || result.build.buildHash !== VAULT_SANDBOX_EXPECTED_BUILD_HASH || result.build.sandboxVersion !== VAULT_SANDBOX_VERSION) {
      throw new VaultSandboxError('sandbox_build_mismatch', 'Sandbox handshake did not accept the pinned release');
    }
    this.assertLive();
    this.handshaken = true;
    this.setAppearance(getVaultSandboxAppearance());
  }

  setAppearance(appearance: VaultSandboxAppearance): void {
    if (!this.handshaken) return;
    try {
      this.target.postMessage(
        {
          channel: CHANNEL,
          version: VERSION,
          id: randomId(),
          op: 'set-appearance' satisfies ParentOnlySandboxOp,
          payload: appearance,
        },
        VAULT_SANDBOX_ORIGIN,
      );
    } catch {
      // The iframe may be mid-navigation or already removed; the next real
      // sandbox request will surface availability errors if the client is stale.
    }
  }

  private async request<T>(
    op: VaultSandboxOp,
    payload?: unknown,
    options: { timeoutMs?: number; interactive?: boolean; authorizationWindow?: string } = {},
  ): Promise<T> {
    await this.readyPromise;
    this.assertLive();
    if (options.authorizationWindow) {
      const authorization = authorizationWindows.get(options.authorizationWindow);
      if (!authorization) throw new VaultSandboxError('sandbox_reset', 'Sandbox authorization window is no longer available', true);
      await authorization.ready;
      authorization.assertOpen();
      this.assertLive();
      if (authorizationWindows.get(options.authorizationWindow) !== authorization) {
        throw new VaultSandboxError('sandbox_reset', 'Sandbox authorization was cancelled', true);
      }
    }
    const id = randomId();
    // Interactive ops expand the iframe before sending so the launcher card is visible before the
    // request reaches the sandbox. The final authorization is completed in a top-level window;
    // the optional surface evidence remains a compatibility field, never a security decision.
    // When the caller already opened that window, the sandbox needs no launcher card, so the
    // iframe stays collapsed until the sandbox asks for its slot with `ui.show`.
    let surface: VaultConfirmSurface | null = null;
    try {
      if (options.interactive) {
        this.interactiveRequests.add(id);
        if (!options.authorizationWindow) {
          this.setModalVisible(true);
          surface = await this.measureSurface();
        }
      }
    } catch (error) {
      this.interactiveRequests.delete(id);
      if (this.interactiveRequests.size === 0 && this.modalVisible) this.setModalVisible(false);
      throw error;
    }
    this.assertLive();
    const promise = new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(id);
        this.interactiveRequests.delete(id);
        if (this.pending.size === 0 && this.modalVisible) this.setModalVisible(false);
        reject(new VaultSandboxError('sandbox_request_timeout', `Sandbox ${op} request timed out`, true));
      }, options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
      this.pending.set(id, {
        resolve: (value) => resolve(value as T),
        reject,
        timer,
      });
    });
    try {
      this.target.postMessage(
        {
          channel: CHANNEL,
          version: VERSION,
          id,
          op,
          payload: payload ?? {},
          ...(surface ? { surface } : {}),
          ...(options.authorizationWindow ? { authorizationWindow: options.authorizationWindow } : {}),
        },
        VAULT_SANDBOX_ORIGIN,
      );
    } catch (error) {
      const pending = this.pending.get(id);
      if (pending) {
        window.clearTimeout(pending.timer);
        this.pending.delete(id);
        this.interactiveRequests.delete(id);
        if (this.pending.size === 0 && this.modalVisible) this.setModalVisible(false);
        pending.reject(error instanceof Error ? error : new Error('Sandbox message could not be sent'));
      }
    }
    return promise;
  }

  status(wrapMeta?: string | null): Promise<VaultSandboxStatusResult> {
    return this.request<VaultSandboxStatusResult>('status', wrapMeta ? { wrapMeta } : {});
  }

  async setup(payload: {
    vaultUserHandle: string;
    displayName: string;
    existingProtectedVault: boolean;
    authzCreationOptions?: unknown;
    rootMetadata?: unknown;
  }): Promise<VaultSandboxSetupResult> {
    // Symmetric with unlock(): refresh the daemon policy and pass it so the first-ever unlocked
    // session (the setup ceremony) reflects a window/Strict change made after handshake but before
    // setup. The sandbox applies the passed policy on setup (protocol v2 §6.5).
    await refreshVaultSandboxPolicy();
    return this.request<VaultSandboxSetupResult>(
      'setup',
      { ...payload, policy: getVaultSandboxPolicy() },
      { timeoutMs: INTERACTIVE_TIMEOUT_MS },
    );
  }

  async unlock(payload: { wrapMeta: string }): Promise<VaultSandboxUnlockResult> {
    // Pull the freshest daemon policy and pass it (protocol v2 §6.5): a settings change made in
    // another tab only broadcasts a lock, not the new policy, and the handshake fetch is
    // best-effort, so the cached mirror alone can carry stale settings. The sandbox applies the
    // passed policy on unlock. Note this covers *explicit* unlocks; the sandbox's internal
    // auto-unlock (a protected op run while locked) still uses the policy pinned at handshake, so a
    // policy tightening like enabling Strict forces a fresh handshake (resetVaultSandboxClient) to
    // re-pin — see the settings dialog. Best-effort: a failed refresh fails closed (strict).
    await refreshVaultSandboxPolicy();
    return this.request<VaultSandboxUnlockResult>(
      'unlock',
      { wrapMeta: payload.wrapMeta, policy: getVaultSandboxPolicy() },
      { timeoutMs: INTERACTIVE_TIMEOUT_MS },
    );
  }

  lock(): Promise<{ ok?: boolean; state?: VaultSandboxState }> {
    return this.request<{ ok?: boolean; state?: VaultSandboxState }>('lock', {}, { timeoutMs: DEFAULT_TIMEOUT_MS });
  }

  seal(
    payload:
      | { name: string; kind: 'static'; value: string; wrapMeta?: string | null }
      | { name: string; kind: 'keypair'; wrapMeta?: string | null },
  ): Promise<VaultSandboxSealResult> {
    // Static = parent-typed value handed in for sealing (the #842 concession). Keypair = generated
    // entirely inside the sandbox (generate-only, no input) returning ciphertext + public material.
    const wire =
      payload.kind === 'static'
        ? { name: payload.name, kind: 'static', inputMode: 'parent-value', value: payload.value, wrapMeta: payload.wrapMeta }
        : { name: payload.name, kind: 'keypair', wrapMeta: payload.wrapMeta };
    return this.request<VaultSandboxSealResult>('seal', wire, { timeoutMs: INTERACTIVE_TIMEOUT_MS });
  }

  reveal(
    payload: { material: ProtectedUnlockMaterialLike; context: VaultSignedOperationContext },
    authorizationWindow?: string,
  ): Promise<{ completed: boolean }> {
    return this.request<{ completed: boolean }>('reveal', payload, {
      timeoutMs: INTERACTIVE_TIMEOUT_MS,
      interactive: true,
      authorizationWindow,
    });
  }

  sign(payload: {
    material: ProtectedUnlockMaterialLike;
    scheme: SignatureScheme;
    signingContext: VaultSandboxSigningContext;
    context: VaultSignedOperationContext;
  }, authorizationWindow?: string): Promise<SignatureResult> {
    return this.request<SignatureResult>('sign', payload, { timeoutMs: INTERACTIVE_TIMEOUT_MS, interactive: true, authorizationWindow });
  }

  /**
   * Batch DEK release: one confirm card lists every member, then the sandbox emits one HPKE blind
   * box per item (order matches `items`). Replaces v1 `releaseDEK`'s per-secret ceremony.
   */
  approveRelease(payload: { items: ApproveReleaseItem[] }, authorizationWindow?: string): Promise<{ blindBoxes: BlindBox[] }> {
    return this.request<{ blindBoxes: BlindBox[] }>('approveRelease', payload, {
      timeoutMs: INTERACTIVE_TIMEOUT_MS,
      interactive: true,
      authorizationWindow,
    });
  }

  /**
   * Tear down this client: stop listening, collapse the modal, drop the iframe, and fail any
   * in-flight request. Used by resetVaultSandboxClient() to force a fresh handshake (e.g. after a
   * policy tightening) — the sandbox pins its enforced policy at handshake, so a new client is how
   * a Strict change reaches the internal auto-unlock path immediately.
   */
  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    clients.delete(this);
    this.cancelReady?.();
    window.removeEventListener('message', this.handleMessage);
    for (const [, pending] of this.pending) {
      window.clearTimeout(pending.timer);
      pending.reject(new VaultSandboxError('sandbox_reset', 'Sandbox client was reset', true));
    }
    this.pending.clear();
    this.interactiveRequests.clear();
    this.setModalVisible(false);
    this.iframe.remove();
  }
}

type ProtectedUnlockMaterialLike = {
  name: string;
  kind?: string | null;
  envelope: ProtectedRecordEnvelope;
};

export async function getVaultSandboxClient(): Promise<VaultSandboxClient> {
  assertVaultEntryAvailable();
  if (!singleton) {
    const pending: Promise<VaultSandboxClient> = VaultSandboxClient.create().catch((err) => {
      // Only clear the globals if they still refer to THIS attempt. A reset may have already
      // replaced `singleton` with a newer create and `activeClient` with a live client; clearing
      // them here unconditionally would orphan that client's iframe (leaving its session state
      // behind while a fresh client is built on the next call). create() already destroyed its own
      // client on the reset-throw path, so there's nothing else to clean up here.
      if (singleton === pending) {
        singleton = null;
        activeClient = null;
      }
      throw err;
    });
    singleton = pending;
  }
  return singleton;
}

/**
 * Discard the current sandbox client so the next {@link getVaultSandboxClient} builds a fresh one
 * and re-handshakes. The sandbox pins its enforced session policy at the first handshake, so this
 * is how a policy tightening (enabling Strict) takes effect immediately on *every* path — including
 * the sandbox's internal auto-unlock — rather than only on the next explicit unlock. The caller is
 * responsible for reflecting the resulting locked state in the parent (see the settings dialog).
 */
export function resetVaultSandboxClient(): void {
  // Advance the generation first so any in-flight create() discards itself on completion.
  clientGeneration += 1;
  generationController.abort();
  generationController = new AbortController();
  singleton = null;
  activeClient = null;
  for (const client of [...clients]) client.destroy();
  for (const handle of [...authorizationWindows.values()]) handle.close();
}

export function getActiveVaultSandboxClient(): VaultSandboxClient | null {
  return activeClient;
}
