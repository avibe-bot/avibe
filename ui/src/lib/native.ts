export type NativePlatform = 'ios' | 'android';
export type NativeCapability =
  | 'accountPresentation'
  | 'app'
  | 'haptics'
  | 'keyboardResizesViewport'
  | 'push'
  | 'viewport'
  | 'voice';
export type NativeImpactStyle = 'light' | 'medium' | 'heavy' | 'soft' | 'rigid';
export type NativeNotificationType = 'success' | 'warning' | 'error';
export type NativeVoicePermission = 'granted' | 'denied' | 'prompt';
export type NativePushSubscription = {
  endpoint: string;
  keys: { p256dh: string; auth: string };
};
export type NativePushRevokeState = 'active' | 'absent' | 'pending';
export type NativePushStatus =
  | {
      state: 'unavailable' | 'off';
      revokeState?: NativePushRevokeState;
      endpoint?: string;
    }
  | ({
      state: 'on';
      revokeState?: NativePushRevokeState;
    } & NativePushSubscription);

export type NativeBridgeError = {
  code: string;
  message: string;
};

export type NativeAppInfo = {
  contract: 1;
  platform: NativePlatform;
  shellVersion: string;
  capabilities: Record<string, number>;
};

interface AvibeNative {
  readonly contract: 1;
  readonly platform: NativePlatform;
  readonly shellVersion: string;
  readonly capabilities: Readonly<Record<string, number>>;
  call<T>(method: string, params?: Record<string, unknown>): Promise<T>;
  on(event: string, handler: (payload: unknown) => void): () => void;
}

declare global {
  interface Window {
    /** Defined at document start by an Avibe native shell for a trusted main frame. */
    readonly avibeNative?: AvibeNative;
  }
}

function bridge(): AvibeNative | null {
  if (typeof window === 'undefined') return null;
  const candidate = window.avibeNative;
  if (
    candidate?.contract !== 1
    || (candidate.platform !== 'ios' && candidate.platform !== 'android')
    || typeof candidate.call !== 'function'
    || typeof candidate.on !== 'function'
    || !candidate.capabilities
  ) {
    return null;
  }
  return candidate;
}

export function isNativeShell(): boolean {
  return bridge() !== null;
}

export function hasNativeCapability(
  capability: string,
  minimumVersion = 1,
): boolean {
  const native = bridge();
  return Boolean(native && (native.capabilities[capability] ?? 0) >= minimumVersion);
}

async function callNative<T>(
  capability: NativeCapability,
  method: string,
  params: Record<string, unknown> = {},
): Promise<T | null> {
  const native = bridge();
  if (!native || (native.capabilities[capability] ?? 0) < 1) return null;
  return native.call<T>(method, params);
}

export async function nativeAppInfo(): Promise<NativeAppInfo | null> {
  return callNative<NativeAppInfo>('app', 'app.info');
}

export async function openNativeExternal(url: string): Promise<boolean> {
  try {
    const result = await callNative<{ opened: boolean }>(
      'app',
      'app.openExternal',
      { url },
    );
    return result?.opened === true;
  } catch {
    return false;
  }
}

export async function nativeShowAccount(): Promise<boolean> {
  try {
    if (!hasNativeCapability('accountPresentation')) return false;
    await callNative<null>('accountPresentation', 'app.showAccount');
    return true;
  } catch {
    return false;
  }
}

export async function setNativeAccountActionAvailable(
  available: boolean,
): Promise<boolean> {
  try {
    if (!hasNativeCapability('accountPresentation', 2)) return false;
    await callNative<null>(
      'accountPresentation',
      'app.setAccountActionAvailable',
      { available },
    );
    return true;
  } catch {
    return false;
  }
}

export async function nativeHapticImpact(style: NativeImpactStyle): Promise<boolean> {
  try {
    if (!hasNativeCapability('haptics')) return false;
    await callNative<null>('haptics', 'haptics.impact', { style });
    return true;
  } catch {
    return false;
  }
}

export async function nativeHapticNotify(type: NativeNotificationType): Promise<boolean> {
  try {
    if (!hasNativeCapability('haptics')) return false;
    await callNative<null>('haptics', 'haptics.notify', { type });
    return true;
  } catch {
    return false;
  }
}

export async function nativeHapticSelection(): Promise<boolean> {
  try {
    if (!hasNativeCapability('haptics')) return false;
    await callNative<null>('haptics', 'haptics.selection');
    return true;
  } catch {
    return false;
  }
}

export async function setNativeOuterScrollLocked(locked: boolean): Promise<boolean> {
  try {
    if (!hasNativeCapability('viewport')) return false;
    await callNative<null>(
      'viewport',
      'viewport.setOuterScrollLocked',
      { locked },
    );
    return true;
  } catch {
    return false;
  }
}

export async function nativeVoicePermission(): Promise<NativeVoicePermission | null> {
  try {
    const result = await callNative<{ state: NativeVoicePermission }>(
      'voice',
      'voice.permission',
    );
    return result?.state ?? null;
  } catch {
    return null;
  }
}

export async function nativePushStatus(
  vapidPublicKey?: string,
): Promise<NativePushStatus | null> {
  const outcome = await nativePushStatusOutcome(vapidPublicKey);
  return outcome.status === 'ok' ? outcome.value : null;
}

export type NativePushStatusOutcome =
  | { status: 'ok'; value: NativePushStatus }
  | { status: 'failed' };

export async function nativePushStatusOutcome(
  vapidPublicKey?: string,
): Promise<NativePushStatusOutcome> {
  try {
    const value = await callNative<NativePushStatus>(
      'push',
      'push.status',
      vapidPublicKey ? { vapidPublicKey } : {},
    );
    return value ? { status: 'ok', value } : { status: 'failed' };
  } catch {
    return { status: 'failed' };
  }
}

export async function nativePushSubscribe(
  vapidPublicKey: string,
  forceResubscribe = false,
): Promise<NativePushSubscription | null> {
  try {
    return await callNative<NativePushSubscription>(
      'push',
      'push.subscribe',
      forceResubscribe
        ? { vapidPublicKey, forceResubscribe: true }
        : { vapidPublicKey },
    );
  } catch {
    return null;
  }
}

export async function nativePushUnsubscribe(): Promise<boolean> {
  const status = (await nativePushUnsubscribeOutcome()).status;
  return status === 'confirmed' || status === 'not_needed';
}

export type NativePushUnsubscribeOutcome =
  | { status: 'confirmed' }
  | { status: 'pending' }
  | { status: 'not_needed' }
  | { status: 'failed' };

/**
 * New shells include `revoke`; `revoked` keeps older Web bundles compatible.
 */
export async function nativePushUnsubscribeOutcome(): Promise<NativePushUnsubscribeOutcome> {
  try {
    const result = await callNative<{
      revoked: boolean;
      revoke?: 'confirmed' | 'pending' | 'not_needed';
    }>(
      'push',
      'push.unsubscribe',
    );
    if (!result) return { status: 'failed' };
    if (
      result.revoke === 'confirmed'
      || result.revoke === 'pending'
      || result.revoke === 'not_needed'
    ) {
      return { status: result.revoke };
    }
    return result.revoked ? { status: 'confirmed' } : { status: 'pending' };
  } catch {
    return { status: 'failed' };
  }
}

export function onNativePushOpened(
  handler: (path: string) => void,
): () => void {
  const native = bridge();
  if (!native || (native.capabilities.push ?? 0) < 1) return () => undefined;
  return native.on('push.opened', (payload) => {
    if (
      payload
      && typeof payload === 'object'
      && 'url' in payload
      && typeof payload.url === 'string'
    ) {
      try {
        const parsed = new URL(payload.url, window.location.origin);
        if (parsed.origin !== window.location.origin) return;
        handler(parsed.pathname + parsed.search + parsed.hash);
      } catch {
        // Ignore malformed navigation payloads from the native shell.
      }
    }
  });
}
