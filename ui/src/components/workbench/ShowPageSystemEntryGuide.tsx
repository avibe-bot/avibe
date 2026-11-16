import React from 'react';
import { AlertCircle, Copy, ExternalLink, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '../ui/button';

export type ShowPageSystemEntryMode = 'ios' | 'mac' | 'other' | 'native' | 'unavailable';

export const ShowPageSystemEntryGuide: React.FC<{
  mode: ShowPageSystemEntryMode;
  link?: string;
  onOpenBrowser?: () => void;
  onCopyLink?: () => void;
}> = ({ mode, link, onOpenBrowser, onCopyLink }) => {
  const { t } = useTranslation();
  const isUnavailable = mode === 'unavailable';
  const title = mode === 'unavailable'
    ? t('chat.showPage.systemEntryUnavailableTitle')
    : mode === 'ios'
      ? t('chat.showPage.addToHomeTitle')
      : mode === 'mac'
        ? t('chat.showPage.addToDockTitle')
        : t('chat.showPage.addToSystemTitle');

  return (
    <div className="pt-2">
      <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
        {isUnavailable ? (
          <AlertCircle className="size-3.5 shrink-0 text-muted" />
        ) : (
          <Plus className="size-3.5 shrink-0 text-cyan-ink" />
        )}
        {title}
      </div>

      {isUnavailable ? (
        <p className="mt-1 text-xs leading-relaxed text-muted">
          {t('chat.showPage.systemEntryUnavailableBody')}
        </p>
      ) : mode === 'native' ? (
        <div className="mt-2 space-y-2">
          <p className="text-xs leading-relaxed text-muted">
            {t('chat.showPage.openInBrowserBody')}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" size="sm" className="h-7 shrink-0 px-2.5" onClick={onOpenBrowser}>
              <ExternalLink className="mr-1.5 size-3.5" />
              {t('chat.showPage.openInBrowser')}
            </Button>
            <Button type="button" variant="ghost" size="sm" className="h-7 shrink-0 px-2.5" onClick={onCopyLink}>
              <Copy className="mr-1.5 size-3.5" />
              {t('chat.showPage.copyLink')}
            </Button>
          </div>
        </div>
      ) : mode === 'ios' || mode === 'mac' ? (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <p className="min-w-0 flex-1 text-xs leading-relaxed text-muted">
            {t(mode === 'ios' ? 'chat.showPage.addToHomeBodySafari' : 'chat.showPage.addToDockBodySafari')}
          </p>
          <Button asChild variant="outline" size="sm" className="h-7 shrink-0 px-2.5">
            <a href={link} target="_blank" rel="noopener noreferrer">
              <ExternalLink className="mr-1.5 size-3.5" />
              {t('chat.showPage.openShowPage')}
            </a>
          </Button>
        </div>
      ) : (
        <p className="mt-1 text-xs leading-relaxed text-muted">
          {t('chat.showPage.addToSystemBodyOther')}
        </p>
      )}
    </div>
  );
};
