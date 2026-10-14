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
  const mount = () => renderHook(
    ({ wanted, active, viewer }) => useSourceNames(wanted, active, viewer),
    { initialProps: { wanted: true, active: true, viewer: 'member' } },
  );

  it('reads the names once per activation, and again after Settings covered the surface', async () => {
    const list = vi.spyOn(modelsApi, 'listSources')
      .mockResolvedValueOnce(named('relay-gpt'))
      .mockResolvedValueOnce(named('relay-gpt-eu'));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current).toEqual({ src_relaygpt1: 'relay-gpt' }));

    // Reopening the menu within one activation reads nothing: the Sources read is heavy.
    act(() => rerender({ wanted: false, active: true, viewer: 'member' }));
    act(() => rerender({ wanted: true, active: true, viewer: 'member' }));
    expect(list).toHaveBeenCalledTimes(1);

    // Settings, where a Source is renamed, covers the surface and uncovers it.
    act(() => rerender({ wanted: true, active: false, viewer: 'member' }));
    act(() => rerender({ wanted: true, active: true, viewer: 'member' }));
    await waitFor(() => expect(result.current).toEqual({ src_relaygpt1: 'relay-gpt-eu' }));
    expect(list).toHaveBeenCalledTimes(2);
  });

  it('drops names an earlier read gave when a later one is refused', async () => {
    // A Member read, then the role is lowered to Editor: the hint goes generic.
    vi.spyOn(modelsApi, 'listSources')
      .mockResolvedValueOnce(named('relay-gpt'))
      .mockRejectedValueOnce(new Error('forbidden'));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current).toEqual({ src_relaygpt1: 'relay-gpt' }));

    act(() => rerender({ wanted: true, active: false, viewer: 'member' }));
    act(() => rerender({ wanted: true, active: true, viewer: 'member' }));
    await waitFor(() => expect(result.current).toBeNull());
  });

  it('reads again when the viewer changes, so names a role may no longer read are dropped', async () => {
    // A Member reads the names; the role is lowered to Editor while the surface stays open.
    vi.spyOn(modelsApi, 'listSources')
      .mockResolvedValueOnce(named('relay-gpt'))
      .mockRejectedValueOnce(new Error('forbidden'));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current).toEqual({ src_relaygpt1: 'relay-gpt' }));

    act(() => rerender({ wanted: true, active: true, viewer: 'editor' }));
    await waitFor(() => expect(result.current).toBeNull());
  });

  it('hides names read for another viewer at once, before the new viewer\'s read answers', async () => {
    let answer!: (sources: Source[]) => void;
    vi.spyOn(modelsApi, 'listSources')
      .mockResolvedValueOnce(named('relay-gpt'))
      .mockImplementationOnce(() => new Promise<Source[]>((resolve) => { answer = resolve; }));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current).toEqual({ src_relaygpt1: 'relay-gpt' }));

    act(() => rerender({ wanted: true, active: true, viewer: 'editor' }));
    // The Editor's read is still out: nothing the Member read shows meanwhile.
    expect(result.current).toBeNull();
    await act(async () => { answer(named('relay-gpt')); });
    expect(result.current).toEqual({ src_relaygpt1: 'relay-gpt' });
  });
});
