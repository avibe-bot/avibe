import { nativeDeviceDescribe, type NativeDeviceDescription } from './native';

/** What an older shell, or one that can't say, is called in push settings. */
const FALLBACK_NATIVE_DEVICE_LABEL = 'iPhone App';

const ALIAS_SOURCES = new Set(['health', 'schedule']);

const isText = (value: unknown, limit: number): value is string =>
  typeof value === 'string' && value.trim().length > 0 && value.length <= limit;

/** The shell's answer, keeping only the fields of the instance's contract. */
function parseNativeDeviceDescription(value: unknown): NativeDeviceDescription | null {
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

/**
 * Per page: answers are kept for the shell's bridge object, which a new
 * document gets afresh. Only an answer is kept; a refusal (the App still
 * settling the account) is asked again next time.
 */
type PageState = {
  description?: Promise<NativeDeviceDescription | null>;
  reported?: Promise<void>;
};
const pages = new WeakMap<object, PageState>();
const NO_SHELL: PageState = {};

function pageState(): PageState {
  const native = typeof window === 'undefined' ? undefined : window.avibeNative;
  if (!native) return NO_SHELL;
  let state = pages.get(native);
  if (!state) {
    state = {};
    pages.set(native, state);
  }
  return state;
}

/** This page's one `device.describe`; null outside the iOS shell or on any failure. */
function nativeDeviceDescription(): Promise<NativeDeviceDescription | null> {
  const state = pageState();
  if (state.description) return state.description;
  const pending = nativeDeviceDescribe()
    .then(parseNativeDeviceDescription)
    .catch(() => null);
  if (state === NO_SHELL) return pending;
  state.description = pending;
  void pending.then((answer) => {
    // An answer without the installation id (push not set up yet) is
    // asked again too.
    if (!answer?.installation_id && state.description === pending) delete state.description;
  });
  return pending;
}

/** The name push settings show for this device: the iPhone's model when the shell says. */
export async function nativeDeviceLabel(): Promise<string> {
  return (await nativeDeviceDescription())?.display_name ?? FALLBACK_NATIVE_DEVICE_LABEL;
}

const BINDING_TICKET = /^[A-Za-z0-9_-]{43}$/;

/**
 * The report's own `device.describe`, asking the App for a one-use Health
 * binding ticket. The ticket stays with this one report: it is never cached
 * and never sent anywhere else. An App that can't attach one answers without
 * it; an older App that refuses the question is described as before.
 */
async function describeForReport(state: PageState): Promise<{
  body: NativeDeviceDescription | null;
  ticket?: string;
}> {
  const answer = await nativeDeviceDescribe({ health_binding: true }).catch(() => null);
  const body = parseNativeDeviceDescription(answer);
  if (!body) return { body: await nativeDeviceDescription() };
  if (body.installation_id && state !== NO_SHELL && !state.description) {
    state.description = Promise.resolve(body);
  }
  const ticket = (answer as Record<string, unknown>).health_binding_ticket;
  return typeof ticket === 'string' && BINDING_TICKET.test(ticket) ? { body, ticket } : { body };
}

/** Whether the instance refused the report because of its ticket (or, being older, the ticket field). */
async function refusedTicket(response: Response): Promise<boolean> {
  if (response.status === 400) return true;
  if (response.status !== 403) return false;
  const answer = await response.clone().json().catch(() => null) as { error?: unknown } | null;
  return answer?.error === 'health_binding_not_accepted';
}

/**
 * Tells the instance who this device is, once per page load. An instance
 * without a device list answers 404; nothing here may block or surface.
 * `post` is the API client, passed in so push code reading the label does not
 * load it. With a Health binding ticket, a refusal of the ticket sends the
 * same description once more without it; a refused ticket is never sent again.
 */
export function reportNativeDevice(
  post: (input: string, init: RequestInit) => Promise<Response>,
): Promise<void> {
  const state = pageState();
  if (state.reported) return state.reported;
  const pending: Promise<void> = (async () => {
    const { body, ticket } = await describeForReport(state);
    // The instance groups a device by its installation id and takes no
    // report without one; the App has it once push has set up after sign-in.
    if (!body?.installation_id) return false;
    const send = (payload: object) => post('/api/devices/self', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    try {
      const response = await send(ticket ? { ...body, health_binding_ticket: ticket } : body);
      if (ticket && await refusedTicket(response)) await send(body);
    } catch {
      // Display only: a failed report changes nothing on this page.
    }
    return true;
  })().then((sent) => {
    // Nothing was sent: the next page activation tries again.
    if (!sent && state.reported === pending) delete state.reported;
  });
  if (state !== NO_SHELL) state.reported = pending;
  return pending;
}
