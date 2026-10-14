import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';
import { modelsApi } from './modelsApi';
import { beginRegionRead, loadingRegion, readRegion, readyRegion, settleRegionRead, type RegionRead } from './regionRead';
import type { HostedToolClear, HostedToolName, HostedToolPut, HostedToolsStatus } from './types';

/**
 * The hosted-tools read, shared by the Tools tab and the Source chips.
 *
 * Read lazily, while either surface is open, like the other report tabs, and
 * again whenever `revision` changes: `revision` is the open Source's identity
 * (as `retry.source_identity` defines it: endpoint, credential, verification),
 * a change of which drops its evidence on the server, so the old status is
 * dropped here too before the new read.
 *
 * Every write (a PUT or a clear) answers the whole status, so writes run one
 * at a time: each starts after the previous one settled, and each answer is
 * then the newest state the server committed. A read never overlaps a write: one asked for
 * while writes are queued waits for them to drain, and one already out when a
 * write is queued is dropped, because it cannot tell whether it saw that write.
 */
export function useHostedTools(active: boolean, revision: string | null = null) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const alive = React.useRef(true);
  React.useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const [read, setRead] = React.useState<RegionRead<HostedToolsStatus>>(loadingRegion);
  const [pending, setPending] = React.useState<ReadonlySet<HostedToolName>>(() => new Set());
  const order = React.useRef({ reads: 0, writes: 0, queued: 0, rereadAfterWrites: false });
  const writeQueue = React.useRef<Promise<void>>(Promise.resolve());

  /** Read the status. `forget` drops what is shown first: it belonged to another Source identity. */
  const refresh = React.useCallback(async ({ forget = false }: { forget?: boolean } = {}) => {
    const clock = order.current;
    if (forget) setRead(loadingRegion);
    if (clock.queued > 0) {
      clock.rereadAfterWrites = true;
      return;
    }
    const readId = ++clock.reads;
    const writesAtStart = clock.writes;
    if (!forget) setRead(beginRegionRead);
    const incoming = await readRegion(() => modelsApi.getHostedTools());
    if (!alive.current || readId !== clock.reads || writesAtStart !== clock.writes) return;
    setRead((previous) => settleRegionRead(previous, incoming));
  }, []);

  // `revision` is the open Source's identity. Evidence follows it, so a new one
  // never shows the old one's records, even if its own read fails.
  const shownRevision = React.useRef(revision);
  React.useEffect(() => {
    if (!active) return;
    const forget = shownRevision.current !== revision;
    shownRevision.current = revision;
    void refresh({ forget });
  }, [active, revision, refresh]);

  // Tools whose write ended without an answer: they stay pending until the read
  // that reconciles them lands, so no control writes a body built on the status
  // that write may already have changed.
  const unsettled = React.useRef<Set<HostedToolName>>(new Set());
  const release = React.useCallback((tools: Iterable<HostedToolName>) => {
    const done = [...tools];
    if (!alive.current || done.length === 0) return;
    setPending((previous) => {
      const next = new Set(previous);
      for (const tool of done) next.delete(tool);
      return next;
    });
  }, []);

  const write = React.useCallback(async (
    tool: HostedToolName,
    send: () => Promise<HostedToolsStatus>,
    failure: 'saveFailed' | 'clearFailed',
  ) => {
    const clock = order.current;
    clock.writes += 1;
    clock.queued += 1;
    // An answer is for the Source identity it was asked under (see `shownRevision`).
    const revisionAtStart = shownRevision.current;
    setPending((previous) => new Set(previous).add(tool));
    const turn = writeQueue.current.then(async () => {
      try {
        const status = await send();
        if (!alive.current) return;
        if (shownRevision.current === revisionAtStart) setRead(readyRegion(status));
        else clock.rereadAfterWrites = true;
      } catch {
        // A refused write changed nothing, and a lost answer may have committed;
        // either way the tab converges on what the server now holds.
        clock.rereadAfterWrites = true;
        unsettled.current.add(tool);
        if (alive.current) {
          showToast(t(`settings.models.tools.${failure}`, { toolInline: t(`settings.models.tools.tool.${tool}.inline`) }) as string, 'error');
        }
      }
    });
    writeQueue.current = turn;
    await turn;
    clock.queued -= 1;
    if (!alive.current) return;
    if (!unsettled.current.has(tool)) release([tool]);
    if (clock.queued === 0 && clock.rereadAfterWrites) {
      clock.rereadAfterWrites = false;
      const reconciling = [...unsettled.current];
      unsettled.current.clear();
      await refresh();
      release(reconciling);
    }
  }, [refresh, release, showToast, t]);

  /** Change the fields `body` names; an absent field stays as saved. */
  const save = React.useCallback(
    (tool: HostedToolName, body: HostedToolPut) => write(tool, () => modelsApi.putHostedTool(tool, body), 'saveFailed'),
    [write],
  );
  /** Clear refusals: from the next request the tool is tried natively again there. */
  const clear = React.useCallback(
    (tool: HostedToolName, body: HostedToolClear) => write(tool, () => modelsApi.clearHostedTool(tool, body), 'clearFailed'),
    [write],
  );

  return { read, pending, refresh, save, clear };
}
