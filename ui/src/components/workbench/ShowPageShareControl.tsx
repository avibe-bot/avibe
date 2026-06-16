import React, { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Copy, Loader2, Share2 } from 'lucide-react';

import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { Switch } from '../ui/switch';
import { useApi } from '../../context/ApiContext';
import { copyTextToClipboard } from '../../lib/utils';
import { copyHref, type ShowPageLinkInfo } from '../../lib/showPageLinks';

type ShowPagePayload = ShowPageLinkInfo & {
  url_available: boolean;
  url_guidance: string | null;
  offline: boolean;
};

// Share affordance shown only while the Show Page is open (the in-chat view).
// A popover with the page link (copy + native share) and a public/private
// toggle that flips visibility in place via the existing show-pages API.
export const ShowPageShareControl: React.FC<{
  sessionId: string;
  // Lets the chat view re-point the iframe at the route that now serves the
  // page when visibility flips (private↔public swap the serving route).
  onPayloadChange?: (payload: ShowPageLinkInfo) => void;
}> = ({ sessionId, onPayloadChange }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [payload, setPayload] = useState<ShowPagePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  // A visibility mutation is authoritative: it always applies its result and
  // clears its own busy state, and it invalidates any refresh read issued
  // before it resolved. A refresh (read) only applies if no newer request has
  // superseded it. reqSeq orders the reads; a mutation bumps it on resolve.
  const reqSeq = useRef(0);

  useEffect(() => {
    if (payload) onPayloadChange?.(payload);
  }, [payload, onPayloadChange]);

  const offline = payload?.visibility === 'offline' || payload?.offline === true;
  const isPublic = payload?.visibility === 'public';
  // Absolute, copyable href; falls back to the same-origin route when Avibe
  // Cloud is off (payload urls null). The field shows this full url so a manual
  // select/copy yields the same link as the Copy button.
  const link = payload ? copyHref(payload) ?? '' : '';
  const canNativeShare = typeof navigator !== 'undefined' && typeof navigator.share === 'function';

  // Re-fetch on every open so a visibility/share change made elsewhere (e.g. the
  // admin Show Pages page) is reflected; keep the last payload visible while
  // refreshing so reopening doesn't flash a spinner.
  const refresh = () => {
    const seq = ++reqSeq.current;
    setLoading(!payload);
    api
      .ensureShowPage(sessionId)
      .then((res: ShowPagePayload) => {
        if (seq === reqSeq.current) setPayload(res);
      })
      .catch(() => undefined)
      .finally(() => setLoading(false));
  };

  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    if (next) refresh();
  };

  const toggleVisibility = (nextPublic: boolean) => {
    setBusy(true);
    api
      .setShowPageVisibility(sessionId, nextPublic ? 'public' : 'private')
      .then((res: ShowPagePayload) => {
        // Authoritative server state: always apply it, and invalidate any
        // in-flight refresh read so a stale read can't revert us afterwards.
        setPayload(res);
        reqSeq.current += 1;
      })
      .catch(() => undefined)
      .finally(() => setBusy(false));
  };

  const copyLink = async () => {
    if (!link) return;
    if (await copyTextToClipboard(link)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    }
  };

  const nativeShare = async () => {
    if (!link) return;
    try {
      await navigator.share({ title: t('chat.showPage.title'), url: link });
    } catch {
      // user dismissed the share sheet, or it is unavailable — no-op
    }
  };

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          aria-label={t('chat.showPage.share')}
          title={t('chat.showPage.share')}
        >
          <Share2 className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 space-y-3">
        <div className="text-sm font-medium">{t('chat.showPage.shareTitle')}</div>

        {loading ? (
          <div className="flex items-center gap-2 py-2 text-sm text-muted">
            <Loader2 className="size-4 animate-spin" />
            {t('common.loading')}
          </div>
        ) : !payload ? (
          <p className="py-1 text-sm text-muted">{t('chat.showPage.loadError')}</p>
        ) : offline ? (
          <p className="py-1 text-sm text-muted">{t('chat.showPage.offlineNote')}</p>
        ) : (
          <>
            <div className="flex items-center gap-1.5">
              <Input
                id="show-share-link"
                readOnly
                value={link}
                onFocus={(e) => e.currentTarget.select()}
                className="h-8 flex-1 text-xs"
              />
              <Button
                type="button"
                variant="outline"
                size="icon"
                className="size-8 shrink-0"
                disabled={!link}
                onClick={copyLink}
                aria-label={t('chat.showPage.copyLink')}
                title={t('chat.showPage.copyLink')}
              >
                {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
              </Button>
              {canNativeShare && (
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  className="size-8 shrink-0"
                  disabled={!link}
                  onClick={nativeShare}
                  aria-label={t('chat.showPage.nativeShare')}
                  title={t('chat.showPage.nativeShare')}
                >
                  <Share2 className="size-3.5" />
                </Button>
              )}
            </div>

            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm">
                  {isPublic ? t('chat.showPage.visibilityPublic') : t('chat.showPage.visibilityPrivate')}
                </div>
                <div className="text-xs text-muted">
                  {isPublic ? t('chat.showPage.publicDesc') : t('chat.showPage.privateDesc')}
                </div>
              </div>
              <Switch
                checked={isPublic}
                disabled={busy}
                onCheckedChange={toggleVisibility}
                label={t('chat.showPage.visibilityPublic')}
              />
            </div>

            {payload && isPublic && !payload.url_available && (
              <div className="rounded-md border border-border px-2.5 py-2 text-xs text-muted">
                {t('chat.showPage.publicUnavailable')}
              </div>
            )}
          </>
        )}
      </PopoverContent>
    </Popover>
  );
};
