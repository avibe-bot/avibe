import { useSyncExternalStore } from 'react';
import { RotateCcw, Settings } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import {
  EDITOR_FONT_DEFAULT,
  EDITOR_FONT_MAX,
  EDITOR_FONT_MIN,
  adjustEditorFontSize,
  getEditorFontSize,
  resetEditorFontSize,
  subscribeEditorFontSize,
} from '../../lib/editorFontSize';
import { Button } from '../ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';

export const EditorFontSizePopover: React.FC<{ trigger: 'activity' | 'mobile' }> = ({ trigger }) => {
  const { t } = useTranslation();
  const size = useSyncExternalStore(
    subscribeEditorFontSize,
    getEditorFontSize,
    () => EDITOR_FONT_DEFAULT,
  );
  const settingsLabel = t('apps.editor.settings');

  return (
    <Popover>
      <PopoverTrigger asChild>
        {trigger === 'activity' ? (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-8 w-12 rounded-none text-muted hover:bg-foreground/[0.06] hover:text-foreground"
            aria-label={settingsLabel}
          >
            <Settings className="size-5" />
          </Button>
        ) : (
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-7 shrink-0 text-muted"
            aria-label={settingsLabel}
          >
            <span aria-hidden className="text-[12px] font-semibold">Aa</span>
          </Button>
        )}
      </PopoverTrigger>
      <PopoverContent
        data-theme={trigger === 'activity' ? 'dark' : undefined}
        side={trigger === 'activity' ? 'right' : 'bottom'}
        align="end"
        sideOffset={8}
        className="w-60 p-2.5"
      >
        <div className="mb-2 text-[12px] font-semibold text-foreground">{settingsLabel}</div>
        {/* Each editor setting owns one row so word wrap and future preferences can extend this list. */}
        <ul aria-label={settingsLabel} className="divide-y divide-border">
          <li className="flex items-center justify-between gap-3 py-1">
            <span className="text-[12px] text-muted">{t('apps.editor.fontSize')}</span>
            <div
              role="group"
              aria-label={t('apps.editor.fontSize')}
              className="flex h-8 shrink-0 items-center overflow-hidden rounded-md border border-border"
            >
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-8 rounded-none border-r border-border px-0 text-[11px]"
                aria-label={t('apps.editor.decreaseFontSize')}
                disabled={size <= EDITOR_FONT_MIN}
                onClick={() => adjustEditorFontSize(-1)}
              >
                <span aria-hidden>A−</span>
              </Button>
              <output
                aria-label={t('apps.editor.currentFontSize', { size })}
                aria-live="polite"
                className="w-8 text-center font-mono text-[12px] tabular-nums text-foreground"
              >
                {size}
              </output>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-8 rounded-none border-l border-border px-0 text-[11px]"
                aria-label={t('apps.editor.increaseFontSize')}
                disabled={size >= EDITOR_FONT_MAX}
                onClick={() => adjustEditorFontSize(1)}
              >
                <span aria-hidden>A+</span>
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-8 rounded-none border-l border-border px-0 text-muted"
                aria-label={t('apps.editor.resetFontSize')}
                disabled={size === EDITOR_FONT_DEFAULT}
                onClick={resetEditorFontSize}
              >
                <RotateCcw className="size-3.5" />
              </Button>
            </div>
          </li>
        </ul>
      </PopoverContent>
    </Popover>
  );
};
