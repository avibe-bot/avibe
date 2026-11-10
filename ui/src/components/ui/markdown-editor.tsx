import * as React from 'react';
import { Suspense, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { SegmentedRadio } from './segmented';
import { Textarea } from './textarea';
import { DeferredMarkdown } from './deferredMarkdown';

type EditorMode = 'edit' | 'preview';

export interface MarkdownEditorProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  /** Show the Edit/Preview toggle + Markdown preview (default true). */
  markdownPreview?: boolean;
  /** Called on Cmd/Ctrl+Enter from inside the textarea. */
  onSubmit?: () => void;
  /** Focus the textarea on mount (default true). */
  autoFocus?: boolean;
}

// The Markdown editing surface shared by EditorDialog and the Global Prompts
// dialog: a large monospace textarea with an optional Edit/Preview toggle that
// renders through the shared <Markdown> component and the .vr-markdown styling.
// Controlled (value / onChange); it owns only the local edit/preview mode, so
// any container — a full modal or a tabbed panel — can host the same surface.
// It fills its parent's remaining height (min-h-0 flex-1 flex-col).
export const MarkdownEditor: React.FC<MarkdownEditorProps> = ({
  value,
  onChange,
  placeholder,
  markdownPreview = true,
  onSubmit,
  autoFocus = true,
}) => {
  const { t } = useTranslation();
  const [mode, setMode] = useState<EditorMode>('edit');
  const showPreview = markdownPreview && mode === 'preview';

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Toolbar — Edit/Preview toggle + Markdown hint (preview-capable only). */}
      {markdownPreview && (
        <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-2.5">
          <div className="w-[200px]">
            <SegmentedRadio<EditorMode>
              value={mode}
              onChange={setMode}
              ariaLabel={t('editor.modeLabel')}
              options={[
                { id: 'edit', label: t('editor.edit') },
                { id: 'preview', label: t('editor.preview') },
              ]}
            />
          </div>
          <span className="font-mono text-[10px] text-muted">{t('editor.markdownHint')}</span>
        </div>
      )}

      {/* Body — editor or preview, fills the remaining height. */}
      <div className="min-h-0 flex-1 overflow-hidden p-5">
        {showPreview ? (
          <div className="h-full overflow-auto rounded-lg border border-edge bg-surface-2 px-4 py-3 shadow-elevation-0">
            {value.trim() ? (
              <Suspense fallback={null}><DeferredMarkdown content={value} /></Suspense>
            ) : (
              <span className="text-[12px] text-muted">{t('editor.previewEmpty')}</span>
            )}
          </div>
        ) : (
          <Textarea
            autoFocus={autoFocus}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={(e) => {
              // Cmd/Ctrl+Enter submits from anywhere in the textarea.
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                e.preventDefault();
                onSubmit?.();
              }
            }}
            placeholder={placeholder}
            className="h-full resize-none font-mono text-[13px] leading-relaxed"
          />
        )}
      </div>
    </div>
  );
};
