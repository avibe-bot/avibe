// This device's own path choice for an instance, in the Avibe App (capability `directPreference`
// 1). Only 「连接方式」 reads or writes it, so it lives beside that page's chunk and not in the
// entry with the rest of the native bridge (src/lib/native.ts).
import { hasNativeCapability } from './native';

/** The bridge, where the App lets this device choose its path. `hasNativeCapability` has already
 *  checked that it is the App's own. */
const bridge = () => (nativeDirectChoiceAvailable() ? window.avibeNative ?? null : null);

/** This device's own path choice for this instance (`directPreference` 1): automatic, or the
 *  Cloudflare tunnel only. `revertedAt`: when the App put `tunnel` back to automatic by itself. */
export type NativeDirectChoice = { mode: 'automatic' | 'tunnel'; revertedAt: number | null };

const DIRECT_CHOICE_EVENT = 'avibe:native:direct.preferenceChanged';

function parseDirectChoice(value: unknown): NativeDirectChoice | null {
  if (!value || typeof value !== 'object') return null;
  const { mode, reverted } = value as { mode?: unknown; reverted?: unknown };
  if (mode !== 'automatic' && mode !== 'tunnel') return null;
  const at = reverted && typeof reverted === 'object' ? (reverted as { at?: unknown }).at : null;
  return { mode, revertedAt: typeof at === 'number' && Number.isFinite(at) ? at : null };
}

/** The App lets this device choose its path (only an App with a direct transport does). */
export function nativeDirectChoiceAvailable(): boolean {
  return hasNativeCapability('directPreference', 1);
}

/** Reads `direct.preference()`. Null without the capability, on failure, or on an answer this
 *  page does not know. */
export async function readNativeDirectChoice(): Promise<NativeDirectChoice | null> {
  const native = bridge();
  if (!native) return null;
  try {
    return parseDirectChoice(await native.call<unknown>('direct.preference'));
  } catch {
    return null;
  }
}

/** Writes the choice. The App answers first and, when the mode changed, reopens the instance. */
export async function setNativeDirectChoice(mode: NativeDirectChoice['mode']): Promise<NativeDirectChoice | null> {
  const native = bridge();
  if (!native) return null;
  try {
    return parseDirectChoice(await native.call<unknown>('direct.setPreference', { mode }));
  } catch {
    return null;
  }
}

/** Calls `handler` when the App changes the choice by itself (back to automatic). */
export function onNativeDirectChoiceChanged(handler: (choice: NativeDirectChoice) => void): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const listener = (event: Event) => {
    const choice = parseDirectChoice((event as CustomEvent<unknown>).detail);
    if (choice) handler(choice);
  };
  window.addEventListener(DIRECT_CHOICE_EVENT, listener);
  return () => window.removeEventListener(DIRECT_CHOICE_EVENT, listener);
}
