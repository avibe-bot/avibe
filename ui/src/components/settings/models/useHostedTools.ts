import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';
import { modelsApi } from './modelsApi';
import { beginRegionRead, loadingRegion, readRegion, readyRegion, settleRegionRead, type RegionRead } from './regionRead';
import { probeKey, sameExecutor, type ProbeTarget } from './hostedTools';
import type { HostedToolName, HostedToolProbeResult, HostedToolPut, HostedToolsStatus } from './types';

/**
 * The hosted-tools read, shared by the Tools tab and the Source chips.
 *
 * Read lazily, while either surface is open, like the other report tabs, and
 * again whenever `revision` changes: `revision` is the open Source's identity
 * (as `retry.source_identity` defines it: endpoint, credential, verification),
 * a change of which drops its evidence on the server, so the old status is
 * dropped here too before the new read.
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

  const save = React.useCallback(async (tool: HostedToolName, body: HostedToolPut) => {
    const clock = order.current;
    clock.writes += 1;
    clock.queued += 1;
    // An answer is for the Source identity it was asked under (see `shownRevision`).
    const revisionAtStart = shownRevision.current;
    setPending((previous) => new Set(previous).add(tool));
    const write = writeQueue.current.then(async () => {
      try {
        const status = await modelsApi.putHostedTool(tool, body);
        if (!alive.current) return;
        if (shownRevision.current === revisionAtStart) setRead(readyRegion(status));
        else clock.rereadAfterWrites = true;
      } catch {
        // A refused write changed nothing, and a lost answer may have committed;
        // either way the tab converges on what the server now holds.
        clock.rereadAfterWrites = true;
        unsettled.current.add(tool);
        if (alive.current) {
          showToast(t('settings.models.tools.saveFailed', { toolInline: t(`settings.models.tools.tool.${tool}.inline`) }) as string, 'error');
        }
      }
    });
    writeQueue.current = write;
    await write;
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

  /**
   * Run one call probe: a real, cost-bearing request the user asked for. Its
   * answer carries the status it left, which lands at once unless a write went
   * out while it ran (that write's answer is newer); an ordinary read then
   * reconciles, and if that read fails the probe's status still stands.
   * Null when the request itself failed.
   */
  const probe = React.useCallback(async (target: ProbeTarget): Promise<HostedToolProbeResult | null> => {
    const key = probeKey(target);
    const clock = order.current;
    const writesAtStart = clock.writes;
    const recordBefore = target.candidate.call.observed_at;
    const revisionAtStart = shownRevision.current;
    setProbing((previous) => new Set(previous).add(key));
    try {
      const { probe: result, hosted_tools: status } = await modelsApi.probeHostedTool(target.tool, {
        source_id: target.candidate.source_id,
        model_id: target.candidate.model_id,
      });
      if (alive.current && clock.writes === writesAtStart && clock.queued === 0 && shownRevision.current === revisionAtStart) {
        // Newer than any read still out, which must not overwrite it.
        clock.reads += 1;
        setRead(readyRegion(status));
      }
      return result;
    } catch {
      // The request may have run and recorded its evidence though its answer was
      // lost: read the record before calling it unanswered, and keep the check
      // running until then, so no second paid request starts meanwhile. The
      // status read for that lands as a probe's answer would.
      const recovered = await lostProbeOutcome(target, recordBefore);
      if (recovered && alive.current && clock.writes === writesAtStart && clock.queued === 0 && shownRevision.current === revisionAtStart) {
        clock.reads += 1;
        setRead(readyRegion(recovered.status));
      }
      return recovered?.result ?? null;
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

/**
 * What a probe whose answer was lost left behind: a new probe record for its
 * pair (one the read did not have before it) is its outcome; anything else is
 * no answer (null).
 */
async function lostProbeOutcome(
  target: ProbeTarget,
  recordBefore: string | null,
): Promise<{ result: HostedToolProbeResult | null; status: HostedToolsStatus } | null> {
  try {
    const status = await modelsApi.getHostedTools();
    const call = status.tools
      .find((tool) => tool.name === target.tool)?.candidates
      .find((candidate) => sameExecutor(candidate, target.candidate))?.call;
    if (!call || call.basis !== 'probe' || call.observed_at === recordBefore || call.status === 'unknown') {
      return { status, result: null };
    }
    return {
      status,
      result: {
        tool: target.tool,
        source_id: target.candidate.source_id,
        model_id: target.candidate.model_id,
        call: { outcome: call.status, error: null },
        replay: null,
      },
    };
  } catch {
    // Unread: still no answer.
    return null;
  }
}
