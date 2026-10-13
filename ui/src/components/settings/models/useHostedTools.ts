import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '@/context/ToastContext';
import { createLatestAsyncAuthority } from './asyncLifetime';
import { modelsApi } from './modelsApi';
import { beginRegionRead, loadingRegion, readRegion, readyRegion, settleRegionRead, type RegionRead } from './regionRead';
import type { HostedToolName, HostedToolPut, HostedToolsStatus } from './types';

/**
 * The hosted-tools read, shared by the Tools tab and the Source chips.
 *
 * Read lazily, while either surface is open, like the other report tabs. A
 * write answers the whole status, so it lands through the same latest-wins
 * authority as a read: an older read cannot paint over a newer write.
 */
export function useHostedTools(active: boolean) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const alive = React.useRef(true);
  React.useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const [read, setRead] = React.useState<RegionRead<HostedToolsStatus>>(loadingRegion);
  const [pending, setPending] = React.useState<ReadonlySet<HostedToolName>>(() => new Set());
  const [authority] = React.useState(() => createLatestAsyncAuthority<RegionRead<HostedToolsStatus>>((incoming) => {
    if (alive.current) setRead((previous) => settleRegionRead(previous, incoming));
  }));

  const refresh = React.useCallback(async () => {
    setRead(beginRegionRead);
    await authority.run(() => readRegion(() => modelsApi.getHostedTools()));
  }, [authority]);

  React.useEffect(() => {
    if (active) void refresh();
  }, [active, refresh]);

  const save = React.useCallback(async (tool: HostedToolName, body: HostedToolPut) => {
    setPending((previous) => new Set(previous).add(tool));
    try {
      await authority.run(async () => readyRegion(await modelsApi.putHostedTool(tool, body)));
    } catch {
      if (!alive.current) return;
      // A refused write changed nothing, and a lost answer may have committed;
      // either way the tab converges on what the server now holds.
      showToast(t('settings.models.tools.saveFailed', { toolInline: t(`settings.models.tools.tool.${tool}.inline`) }) as string, 'error');
      void refresh();
    } finally {
      if (alive.current) {
        setPending((previous) => {
          const next = new Set(previous);
          next.delete(tool);
          return next;
        });
      }
    }
  }, [authority, refresh, showToast, t]);

  return { read, pending, refresh, save };
}
