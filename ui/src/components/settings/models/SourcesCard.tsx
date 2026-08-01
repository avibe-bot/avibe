// Demoted account-management section. Each source keeps its repair journeys and
// exposes the server-provided model inventory plus the shared manual-model action.
//
// Mobile header (design.pen M01 m01SrcHead): the title block and 添加来源 stack
// instead of competing for one line, with 添加来源 as a full-width primary
// button — at 360px the side-by-side desktop header squeezed the sub-line to two
// or three lines and left the button a cramped tap target.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { AddSourceMenu } from './AddSourceMenu';
import { SourceRow } from './SourceRow';
import type { RaisedRepair } from './SourceRowMenu';
import type { Source } from './types';

export const SourcesCard: React.FC<{
  sources: Source[];
  onConnectClaude: () => void;
  onConnectChatGPT: () => void;
  onAddApiKey: () => void;
  /** Re-fetch after a per-row action (rename / delete). */
  onSourceChanged: () => void;
  onRefreshSource: (source: Source) => void;
  refreshingSourceId: string | null;
  /** Open a repair journey the page hosts — see SourceRowMenu's `onRepair`. */
  onRepair?: (source: Source, kind: RaisedRepair) => void;
  onAddModel: (source: Source) => void;
}> = ({
  sources,
  onConnectClaude,
  onConnectChatGPT,
  onAddApiKey,
  onSourceChanged,
  onRefreshSource,
  refreshingSourceId,
  onRepair,
  onAddModel,
}) => {
  const { t } = useTranslation();

  return (
    // Not overflow-hidden: the row supply tooltip must escape the card bounds.
    <section className="rounded-xl border border-border bg-background">
      <div className="flex flex-col gap-2.5 border-b border-border px-4 py-4 sm:flex-row sm:items-start sm:justify-between sm:gap-4 sm:px-5">
        <div className="flex min-w-0 flex-col gap-1">
          <h2 className="text-[15px] font-semibold text-foreground">{t('settings.models.sources.title')}</h2>
          <p className="text-[12px] leading-relaxed text-muted">{t('settings.models.sources.subtitle')}</p>
        </div>
        <AddSourceMenu
          onConnectClaude={onConnectClaude}
          onConnectChatGPT={onConnectChatGPT}
          onAddApiKey={onAddApiKey}
        />
      </div>

      {sources.length === 0 ? (
        <div className="px-4 py-12 text-center sm:px-5 text-[13px] text-muted">{t('settings.models.sources.empty')}</div>
      ) : (
        <div className="flex flex-col">
          {sources.map((source) => (
            <SourceRow
              key={source.id}
              source={source}
              onChanged={onSourceChanged}
              onRefresh={onRefreshSource}
              refreshing={refreshingSourceId === source.id}
              refreshDisabled={refreshingSourceId !== null}
              onRepair={onRepair}
              onAddModel={onAddModel}
            />
          ))}
        </div>
      )}
    </section>
  );
};
