import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';
import { classifyModelHubFailure } from './asyncLifetime';
import { apiFailure, modelsApi } from './modelsApi';
import { beginRegionRead, foldRegionRead, loadingRegion, readRegion, readyRegion, settleRegionRead, type RegionRead } from './regionRead';
import type { HostedToolClear, HostedToolName, HostedToolPut, HostedToolsStatus } from './types';

/**
 * The hosted-tools read, shared by Tools, Sources activation, and Source detail.
 *
 * Read while one of those surfaces is open, and again whenever `revision`
 * changes: `revision` is the open Source's identity
 * (as `retry.source_identity` defines it: endpoint, credential, verification),
 * a change of which drops its evidence on the server, so the old status is
 * dropped here too before the new read.
 *
 * Every write answers the whole status (or reads it after the retry POST), so
 * writes run one at a time: each starts after the previous one settled, and each answer is
 * then the newest state the server committed. A read never overlaps a write:
 * one asked for while writes are queued waits for them to drain, and one
 * already out when a write is queued is dropped, because it cannot tell
 * whether it saw that write.
 *
 * `pending` comes from one per-tool ledger and the report's freshness. A tool
 * is pending while a write for it is queued or out, or while a write for it
 * ended without an answer and no authoritative status has landed since: that
 * write may have committed, so no control may build a body on what is shown.
 * Only an authoritative status ends that wait: a write's answer or a read that
 * landed. Each is the whole status and newer than every write before it, so it
 * covers every tool. A read that failed or was dropped covers nothing. A report
 * that is itself out of date (being re-read, or kept after its read failed)
 * holds every tool it shows the same way, for the same reason.
 */
export function useHostedTools(active: boolean, revision: string | null = null, surface: string | null = null) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const alive = React.useRef(true);
  React.useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const [read, setRead] = React.useState<RegionRead<HostedToolsStatus>>(loadingRegion);
  const [pending, setPending] = React.useState<ReadonlySet<HostedToolName>>(() => new Set());
  const order = React.useRef({ reads: 0, writes: 0, rereadAfterWrites: false });
  const writeQueue = React.useRef<Promise<void>>(Promise.resolve());

  const ledger = React.useRef(new Map<HostedToolName, { queued: number; awaiting: boolean }>());
  const entry = (tool: HostedToolName) => {
    const found = ledger.current.get(tool);
    if (found) return found;
    const created = { queued: 0, awaiting: false };
    ledger.current.set(tool, created);
    return created;
  };
  const queuedWrites = () => [...ledger.current.values()].reduce((sum, item) => sum + item.queued, 0);
  const publish = React.useCallback(() => {
    if (!alive.current) return;
    setPending(new Set([...ledger.current].filter(([, item]) => item.queued > 0 || item.awaiting).map(([tool]) => tool)));
  }, []);
  /** An authoritative status landed: no lost write is unaccounted for any more. */
  const covered = React.useCallback(() => {
    for (const item of ledger.current.values()) item.awaiting = false;
    publish();
  }, [publish]);

  /** Read the status. `forget` drops what is shown first: it belonged to another Source identity. */
  const refresh = React.useCallback(async ({ forget = false }: { forget?: boolean } = {}) => {
    const clock = order.current;
    if (forget) setRead(loadingRegion);
    if (queuedWrites() > 0) {
      clock.rereadAfterWrites = true;
      return;
    }
    const readId = ++clock.reads;
    const writesAtStart = clock.writes;
    if (!forget) setRead(beginRegionRead);
    const incoming = await readRegion(() => modelsApi.getHostedTools());
    if (!alive.current || readId !== clock.reads || writesAtStart !== clock.writes) return;
    setRead((previous) => settleRegionRead(previous, incoming));
    if (incoming.kind === 'ready') covered();
  }, [covered]);

  // `revision` is the open Source's identity. Evidence follows it, so a new one
  // never shows the old one's records, even if its own read fails.
  const shownRevision = React.useRef(revision);
  React.useEffect(() => {
    if (!active) return;
    const forget = shownRevision.current !== revision;
    shownRevision.current = revision;
    void refresh({ forget });
  }, [active, revision, surface, refresh]);

  const write = React.useCallback(async (
    tool: HostedToolName,
    send: () => Promise<HostedToolsStatus>,
    failure: 'save' | 'clear',
  ) => {
    const clock = order.current;
    clock.writes += 1;
    // An answer is for the Source identity it was asked under (see `shownRevision`).
    const revisionAtStart = shownRevision.current;
    entry(tool).queued += 1;
    publish();
    const turn = writeQueue.current.then(async () => {
      try {
        const status = await send();
        if (!alive.current) return;
        if (shownRevision.current === revisionAtStart) {
          setRead(readyRegion(status));
          covered();
        } else {
          clock.rereadAfterWrites = true;
        }
      } catch (error) {
        // A refused write changed nothing, and a lost answer may have committed;
        // either way the tab converges on what the server now holds. Only a
        // decided refusal is a failure: a lost answer is an unknown outcome.
        clock.rereadAfterWrites = true;
        entry(tool).awaiting = true;
        if (alive.current) {
          const toolInline = t(`settings.models.tools.tool.${tool}.inline`);
          // `engine_down` is inconclusive too: the UI process also says it when the engine's answer never reached it.
          if (classifyModelHubFailure(apiFailure(error)) === 'inconclusive') showToast(t(`settings.models.tools.${failure}Unconfirmed`, { toolInline }) as string, 'warning');
          else showToast(t(`settings.models.tools.${failure}Failed`, { toolInline }) as string, 'error');
        }
      }
    });
    writeQueue.current = turn;
    await turn;
    entry(tool).queued -= 1;
    publish();
    if (!alive.current) return;
    if (queuedWrites() === 0 && clock.rereadAfterWrites) {
      clock.rereadAfterWrites = false;
      await refresh();
    }
  }, [covered, publish, refresh, showToast, t]);

  /** Change the fields `body` names; an absent field stays as saved. */
  const save = React.useCallback(
    (tool: HostedToolName, body: HostedToolPut) => write(tool, () => modelsApi.putHostedTool(tool, body), 'save'),
    [write],
  );
  /** Clear refusals: from the next request the tool is tried natively again there. */
  const clear = React.useCallback(
    (tool: HostedToolName, body: HostedToolClear) => write(tool, () => modelsApi.clearHostedTool(tool, body), 'clear'),
    [write],
  );

  const held = React.useMemo<ReadonlySet<HostedToolName>>(() => foldRegionRead(read, {
    loading: () => pending,
    ready: () => pending,
    unread: () => pending,
    degraded: (stale) => new Set([...pending, ...stale.tools.map((tool) => tool.name)]),
  }), [read, pending]);

  const retryService = React.useCallback(
    (sourceId: string) => write('web_search', async () => {
      await modelsApi.retrySearchService(sourceId);
      return modelsApi.getHostedTools();
    }, 'save'),
    [write],
  );

  const retryFetch = React.useCallback(
    (sourceId: string) => write('web_fetch', async () => {
      await modelsApi.retryFetchService(sourceId);
      return modelsApi.getHostedTools();
    }, 'save'),
    [write],
  );

  return { read, pending: held, refresh, save, clear, retryService, retryFetch };
}
