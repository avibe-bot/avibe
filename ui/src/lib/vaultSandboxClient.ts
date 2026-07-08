import type {
  SigningAddresses,
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

const CHANNEL = 'avibe.vault.crypto';
const VERSION = 1;
const REQUIRED_OPS = [
  'status',
  'setup',
  'unlock',
  'lock',
  'seal',
  'unseal',
  'sign',
  'releaseDEK',
] as const;
const DEFAULT_TIMEOUT_MS = 15_000;
const INTERACTIVE_TIMEOUT_MS = 5 * 60_000;

type VaultSandboxOp = (typeof REQUIRED_OPS)[number] | 'handshake';
type PendingRequest = {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
  timer: number;
  interactive: boolean;
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

export type VaultSandboxState = 'needs-setup' | 'locked' | 'unlocked';

export type VaultSandboxStatusResult = {
  state: VaultSandboxState;
  expiresAt?: number | null;
  freshSetup?: boolean;
};

export type VaultSandboxSetupResult = {
  wrapMeta: string;
  rpId: string;
  credentialId: string;
  authzRegistration?: VaultWebAuthnRegistrationPayload;
  state: 'unlocked';
  expiresAt?: number;
};

export type VaultSandboxUnlockResult = {
  state: 'unlocked';
  expiresAt?: number;
  wrapMeta?: string;
};

export type VaultSandboxSealResult = {
  envelope: ProtectedRecordEnvelope;
  establishingVmk: boolean;
  publicKey?: string;
  addresses?: SigningAddresses;
};

export type DaemonAgentBinding = {
  challengeId: string;
  requestId: string;
  grantId: string;
  agent: {
    publicKey: { public_key: string; fingerprint?: string };
    fingerprint: string;
  };
  context: Record<string, unknown>;
  expiresAt: string;
  signature: { alg: 'ed25519'; keyId: string; value: string };
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

export class VaultSandboxClient {
  private iframe: HTMLIFrameElement;
  private pending = new Map<string, PendingRequest>();
  private readyPromise: Promise<ReadyMessage>;
  private interactiveDepth = 0;

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
    iframe.style.inset = '0';
    iframe.style.border = '0';
    iframe.style.background = 'transparent';
    iframe.style.zIndex = '2147483647';
    iframe.style.colorScheme = 'normal';
    // At rest the sandbox is a headless RPC worker: keep it in the DOM (so
    // postMessage works) but 0-sized + hidden so it never covers the app. It
    // only expands to a full-screen overlay while an interactive ceremony is
    // active — see setInteractive().
    iframe.style.width = '0';
    iframe.style.height = '0';
    iframe.style.visibility = 'hidden';
    iframe.style.pointerEvents = 'none';

    const client = new VaultSandboxClient(iframe);
    iframe.src = VAULT_SANDBOX_IFRAME_URL;
    document.body.appendChild(iframe);
    await client.handshake();
    return client;
  }

  private get target(): Window {
    const target = this.iframe.contentWindow;
    if (!target) throw new VaultSandboxError('sandbox_unavailable', 'Sandbox iframe is unavailable', true);
    return target;
  }

  private setInteractive(active: boolean): void {
    this.interactiveDepth += active ? 1 : -1;
    this.interactiveDepth = Math.max(0, this.interactiveDepth);
    const interactive = this.interactiveDepth > 0;
    // Expand to a full-screen overlay only while a ceremony is active; otherwise
    // collapse to a hidden 0-size worker so the sandbox never covers the app.
    this.iframe.style.width = interactive ? '100vw' : '0';
    this.iframe.style.height = interactive ? '100vh' : '0';
    this.iframe.style.visibility = interactive ? 'visible' : 'hidden';
    this.iframe.style.pointerEvents = interactive ? 'auto' : 'none';
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
    const reply = parseTerminalMessage(event.data);
    if (!reply) return;
    const pending = this.pending.get(reply.id);
    if (!pending) return;
    this.pending.delete(reply.id);
    window.clearTimeout(pending.timer);
    if (pending.interactive) this.setInteractive(false);
    if (reply.ok) {
      pending.resolve(reply.result);
    } else {
      pending.reject(serializeSandboxError(reply.error));
    }
  };

  private async handshake(): Promise<void> {
    await this.readyPromise;
    await this.request(
      'handshake',
      {
        parentOrigin: window.location.origin,
        nonce: randomId(),
        expectedBuildHash: VAULT_SANDBOX_EXPECTED_BUILD_HASH,
      },
      { timeoutMs: DEFAULT_TIMEOUT_MS },
    );
  }

  private async request<T>(
    op: VaultSandboxOp,
    payload?: unknown,
    options: { timeoutMs?: number; interactive?: boolean } = {},
  ): Promise<T> {
    await this.readyPromise;
    const id = randomId();
    const interactive = Boolean(options.interactive);
    if (interactive) this.setInteractive(true);
    const promise = new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(id);
        if (interactive) this.setInteractive(false);
        reject(new VaultSandboxError('sandbox_request_timeout', `Sandbox ${op} request timed out`, true));
      }, options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
      this.pending.set(id, {
        resolve: (value) => resolve(value as T),
        reject,
        timer,
        interactive,
      });
    });
    this.target.postMessage({ channel: CHANNEL, version: VERSION, id, op, payload: payload ?? {} }, VAULT_SANDBOX_ORIGIN);
    return promise;
  }

  status(wrapMeta?: string | null): Promise<VaultSandboxStatusResult> {
    return this.request<VaultSandboxStatusResult>('status', wrapMeta ? { wrapMeta } : {});
  }

  setup(payload: {
    vaultUserHandle: string;
    displayName: string;
    existingProtectedVault: boolean;
    authzCreationOptions?: unknown;
    rootMetadata?: unknown;
  }): Promise<VaultSandboxSetupResult> {
    return this.request<VaultSandboxSetupResult>('setup', payload, {
      timeoutMs: INTERACTIVE_TIMEOUT_MS,
      interactive: true,
    });
  }

  unlock(payload: { wrapMeta: string }): Promise<VaultSandboxUnlockResult> {
    return this.request<VaultSandboxUnlockResult>('unlock', payload, {
      timeoutMs: INTERACTIVE_TIMEOUT_MS,
      interactive: true,
    });
  }

  lock(): Promise<{ ok?: boolean }> {
    return this.request<{ ok?: boolean }>('lock', {}, { timeoutMs: DEFAULT_TIMEOUT_MS });
  }

  seal(payload: {
    name: string;
    kind: 'static' | 'keypair';
    inputMode: 'sandbox-entry';
    wrapMeta?: string | null;
  }): Promise<VaultSandboxSealResult> {
    return this.request<VaultSandboxSealResult>('seal', payload, {
      timeoutMs: INTERACTIVE_TIMEOUT_MS,
      interactive: true,
    });
  }

  unseal(payload: {
    material: ProtectedUnlockMaterialLike;
    mode: 'sandbox-display' | 'sandbox-copy';
  }): Promise<{ completed: boolean }> {
    return this.request<{ completed: boolean }>('unseal', payload, {
      timeoutMs: INTERACTIVE_TIMEOUT_MS,
      interactive: true,
    });
  }

  sign(payload: {
    material: ProtectedUnlockMaterialLike;
    scheme: SignatureScheme;
    signingContext: VaultSandboxSigningContext;
  }): Promise<SignatureResult> {
    return this.request<SignatureResult>('sign', payload, {
      timeoutMs: INTERACTIVE_TIMEOUT_MS,
      interactive: true,
    });
  }

  releaseDEK(payload: {
    material: ProtectedUnlockMaterialLike;
    agentBinding: DaemonAgentBinding;
  }): Promise<BlindBox> {
    return this.request<BlindBox>('releaseDEK', payload, {
      timeoutMs: INTERACTIVE_TIMEOUT_MS,
      interactive: true,
    });
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
      throw err;
    });
  }
  return singleton;
}
