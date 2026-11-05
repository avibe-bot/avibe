export type NativePlatform = 'ios' | 'android';
export type NativeCapability = 'app' | 'haptics' | 'voice';
export type NativeImpactStyle = 'light' | 'medium' | 'heavy' | 'soft' | 'rigid';
export type NativeNotificationType = 'success' | 'warning' | 'error';
export type NativeVoicePermission = 'granted' | 'denied' | 'prompt';

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
