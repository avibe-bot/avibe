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
