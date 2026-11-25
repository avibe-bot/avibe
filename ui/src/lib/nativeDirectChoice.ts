// This device's own path choice for an instance, in the Avibe App or a Desktop window of another
// computer's instance (capability `directPreference`; docs/plans/ios-app.md). Only 「连接方式」 reads
// or writes it, so it lives beside that page's chunk and not in the entry with the rest of the
// native bridge (src/lib/native.ts).
import { nativeDirectShell } from './native';

/** The shell's bridge, where it lets this device choose its path. */
function bridge() {
  const native = nativeDirectShell();
  return native && (native.capabilities.directPreference ?? 0) >= 1 ? native : null;
}

export type NativeDirectMode = 'automatic' | 'tunnel' | 'direct';

/** This device's own path choice for this instance: automatic, the Cloudflare tunnel only, or
 *  direct and the relay only (`directPreference` 2). `reverted`: when the shell put a choice back
 *  to automatic by itself, and which (a shell before `from` only ever puts `tunnel` back). */
export type NativeDirectChoice = { mode: NativeDirectMode; reverted: { at: number; from: 'tunnel' | 'direct' } | null };

const DIRECT_CHOICE_EVENT = 'avibe:native:direct.preferenceChanged';

function parseDirectChoice(value: unknown): NativeDirectChoice | null {
  if (!value || typeof value !== 'object') return null;
  const { mode, reverted } = value as { mode?: unknown; reverted?: unknown };
  if (mode !== 'automatic' && mode !== 'tunnel' && mode !== 'direct') return null;
  const { at, from } = reverted && typeof reverted === 'object' ? reverted as { at?: unknown; from?: unknown } : {};
  return {
    mode,
    reverted: typeof at === 'number' && Number.isFinite(at) ? { at, from: from === 'direct' ? 'direct' : 'tunnel' } : null,
  };
}

/** The shell lets this device choose its path (only one with a direct transport does). */
export function nativeDirectChoiceAvailable(): boolean {
  return bridge() !== null;
}

/** The shell takes 「仅直连和中继」 (`directPreference` 2). */
export function nativeDirectOnlyAvailable(): boolean {
  return (bridge()?.capabilities.directPreference ?? 0) >= 2;
}

/** Which shell is choosing: it decides what the page says goes back to automatic. */
export function nativeDirectChoiceShell(): 'app' | 'desktop' | null {
  const native = bridge();
  return native ? (native.platform === 'desktop' ? 'desktop' : 'app') : null;
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
export async function setNativeDirectChoice(mode: NativeDirectMode): Promise<NativeDirectChoice | null> {
  const native = bridge();
  if (!native || (mode === 'direct' && !nativeDirectOnlyAvailable())) return null;
  try {
    return parseDirectChoice(await native.call<unknown>('direct.setPreference', { mode }));
  } catch {
    return null;
  }
}

/** Calls `handler` when the shell changes the choice by itself (back to automatic). */
export function onNativeDirectChoiceChanged(handler: (choice: NativeDirectChoice) => void): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const listener = (event: Event) => {
    const choice = parseDirectChoice((event as CustomEvent<unknown>).detail);
    if (choice) handler(choice);
  };
  window.addEventListener(DIRECT_CHOICE_EVENT, listener);
  return () => window.removeEventListener(DIRECT_CHOICE_EVENT, listener);
}
