import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';
import { modelsApi } from './modelsApi';
import { beginRegionRead, loadingRegion, readRegion, readyRegion, settleRegionRead, type RegionRead } from './regionRead';
import { probeKey, type ProbeTarget } from './hostedTools';
import type { HostedToolName, HostedToolProbeResult, HostedToolPut, HostedToolsStatus } from './types';

/**
 * The hosted-tools read, shared by the Tools tab and the Source chips.
 *
 * Read lazily, while either surface is open, like the other report tabs, and
 * again whenever `revision` changes: an edited endpoint or a replaced key
 * changes the Source's identity, which drops its evidence on the server.
 *
 * Every write answers the whole status, so writes run one at a time: each PUT
 * starts after the previous one settled, and each answer is then the newest
 * state the server committed. A read never overlaps a write: one asked for
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
  const [probing, setProbing] = React.useState<ReadonlySet<string>>(() => new Set());
  const order = React.useRef({ reads: 0, writes: 0, queued: 0, rereadAfterWrites: false });
  const writeQueue = React.useRef<Promise<void>>(Promise.resolve());

  const refresh = React.useCallback(async () => {
    const clock = order.current;
    if (clock.queued > 0) {
      clock.rereadAfterWrites = true;
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
    clock.writes += 1;
    clock.queued += 1;
    setPending((previous) => new Set(previous).add(tool));
    const write = writeQueue.current.then(async () => {
      try {
        const status = await modelsApi.putHostedTool(tool, body);
        if (alive.current) setRead(readyRegion(status));
      } catch {
        // A refused write changed nothing, and a lost answer may have committed;
        // either way the tab converges on what the server now holds.
        clock.rereadAfterWrites = true;
        if (alive.current) {
          showToast(t('settings.models.tools.saveFailed', { toolInline: t(`settings.models.tools.tool.${tool}.inline`) }) as string, 'error');
        }
      }
    });
    writeQueue.current = write;
    await write;
    clock.queued -= 1;
    if (!alive.current) return;
    setPending((previous) => {
      const next = new Set(previous);
      next.delete(tool);
      return next;
    });
    if (clock.queued === 0 && clock.rereadAfterWrites) {
      clock.rereadAfterWrites = false;
      void refresh();
    }
  }, [refresh, showToast, t]);

  /**
   * Run one call probe: a real, cost-bearing request the user asked for. Its
   * answer carries the status it left, but a probe runs long beside writes, so
   * the screen converges through an ordinary read rather than that snapshot.
   * Null when the request itself failed.
   */
  const probe = React.useCallback(async (target: ProbeTarget): Promise<HostedToolProbeResult | null> => {
    const key = probeKey(target);
    setProbing((previous) => new Set(previous).add(key));
    try {
      const { probe: result } = await modelsApi.probeHostedTool(target.tool, {
        source_id: target.candidate.source_id,
        model_id: target.candidate.model_id,
      });
      return result;
    } catch {
      return null;
    } finally {
      if (alive.current) {
        setProbing((previous) => {
          const next = new Set(previous);
          next.delete(key);
          return next;
        });
        void refresh();
      }
    }
  }, [refresh]);

  return { read, pending, probing, refresh, save, probe };
}
