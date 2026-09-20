// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import { afterEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import { SourceMutationReport } from './SourceMutationReport';
import type { SourceMutationCommit, SourceMutationLanding } from './mutationSettlement';
import { useSourceMutationReport } from './useSourceMutationReport';

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
};

const degradedLanding = (): SourceMutationLanding => ({
  verdict: 'degraded',
  reads: null,
  affectedChains: [],
});

afterEach(cleanup);

describe('page-owned Source mutation reports', () => {
  it('queues overlapping reports without destroying either mutation holder', async () => {
    const editLanding = deferred<SourceMutationLanding>();
    const deleteLanding = deferred<SourceMutationLanding>();
    const commits: SourceMutationCommit[] = [
      { action: 'edit', impact: null, settle: () => editLanding.promise },
      { action: 'delete', impact: null, settle: () => deleteLanding.promise },
    ];
    const editReleased = vi.fn();
    const deleteReleased = vi.fn();
    const Owner = () => {
      const owner = useSourceMutationReport();
      return (
        <>
          <button
            type="button"
            onClick={() => { void owner.present(commits[0]).then(editReleased); }}
          >
            Present edit report
          </button>
          <button
            type="button"
            onClick={() => { void owner.present(commits[1]).then(deleteReleased); }}
          >
            Present delete report
          </button>
          <SourceMutationReport
            report={owner.report}
            onComplete={() => { void owner.complete(); }}
            onDismiss={owner.dismiss}
          />
        </>
      );
    };
    render(<I18nextProvider i18n={i18n}><Owner /></I18nextProvider>);
    fireEvent.click(screen.getByRole('button', { name: 'Present edit report' }));
    fireEvent.click(screen.getByRole('button', { name: 'Present delete report' }));

    await act(async () => {
      editLanding.resolve(degradedLanding());
      await editLanding.promise;
    });
    expect(screen.getByRole('dialog', { name: /^The source was updated$/i })).toBeTruthy();

    await act(async () => {
      deleteLanding.resolve(degradedLanding());
      await deleteLanding.promise;
    });
    expect(screen.getByRole('dialog', { name: /^The source was updated$/i })).toBeTruthy();
    expect(screen.queryByRole('dialog', { name: /^The source was removed$/i })).toBeNull();
    expect(editReleased).not.toHaveBeenCalled();
    expect(deleteReleased).not.toHaveBeenCalled();

    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss unverified result' })[0]);
    await waitFor(() => expect(editReleased).toHaveBeenCalledOnce());
    expect(deleteReleased).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: /^The source was removed$/i })).toBeTruthy();

    fireEvent.click(screen.getAllByRole('button', { name: 'Dismiss unverified result' })[0]);
    await waitFor(() => expect(deleteReleased).toHaveBeenCalledOnce());
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});
