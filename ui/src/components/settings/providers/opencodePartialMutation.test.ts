import { describe, expect, it, vi } from 'vitest';

import { reconcileOpencodePartialMutation } from './opencodePartialMutation';

describe('reconcileOpencodePartialMutation', () => {
  it('reconciles a partial provider-auth save without clearing the editor', async () => {
    const warn = vi.fn();
    const notify = vi.fn();
    const reload = vi.fn(async () => undefined);

    const reconciled = await reconcileOpencodePartialMutation(
      { ok: false, partial: true, saved: true },
      { message: 'saved partially', warn, notify, reload },
    );

    expect(reconciled).toBe(true);
    expect(warn).toHaveBeenCalledWith('saved partially');
    expect(notify).toHaveBeenCalledOnce();
    expect(reload).toHaveBeenCalledOnce();
  });

  it('reconciles a partial delete and clears its expanded row', async () => {
    const warn = vi.fn();
    const notify = vi.fn();
    const reload = vi.fn(async () => undefined);
    const clearExpanded = vi.fn();

    const reconciled = await reconcileOpencodePartialMutation(
      { ok: false, partial: true, removed: null },
      { message: 'delete outcome uncertain', warn, notify, reload, clearExpanded },
    );

    expect(reconciled).toBe(true);
    expect(warn).toHaveBeenCalledWith('delete outcome uncertain');
    expect(notify).toHaveBeenCalledOnce();
    expect(reload).toHaveBeenCalledOnce();
    expect(clearExpanded).toHaveBeenCalledOnce();
  });

  it('leaves an ordinary failure untouched', async () => {
    const reload = vi.fn(async () => undefined);

    const reconciled = await reconcileOpencodePartialMutation(
      { ok: false },
      { message: 'failed', warn: vi.fn(), notify: vi.fn(), reload },
    );

    expect(reconciled).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });
});
