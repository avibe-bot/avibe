/** @vitest-environment jsdom */
// Source names for a hint must follow Settings: a Source renamed there while the
// naming surface stays mounted is read again the next time the hint is needed.
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { modelsApi } from '../components/settings/models/modelsApi';
import type { Source } from '../components/settings/models/types';
import { useSourceNames } from './useSourceNames';

const named = (display_name: string) => [{ id: 'src_relaygpt1', display_name } as Source];

afterEach(() => vi.restoreAllMocks());

describe('useSourceNames', () => {
  it('reads the names again each time it turns on, so a rename in Settings shows', async () => {
    const list = vi.spyOn(modelsApi, 'listSources')
      .mockResolvedValueOnce(named('relay-gpt'))
      .mockResolvedValueOnce(named('relay-gpt-eu'));
    const { result, rerender } = renderHook(({ active }) => useSourceNames(active), { initialProps: { active: true } });
    await waitFor(() => expect(result.current).toEqual({ src_relaygpt1: 'relay-gpt' }));

    act(() => rerender({ active: false }));
    act(() => rerender({ active: true }));
    await waitFor(() => expect(result.current).toEqual({ src_relaygpt1: 'relay-gpt-eu' }));
    expect(list).toHaveBeenCalledTimes(2);
  });

  it('drops names an earlier read gave when a later one is refused', async () => {
    // A Member read, then the role is lowered to Editor: the hint goes generic.
    vi.spyOn(modelsApi, 'listSources')
      .mockResolvedValueOnce(named('relay-gpt'))
      .mockRejectedValueOnce(new Error('forbidden'));
    const { result, rerender } = renderHook(({ active }) => useSourceNames(active), { initialProps: { active: true } });
    await waitFor(() => expect(result.current).toEqual({ src_relaygpt1: 'relay-gpt' }));

    act(() => rerender({ active: false }));
    act(() => rerender({ active: true }));
    await waitFor(() => expect(result.current).toBeNull());
  });
});
