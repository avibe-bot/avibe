import type {
  SigningAddresses,
  VaultSignedOperationContext,
  VaultWebAuthnRegistrationPayload,
} from '@/context/ApiContext';
import type { BlindBox, ProtectedRecordEnvelope, SignatureResult, SignatureScheme } from './vaultCrypto';
import {
  VAULT_SANDBOX_EXPECTED_BUILD_HASH,
  VAULT_SANDBOX_INTEGRITY_ENFORCED,
  VAULT_SANDBOX_IFRAME_URL,
  VAULT_SANDBOX_IFRAME_RESOURCE_PATH,
  VAULT_SANDBOX_MANIFEST_PATH,
  VAULT_SANDBOX_ORIGIN,
  VAULT_SANDBOX_PINNED_MANIFEST,
  VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS,
} from './vaultSandboxManifest';
import { getVaultSandboxAppearance, type VaultSandboxAppearance } from './vaultSandboxAppearance';
import { getVaultSandboxPolicy, refreshVaultSandboxPolicy, type VaultSessionPolicy } from './vaultSandboxPolicy';

const CHANNEL = 'avibe.vault.crypto';
const VERSION = 2;
// Protocol v2 operation surface (vault-sandbox #9): `unseal`→`reveal`, `releaseDEK`→`approveRelease`.
const REQUIRED_OPS = ['status', 'setup', 'unlock', 'lock', 'seal', 'reveal', 'sign', 'approveRelease'] as const;
const DEFAULT_TIMEOUT_MS = 15_000;
// Ops that may raise an in-sandbox card (confirm / passkey / plaintext display) can sit open for
// as long as the user takes to act, so they get the long ceremony timeout regardless of tier.
const INTERACTIVE_TIMEOUT_MS = 5 * 60_000;

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

let integrityPromise: Promise<void> | null = null;
let singleton: Promise<VaultSandboxClient> | null = null;
let activeClient: VaultSandboxClient | null = null;

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

function base64(bytes: ArrayBuffer): string {
  const raw = String.fromCharCode(...new Uint8Array(bytes));
  return btoa(raw);
}

async function sha256Subresource(value: ArrayBuffer): Promise<string> {
  return `sha256-${base64(await crypto.subtle.digest('SHA-256', value))}`;
}

function failIntegrity(message: string): never {
  throw new VaultSandboxError('sandbox_integrity_failed', message);
}

function sameManifestShape(live: unknown): live is typeof VAULT_SANDBOX_PINNED_MANIFEST {
  if (!isObject(live) || live.algorithm !== VAULT_SANDBOX_PINNED_MANIFEST.algorithm || !isObject(live.resources)) {
    return false;
  }
  const liveResources = live.resources as Record<string, unknown>;
  const pinnedEntries = Object.entries(VAULT_SANDBOX_PINNED_MANIFEST.resources);
  if (Object.keys(liveResources).length !== pinnedEntries.length) return false;
  return pinnedEntries.every(([path, digest]) => liveResources[path] === digest);
}

export async function verifyVaultSandboxIntegrity(): Promise<void> {
  if (integrityPromise) return integrityPromise;
  integrityPromise = (async () => {
    if (typeof window === 'undefined' || typeof crypto === 'undefined' || !crypto.subtle) {
      failIntegrity('Sandbox integrity requires Web Crypto in a browser context');
    }
    const manifestUrl = `${VAULT_SANDBOX_ORIGIN}${VAULT_SANDBOX_MANIFEST_PATH}`;
    const manifestResponse = await fetch(manifestUrl, { cache: 'no-store', mode: 'cors', credentials: 'omit' });
    if (!manifestResponse.ok) {
      failIntegrity(`Unable to fetch sandbox manifest (${manifestResponse.status})`);
    }
    const liveManifest = await manifestResponse.json();
    if (!sameManifestShape(liveManifest)) {
      failIntegrity('Sandbox manifest does not match the pinned build manifest');
    }
    if (!VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS.includes(VAULT_SANDBOX_IFRAME_RESOURCE_PATH)) {
      failIntegrity('Sandbox iframe URL is not pinned in the build manifest');
    }

    await Promise.all(
      VAULT_SANDBOX_REQUIRED_RESOURCE_PATHS.map(async (path) => {
        const response = await fetch(`${VAULT_SANDBOX_ORIGIN}${path}`, {
          cache: 'no-store',
          mode: 'cors',
          credentials: 'omit',
        });
        if (!response.ok) {
          failIntegrity(`Unable to fetch sandbox resource ${path} (${response.status})`);
        }
        const actual = await sha256Subresource(await response.arrayBuffer());
        const expected = VAULT_SANDBOX_PINNED_MANIFEST.resources[path];
        if (actual !== expected) {
          failIntegrity(`Sandbox resource hash mismatch for ${path}`);
        }
      }),
    );
  })().catch((err) => {
    integrityPromise = null;
    throw err;
  });
  return integrityPromise;
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

export class VaultSandboxClient {
  private iframe: HTMLIFrameElement;
  private backdrop: HTMLDivElement | null = null;
  private pending = new Map<string, PendingRequest>();
  private readyPromise: Promise<ReadyMessage>;
  private handshaken = false;
  private modalVisible = false;

  private constructor(iframe: HTMLIFrameElement) {
    this.iframe = iframe;
    this.readyPromise = this.waitForReady();
    window.addEventListener('message', this.handleMessage);
  }

  static async create(): Promise<VaultSandboxClient> {
    // Gated during pre-launch iteration — see VAULT_SANDBOX_INTEGRITY_ENFORCED. When off, the
    // parent still origin-isolates the sandbox; it just doesn't fail-closed on a manifest mismatch.
    if (VAULT_SANDBOX_INTEGRITY_ENFORCED) await verifyVaultSandboxIntegrity();
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
    await client.handshake();
    activeClient = client;
    return client;
  }

  private get target(): Window {
    const target = this.iframe.contentWindow;
    if (!target) throw new VaultSandboxError('sandbox_unavailable', 'Sandbox iframe is unavailable', true);
    return target;
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
  }

  private waitForReady(): Promise<ReadyMessage> {
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => {
        window.removeEventListener('message', onMessage);
        reject(new VaultSandboxError('sandbox_ready_timeout', 'Sandbox did not become ready', true));
      }, DEFAULT_TIMEOUT_MS);
      const onMessage = (event: MessageEvent) => {
        if (event.origin !== VAULT_SANDBOX_ORIGIN || event.source !== this.iframe.contentWindow) return;
        const ready = parseReadyMessage(event.data);
        if (!ready) return;
        window.clearTimeout(timer);
        window.removeEventListener('message', onMessage);
        const operations = ready.capabilities?.operations ?? [];
        const missing = REQUIRED_OPS.filter((op) => !operations.includes(op));
        if (ready.build?.buildHash !== VAULT_SANDBOX_EXPECTED_BUILD_HASH) {
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
      this.handleEvent(eventMessage);
      return;
    }
    const reply = parseTerminalMessage(event.data);
    if (!reply) return;
    const pending = this.pending.get(reply.id);
    if (!pending) return;
    this.pending.delete(reply.id);
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
    await this.request(
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

  private async request<T>(op: VaultSandboxOp, payload?: unknown, options: { timeoutMs?: number } = {}): Promise<T> {
    await this.readyPromise;
    const id = randomId();
    const promise = new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(id);
        if (this.pending.size === 0 && this.modalVisible) this.setModalVisible(false);
        reject(new VaultSandboxError('sandbox_request_timeout', `Sandbox ${op} request timed out`, true));
      }, options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
      this.pending.set(id, {
        resolve: (value) => resolve(value as T),
        reject,
        timer,
      });
    });
    this.target.postMessage({ channel: CHANNEL, version: VERSION, id, op, payload: payload ?? {} }, VAULT_SANDBOX_ORIGIN);
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
    // Symmetric with unlock(): refresh the daemon policy and pass it, so the first-ever unlocked
    // session (the setup ceremony) reflects a window/Strict change made after this client
    // handshaked but before setup, rather than the handshake-time snapshot. See unlock() for the
    // sandbox-pin caveat.
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
    // best-effort, so the cached mirror alone can carry stale settings. Best-effort — a failed
    // refresh keeps the last-known policy. Caveat: the sandbox currently *pins* the policy at the
    // first handshake and reads that for unlock/setup, so a mid-session change fully lands on the
    // next handshake (page reload / new client); passing it here keeps the frontend correct and
    // forward-compatible for when the sandbox honors the per-op policy.
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

  reveal(payload: { material: ProtectedUnlockMaterialLike; context: VaultSignedOperationContext }): Promise<{ completed: boolean }> {
    return this.request<{ completed: boolean }>('reveal', payload, { timeoutMs: INTERACTIVE_TIMEOUT_MS });
  }

  sign(payload: {
    material: ProtectedUnlockMaterialLike;
    scheme: SignatureScheme;
    signingContext: VaultSandboxSigningContext;
    context: VaultSignedOperationContext;
  }): Promise<SignatureResult> {
    return this.request<SignatureResult>('sign', payload, { timeoutMs: INTERACTIVE_TIMEOUT_MS });
  }

  /**
   * Batch DEK release: one confirm card lists every member, then the sandbox emits one HPKE blind
   * box per item (order matches `items`). Replaces v1 `releaseDEK`'s per-secret ceremony.
   */
  approveRelease(payload: { items: ApproveReleaseItem[] }): Promise<{ blindBoxes: BlindBox[] }> {
    return this.request<{ blindBoxes: BlindBox[] }>('approveRelease', payload, { timeoutMs: INTERACTIVE_TIMEOUT_MS });
  }
}

type ProtectedUnlockMaterialLike = {
  name: string;
  kind?: string | null;
  envelope: ProtectedRecordEnvelope;
};

export async function getVaultSandboxClient(): Promise<VaultSandboxClient> {
  if (!singleton) {
    singleton = VaultSandboxClient.create().catch((err) => {
      singleton = null;
      activeClient = null;
      throw err;
    });
  }
  return singleton;
}

export function getActiveVaultSandboxClient(): VaultSandboxClient | null {
  return activeClient;
}
