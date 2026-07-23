// The 来源 band (frame 01r): header (policy sub-line + 添加来源 menu) over a
// drag-to-reorder priority list. Fully controlled — the ordered `sources` and
// the reorder handlers live in the page, so this card holds no derived state.
// Drag is restricted to each row's handle via framer-motion drag controls.
import * as React from 'react';
import { Reorder, useDragControls } from 'framer-motion';
import { useTranslation } from 'react-i18next';

import { AddSourceMenu } from './AddSourceMenu';
import { SourceRow } from './SourceRow';
import type { Source } from './types';

const SourceReorderItem: React.FC<{ source: Source; priority: number; onCommit: () => void }> = ({
  source,
  priority,
  onCommit,
}) => {
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
  onConnectClaude: () => void;
  onConnectChatGPT: () => void;
  onAddApiKey: () => void;
}> = ({ sources, onReorderPreview, onReorderCommit, onConnectClaude, onConnectChatGPT, onAddApiKey }) => {
  const { t } = useTranslation();
  const ids = sources.map((s) => s.id);

  return (
    // Not overflow-hidden: the row supply tooltip must escape the card bounds.
    <section className="rounded-xl border border-border bg-background">
      <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
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
        <div className="px-5 py-12 text-center text-[13px] text-muted">{t('settings.models.sources.empty')}</div>
      ) : (
        <Reorder.Group axis="y" values={ids} onReorder={onReorderPreview} className="flex flex-col">
          {sources.map((source, index) => (
            <SourceReorderItem key={source.id} source={source} priority={index + 1} onCommit={onReorderCommit} />
          ))}
        </Reorder.Group>
      )}
    </section>
  );
};
