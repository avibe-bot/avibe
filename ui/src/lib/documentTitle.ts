// Browser tab title: "Avibe - <name>", where <name> is the configured
// ``ui.instance_name`` or, when blank, the machine's system hostname (both
// served in the /api/config payload). Falls back to plain "Avibe" when neither
// is available.
const BASE_TITLE = 'Avibe';

export function computeAppTitle(config: unknown): string {
  const ui = (config as { ui?: Record<string, unknown> } | null | undefined)?.ui ?? {};
  const configured = typeof ui.instance_name === 'string' ? ui.instance_name.trim() : '';
  const hostname = typeof ui.system_hostname === 'string' ? ui.system_hostname.trim() : '';
  const name = configured || hostname;
  return name ? `${BASE_TITLE} - ${name}` : BASE_TITLE;
}

export function applyAppTitle(config: unknown): void {
  try {
    document.title = computeAppTitle(config);
  } catch {
    // document may be unavailable in non-DOM contexts (SSR/tests); ignore.
  }
}
