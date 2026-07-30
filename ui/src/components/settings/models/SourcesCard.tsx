// The 来源 band (design.pen 「产品改造 V6 01」): header (asset sub-line + 添加来源
// menu) over a plain inventory list. Fully controlled — `sources` comes from the
// page and this card holds no derived state.
//
// V6 turned this card into a pure asset inventory: identity · usage · billing ·
// health, and nothing else. Ordering is no longer a property of a source, it is a
// property of an Agent (a per-backend ordered subset), so the drag handle, the
// priority number and the one-step move actions moved into the per-Agent 来源顺序
// drawer. The list order here is whatever the server returns.
//
// Mobile header (design.pen M01 m01SrcHead): the title block and 添加来源 stack
// instead of competing for one line, with 添加来源 as a full-width primary
// button — at 360px the side-by-side desktop header squeezed the sub-line to two
// or three lines and left the button a cramped tap target.
import * as React from 'react';
import { useTranslation } from 'react-i18next';

import { AddSourceMenu } from './AddSourceMenu';
import { SourceRow } from './SourceRow';
import type { Source } from './types';

export const SourcesCard: React.FC<{
  sources: Source[];
  onConnectClaude: () => void;
  onConnectChatGPT: () => void;
  onAddApiKey: () => void;
  /** Re-fetch after a per-row action (rename / re-discover / delete). */
  onSourceChanged: () => void;
}> = ({ sources, onConnectClaude, onConnectChatGPT, onAddApiKey, onSourceChanged }) => {
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
            <SourceRow key={source.id} source={source} onChanged={onSourceChanged} />
          ))}
        </div>
      )}
    </section>
  );
};
