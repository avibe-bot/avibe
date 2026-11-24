// 「测速」 in 连接诊断与网络路径: the shell measures, on demand, small HTTPS round trips on a ready
// connection over the path it uses now and over the Cloudflare tunnel (capability `directMeasure`;
// docs/plans/ios-app.md). Nothing is stored; the result lives in the page.
import { nativeDirectShell } from './native';

function bridge() {
  const native = nativeDirectShell();
  return native && (native.capabilities.directMeasure ?? 0) >= 1 ? native : null;
}

/** One path's samples. `kind` and `scope` name the route; `relayRegion` the relay's region. */
export type MeasureLeg = {
  kind: 'relay' | 'ipv4' | 'ipv6' | 'tunnel';
  scope: 'local' | 'global' | null;
  relayRegion: string | null;
  ok: number;
  medianMs: number | null;
  maxMs: number | null;
  error: 'failed' | 'timeout' | null;
};

export type DirectMeasurement = {
  measuredAt: number;
  samples: number;
  /** The direct or relay path in use now; null when there is none. */
  current: MeasureLeg | null;
  /** Null only when the shell could not try the tunnel at all. */
  tunnel: MeasureLeg | null;
};

const KINDS = ['relay', 'ipv4', 'ipv6', 'tunnel'] as const;
const ms = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null);

function leg(value: unknown): MeasureLeg | null {
  if (!value || typeof value !== 'object') return null;
  const raw = value as Record<string, unknown>;
  const kind = KINDS.find((each) => each === raw.kind);
  if (!kind) return null;
  const ok = typeof raw.ok === 'number' && Number.isInteger(raw.ok) && raw.ok >= 0 ? raw.ok : 0;
  return {
    kind,
    scope: (kind === 'ipv4' || kind === 'ipv6') && (raw.scope === 'local' || raw.scope === 'global') ? raw.scope : null,
    relayRegion: kind === 'relay' && typeof raw.relayRegion === 'string' && raw.relayRegion ? raw.relayRegion : null,
    ok,
    medianMs: ok > 0 ? ms(raw.medianMs) : null,
    maxMs: ok > 0 ? ms(raw.maxMs) : null,
    error: raw.error === 'failed' || raw.error === 'timeout' ? raw.error : null,
  };
}

/** The shell can measure (`directMeasure` 1). */
export function nativeDirectMeasureAvailable(): boolean {
  return bridge() !== null;
}

/** Measures. `busy`: one is already running. `failed`: no answer this page can read. */
export async function measureNativeDirect(): Promise<DirectMeasurement | 'busy' | 'failed'> {
  const native = bridge();
  if (!native) return 'failed';
  try {
    const answer = await native.call<unknown>('direct.measure');
    if (!answer || typeof answer !== 'object') return 'failed';
    const raw = answer as Record<string, unknown>;
    if (typeof raw.measuredAt !== 'number' || !Number.isFinite(raw.measuredAt)) return 'failed';
    return {
      measuredAt: raw.measuredAt,
      samples: typeof raw.samples === 'number' && raw.samples > 0 ? raw.samples : 5,
      current: leg(raw.current),
      tunnel: leg(raw.tunnel),
    };
  } catch (error) {
    return error && typeof error === 'object' && (error as { code?: unknown }).code === 'busy' ? 'busy' : 'failed';
  }
}
