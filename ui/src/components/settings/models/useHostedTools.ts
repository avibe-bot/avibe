import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';
import { modelsApi } from './modelsApi';
import { beginRegionRead, loadingRegion, readRegion, readyRegion, settleRegionRead, type RegionRead } from './regionRead';
import type { HostedToolName, HostedToolPut, HostedToolsStatus } from './types';

/**
 * The hosted-tools read, shared by the Tools tab and the Source chips.
 *
 * Read lazily, while either surface is open, like the other report tabs, and
 * again whenever `revision` changes: an edited endpoint or a replaced key
 * changes the Source's identity, which drops its evidence on the server.
 *
 * A write answers the whole status, so reads and writes are ordered causally,
 * not by launch order. A read that overlaps a write cannot know whether it saw
 * that write, so it never lands; the write's own answer does. A read asked for
 * while a write is in flight waits for the writes to settle. Among writes, the
 * last one started is the newest state the server committed (they commit in
 * arrival order under the Hub's mutation lock).
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
  const order = React.useRef({ reads: 0, writes: 0, landedWrite: 0, inFlight: 0, deferredRead: false });

  const refresh = React.useCallback(async () => {
    const clock = order.current;
    if (clock.inFlight > 0) {
      clock.deferredRead = true;
      return;
    }
    const readId = ++clock.reads;
    const writesAtStart = clock.writes;
    setRead(beginRegionRead);
    const incoming = await readRegion(() => modelsApi.getHostedTools());
    if (!alive.current || readId !== clock.reads || writesAtStart !== clock.writes) return;
    setRead((previous) => settleRegionRead(previous, incoming));
  }, []);

  React.useEffect(() => {
    if (active) void refresh();
  }, [active, revision, refresh]);

  const save = React.useCallback(async (tool: HostedToolName, body: HostedToolPut) => {
    const clock = order.current;
    const writeId = ++clock.writes;
    clock.inFlight += 1;
    setPending((previous) => new Set(previous).add(tool));
    try {
      const status = await modelsApi.putHostedTool(tool, body);
      if (alive.current && writeId === clock.writes) {
        clock.landedWrite = writeId;
        setRead(readyRegion(status));
      }
    } catch {
      // A refused write changed nothing, and a lost answer may have committed;
      // either way the tab converges on what the server now holds.
      if (alive.current) {
        showToast(t('settings.models.tools.saveFailed', { toolInline: t(`settings.models.tools.tool.${tool}.inline`) }) as string, 'error');
      }
    } finally {
      clock.inFlight -= 1;
      if (alive.current) {
        setPending((previous) => {
          const next = new Set(previous);
          next.delete(tool);
          return next;
        });
      }
    }
    // Once every write has settled, the screen holds the newest write's answer
    // unless that write failed; then, or when a read was held back, read again.
    if (clock.inFlight === 0 && (clock.landedWrite !== clock.writes || clock.deferredRead) && alive.current) {
      clock.deferredRead = false;
      void refresh();
    }
  }, [refresh, showToast, t]);

  return { read, pending, refresh, save };
}
