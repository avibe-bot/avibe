import { nativeDeviceDescribe, type NativeDeviceDescription } from './native';

/** What an older shell, or one that can't say, is called in push settings. */
export const FALLBACK_NATIVE_DEVICE_LABEL = 'iPhone App';

const ALIAS_SOURCES = new Set(['health', 'schedule']);

const isText = (value: unknown, limit: number): value is string =>
  typeof value === 'string' && value.trim().length > 0 && value.length <= limit;

/** The shell's answer, keeping only the fields of the instance's contract. */
export function parseNativeDeviceDescription(value: unknown): NativeDeviceDescription | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  if (
    raw.kind !== 'iphone_app'
    || !isText(raw.display_name, 120)
    || !isText(raw.app_version, 64)
    || !isText(raw.system_version, 64)
    || !Array.isArray(raw.aliases)
  ) {
    return null;
  }
  const aliases = raw.aliases.flatMap((alias) => {
    if (!alias || typeof alias !== 'object') return [];
    const { source, id } = alias as Record<string, unknown>;
    return typeof source === 'string' && ALIAS_SOURCES.has(source) && isText(id, 128)
      ? [{ source: source as 'health' | 'schedule', id }]
      : [];
  });
  return {
    kind: 'iphone_app',
    display_name: raw.display_name,
    ...(isText(raw.installation_id, 128) ? { installation_id: raw.installation_id } : {}),
    aliases,
    app_version: raw.app_version,
    system_version: raw.system_version,
  };
}

let description: Promise<NativeDeviceDescription | null> | null = null;
let reported: Promise<void> | null = null;
let reportAttempt = 0;

/** This page's one `device.describe`; null outside the iOS shell or on any failure. */
export function nativeDeviceDescription(): Promise<NativeDeviceDescription | null> {
  const pending = description ?? nativeDeviceDescribe()
    .then(parseNativeDeviceDescription)
    .catch(() => null);
  description = pending;
  // Only an answer is kept: a refusal (the App still confirming the account)
  // is asked again next time.
  void pending.then((answer) => {
    if (!answer && description === pending) description = null;
  });
  return pending;
}

/** The name push settings show for this device: the iPhone's model when the shell says. */
export async function nativeDeviceLabel(): Promise<string> {
  return (await nativeDeviceDescription())?.display_name ?? FALLBACK_NATIVE_DEVICE_LABEL;
}

/**
 * Tells the instance who this device is, once per page load. An instance
 * without a device list answers 404; nothing here may block or surface.
 * `post` is the API client, passed in so push code reading the label does not
 * load it.
 */
export function reportNativeDevice(
  post: (input: string, init: RequestInit) => Promise<Response>,
): Promise<void> {
  if (reported) return reported;
  const attempt = ++reportAttempt;
  const pending: Promise<void> = (async () => {
    const body = await nativeDeviceDescription();
    if (!body) {
      // Nothing was sent: the next page activation tries again.
      if (reportAttempt === attempt) reported = null;
      return;
    }
    try {
      await post('/api/devices/self', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch {
      // Display only: a failed report changes nothing on this page.
    }
  })();
  reported = pending;
  return pending;
}

export function resetNativeDeviceReportForTests(): void {
  description = null;
  reported = null;
}
