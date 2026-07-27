// The 来源 band (frame 01r): header (policy sub-line + 添加来源 menu) over a
// drag-to-reorder priority list. Fully controlled — the ordered `sources` and
// the reorder handlers live in the page, so this card holds no derived state.
// Drag is restricted to each row's handle via framer-motion drag controls.
//
// Mobile header (design.pen M01 m01SrcHead): the title block and 添加来源 stack
// instead of competing for one line, with 添加来源 as a full-width primary
// button — at 360px the side-by-side desktop header squeezed the sub-line to two
// or three lines and left the button a cramped tap target.
import * as React from 'react';
import { Reorder, useDragControls } from 'framer-motion';
import { useTranslation } from 'react-i18next';

import { AddSourceMenu } from './AddSourceMenu';
import { SourceRow } from './SourceRow';
import { movedOrder } from './reorder';
import type { Source } from './types';

const SourceReorderItem: React.FC<{
  source: Source;
  priority: number;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onMove: (delta: -1 | 1) => void;
  onCommit: () => void;
  onChanged: () => void;
}> = ({ source, priority, canMoveUp, canMoveDown, onMove, onCommit, onChanged }) => {
  const controls = useDragControls();
  return (
    <Reorder.Item
      value={source.id}
      dragListener={false}
      dragControls={controls}
      onDragEnd={onCommit}
      className="list-none bg-background"
    >
      <SourceRow
        source={source}
        priority={priority}
        onDragHandlePointerDown={(e) => controls.start(e)}
        canMoveUp={canMoveUp}
        canMoveDown={canMoveDown}
        onMove={onMove}
        onChanged={onChanged}
      />
    </Reorder.Item>
  );
};

export const SourcesCard: React.FC<{
  sources: Source[];
  /** Fires continuously during drag with the new id order (visual only). */
  onReorderPreview: (orderedIds: string[]) => void;
  /** Fires on drag end — persist the current order. */
  onReorderCommit: () => void;
  /** Preview + persist an explicit order (the row menu's one-step reorder). */
  onReorderTo: (orderedIds: string[]) => void;
  onConnectClaude: () => void;
  onConnectChatGPT: () => void;
  onAddApiKey: () => void;
  /** Re-fetch after a per-row action (rename / re-discover / delete). */
  onSourceChanged: () => void;
}> = ({
  sources,
  onReorderPreview,
  onReorderCommit,
  onReorderTo,
  onConnectClaude,
  onConnectChatGPT,
  onAddApiKey,
  onSourceChanged,
}) => {
  const { t } = useTranslation();
  const ids = sources.map((s) => s.id);

  const moveByOneStep = (index: number, delta: -1 | 1) => {
    const next = movedOrder(ids, index, delta);
    onReorderTo(next);
  };

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

      {ids.length === 0 ? (
        <div className="px-4 py-12 text-center sm:px-5 text-[13px] text-muted">{t('settings.models.sources.empty')}</div>
      ) : (
        <Reorder.Group axis="y" values={ids} onReorder={onReorderPreview} className="flex flex-col">
          {sources.map((source, index) => (
            <SourceReorderItem
              key={source.id}
              source={source}
              priority={index + 1}
              canMoveUp={index > 0}
              canMoveDown={index < sources.length - 1}
              onMove={(delta) => moveByOneStep(index, delta)}
              onCommit={onReorderCommit}
              onChanged={onSourceChanged}
            />
          ))}
        </Reorder.Group>
      )}
    </section>
  );
};
