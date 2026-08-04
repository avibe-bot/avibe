import type { TunnelQualitySnapshot } from '../context/ApiContext';

export const getTunnelQualityDisplayState = (
  quality: Pick<TunnelQualitySnapshot, 'state' | 'grade'> | undefined,
  fresh: boolean,
): TunnelQualitySnapshot['grade'] | 'degraded' | 'recovering' => {
  if (!fresh || !quality) return 'unknown';
  if (quality.state === 'recovering') return 'recovering';
  if (quality.state === 'degraded') return 'degraded';
  return quality.grade;
};

export const getTunnelRequestPathDisplayState = (
  requestPath: TunnelQualitySnapshot['request_path'],
): 'absent' | 'measuring' | 'latency' | 'unavailable' => {
  if (!requestPath) return 'absent';
  if (
    requestPath.confidence !== 'low'
    && (requestPath.status === 'unavailable' || requestPath.success_count === 0)
  ) {
    return 'unavailable';
  }
  if (requestPath.confidence !== 'low' && requestPath.latency_ms) return 'latency';
  return 'measuring';
};
