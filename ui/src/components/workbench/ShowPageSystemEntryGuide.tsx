import React from 'react';
import { ExternalLink, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '../ui/button';

export type ShowPageSystemEntryMode = 'ios' | 'mac' | 'other' | 'native' | 'unavailable';

export const ShowPageSystemEntryGuide: React.FC<{
  mode: ShowPageSystemEntryMode;
  link?: string;
  onOpenBrowser?: () => void;
}> = ({ mode, link, onOpenBrowser }) => {
  const { t } = useTranslation();
  const title = mode === 'unavailable'
    ? t('chat.showPage.systemEntryUnavailableTitle')
    : mode === 'ios'
      ? t('chat.showPage.addToHomeTitle')
      : mode === 'mac'
        ? t('chat.showPage.addToDockTitle')
        : t('chat.showPage.addToSystemTitle');

  return (
    <div className="border-t border-border pt-3">
      <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
        <Plus className="size-3.5 shrink-0 text-cyan-ink" />
        {title}
      </div>

      {mode === 'unavailable' ? (
        <p className="mt-1 text-xs leading-relaxed text-muted">
          {t('chat.showPage.systemEntryUnavailableBody')}
        </p>
      ) : mode === 'native' ? (
        <div className="mt-2 flex items-center justify-between gap-2">
          <p className="text-xs leading-relaxed text-muted">
            {t('chat.showPage.openInBrowserBody')}
          </p>
          <Button type="button" variant="outline" size="sm" className="h-7 shrink-0 px-2.5" onClick={onOpenBrowser}>
            <ExternalLink className="mr-1.5 size-3.5" />
            {t('chat.showPage.openInBrowser')}
          </Button>
        </div>
      ) : mode === 'ios' ? (
        <a
          href={link}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-1 block text-xs leading-relaxed text-foreground underline underline-offset-2"
        >
          {t('chat.showPage.addToHomeBodySafari')}
        </a>
      ) : mode === 'mac' ? (
        <a
          href={link}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-1 block text-xs leading-relaxed text-foreground underline underline-offset-2"
        >
          {t('chat.showPage.addToDockBodySafari')}
        </a>
      ) : (
        <p className="mt-1 text-xs leading-relaxed text-muted">
          {t('chat.showPage.addToSystemBodyOther')}
        </p>
      )}
    </div>
  );
};
