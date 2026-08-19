import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Check, Copy, ExternalLink, LayoutGrid, Loader2, Plus, Share2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { Switch } from '../ui/switch';
import { useApi } from '../../context/ApiContext';
import { useDock } from '../../context/DockContext';
import { showDockId } from '../../context/dockDoc';
import { isIosDevice, isRealMobileSafari, isStandalonePwa } from '../../lib/platform';
import type { ShowPageAccess } from '../../lib/showPageAccess';
import { copyHref, type ShowPageLinkInfo } from '../../lib/showPageLinks';
import { copyTextToClipboard } from '../../lib/utils';
import { useShowPageInventory, type ShowPage } from '../useShowPages';
import { ShowPageSharingSettings } from './ShowPageSharingSettings';
import { ShowPageWorkspaceAccessControl } from './ShowPageWorkspaceAccessControl';

type ShowPagePayload = ShowPageLinkInfo & {
  url_available: boolean;
  url_guidance?: string | null;
  offline: boolean;
  title?: string | null;
};

export const ShowPageShareControl: React.FC<{
  sessionId: string;
  initialAccess?: ShowPageAccess | null;
  canManageInstance?: boolean;
  onPayloadChange?: (payload: ShowPageLinkInfo) => void;
  onOpenChange?: (open: boolean) => void;
  compact?: boolean;
  ownerWindowId?: string;
}> = ({
  sessionId,
  initialAccess = null,
  canManageInstance = false,
  onPayloadChange,
  onOpenChange,
  compact = false,
  ownerWindowId,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const dock = useDock();
  const { pages, mergePage, reload } = useShowPageInventory();
  const inventoryPage = pages.find((page) => page.session_id === sessionId);
  const [open, setOpen] = useState(false);
  const [localPayload, setLocalPayload] = useState<ShowPagePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [accessLoading, setAccessLoading] = useState(false);
  const [access, setAccess] = useState<ShowPageAccess | null>(initialAccess);
  const [accessError, setAccessError] = useState(false);
  const [copied, setCopied] = useState(false);
  const reqSeq = useRef(0);
  const hasObservedPayloadRef = useRef(false);

  const candidatePayload = useMemo<ShowPagePayload | null>(() => {
    const local = localPayload?.session_id === sessionId ? localPayload : null;
    if (!inventoryPage) return local;
    if (!local) return inventoryPage as ShowPagePayload;
    const inventoryCaughtUp = inventoryPage.visibility === local.visibility
      && inventoryPage.active_url === local.active_url
      && inventoryPage.share_id === local.share_id
      && inventoryPage.offline === local.offline
      && inventoryPage.url_available === local.url_available;
    return (inventoryCaughtUp
      ? { ...local, ...inventoryPage }
      : { ...inventoryPage, ...local }) as ShowPagePayload;
  }, [inventoryPage, localPayload, sessionId]);

  const canReadPayload = !accessError && (canManageInstance || access?.can_use === true);
  const payload = canReadPayload ? candidatePayload : null;
  const offline = payload?.visibility === 'offline' || payload?.offline === true;
  const link = payload && !offline ? copyHref(payload) ?? '' : '';
  const canNativeShare = typeof navigator !== 'undefined' && typeof navigator.share === 'function';
  const iosStandalone = isIosDevice() && isStandalonePwa();
  const showAddToHome = !!link && (iosStandalone || isRealMobileSafari());

  const applyPayload = (next: ShowPagePayload) => {
    setLocalPayload(next);
    mergePage(next as ShowPage);
  };

  const reloadPayload = async () => {
    const seq = ++reqSeq.current;
    if (!canReadPayload) {
      reload();
      return;
    }
    try {
      const next: ShowPagePayload = await api.getShowPage(sessionId);
      if (seq === reqSeq.current) applyPayload(next);
    } catch {
      reload();
    }
  };

  useEffect(() => {
    if (!payload) return;
    const hasFreshLocalPayload = localPayload?.session_id === sessionId;
    if (!hasObservedPayloadRef.current) {
      hasObservedPayloadRef.current = true;
      if (!hasFreshLocalPayload) return;
    }
    onPayloadChange?.(payload);
  }, [localPayload, onPayloadChange, payload, sessionId]);

  useEffect(() => () => onOpenChange?.(false), [onOpenChange]);

  useEffect(() => {
    setAccess(initialAccess);
    setAccessError(false);
  }, [initialAccess, sessionId]);

  const refresh = () => {
    const seq = ++reqSeq.current;
    const loadPayload = async (granted: boolean) => {
      if (!granted) {
        setLoading(false);
        return;
      }
      setLoading(!payload);
      try {
        const result: ShowPagePayload = await api.getShowPage(sessionId);
        if (seq !== reqSeq.current) return;
        applyPayload(result);
        if (!inventoryPage) reload();
      } catch {
        // A page can expose access metadata and still not be readable here (no
        // page row yet, or use access revoked). Opening this panel must never
        // create one, so there is nothing to recover — leave the link empty.
      } finally {
        setLoading(false);
      }
    };
    const readAccess = (thenLoadPayload: boolean) => {
      setAccessLoading(!access);
      setAccessError(false);
      void (async () => {
        let nextAccess: ShowPageAccess | null = null;
        try {
          nextAccess = await api.getShowPageAccess(sessionId);
          if (seq !== reqSeq.current) return;
          setAccess(nextAccess);
        } catch {
          if (seq !== reqSeq.current) return;
          setAccess(null);
          setAccessError(true);
          return;
        } finally {
          setAccessLoading(false);
        }
        if (thenLoadPayload) {
          await loadPayload(canManageInstance || nextAccess?.can_use === true);
        }
      })();
    };
    if (canManageInstance || access?.can_use === true) {
      void loadPayload(true);
      readAccess(false);
    } else {
      readAccess(true);
    }
  };

  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    onOpenChange?.(next);
    if (next) refresh();
  };

  const handleShowAccessApplied = () => {
    void reloadPayload();
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
      // The native sheet may be dismissed without changing state.
    }
  };

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className={compact
            ? 'size-6 shrink-0 rounded-md text-muted hover:bg-foreground/[0.06] hover:text-foreground'
            : 'size-7 shrink-0'}
          aria-label={t('chat.showPage.share')}
        >
          <Share2 className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="max-h-[var(--radix-popover-content-available-height)] w-[min(23rem,calc(100vw-1rem))] space-y-3 overflow-y-auto"
        data-window-owner-id={ownerWindowId}
      >
        <div className="text-sm font-medium">{t('chat.showPage.shareTitle')}</div>

        {(loading || accessLoading) && !access ? (
          <div className="flex h-9 items-center gap-2 text-sm text-muted">
            <Loader2 className="size-4 animate-spin" />
            {t('common.loading')}
          </div>
        ) : !payload && !access ? (
          <p className="py-1 text-sm text-muted">{t('chat.showPage.loadError')}</p>
        ) : null}

        {payload && !offline ? (
          <div className="flex items-center gap-1.5">
            <Input
              id="show-share-link"
              readOnly
              value={link}
              onFocus={(event) => event.currentTarget.select()}
              className="h-8 min-w-0 flex-1 text-xs"
            />
            {link ? (
              <Button
                asChild
                variant="outline"
                size="icon"
                className="size-8 shrink-0"
              >
                <a
                  href={link}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={t('showPages.open')}
                >
                  <ExternalLink className="size-3.5" />
                </a>
              </Button>
            ) : (
              <Button
                type="button"
                variant="outline"
                size="icon"
                className="size-8 shrink-0"
                disabled
                aria-label={t('showPages.open')}
              >
                <ExternalLink className="size-3.5" />
              </Button>
            )}
            <Button
              type="button"
              variant="outline"
              size="icon"
              className="size-8 shrink-0"
              disabled={!link}
              onClick={copyLink}
              aria-label={t('chat.showPage.copyLink')}
            >
              {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            </Button>
            {canNativeShare ? (
              <Button
                type="button"
                variant="outline"
                size="icon"
                className="size-8 shrink-0"
                disabled={!link}
                onClick={nativeShare}
                aria-label={t('chat.showPage.nativeShare')}
              >
                <Share2 className="size-3.5" />
              </Button>
            ) : null}
          </div>
        ) : null}

        {access?.can_publish_public ? (
          <div className="border-t border-border pt-3">
            <ShowPageSharingSettings
              active={open}
              canManage
              sessionId={sessionId}
              onApplied={handleShowAccessApplied}
              ownerWindowId={ownerWindowId}
              showCustomLink={access.mode !== 'organization' && access.mode !== 'organization_pending'}
            />
          </div>
        ) : null}

        {access && (
          access.ownership_status === 'conflict'
          || access.mode === 'organization'
          || access.mode === 'organization_pending'
          || access.mode === 'configuration_unavailable'
        ) ? (
          <div className="border-t border-border pt-3">
            <ShowPageWorkspaceAccessControl
              access={access}
              active={open}
              canManageInstance={canManageInstance}
              sessionId={sessionId}
              ownerWindowId={ownerWindowId}
            />
          </div>
        ) : null}

        {accessError ? (
          <p className="border-t border-border pt-3 text-[11px] leading-snug text-destructive-ink">
            {t('chat.showPage.accessLoadError')}
          </p>
        ) : null}

        {showAddToHome ? (
          <div className="border-t border-border pt-3">
            <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
              <Plus className="size-3.5 shrink-0 text-cyan-ink" />
              {t('chat.showPage.addToHomeTitle')}
            </div>
            {iosStandalone ? (
              <p className="mt-1 text-xs leading-relaxed text-muted">
                {t('chat.showPage.addToHomeBodyPwa')}
              </p>
            ) : (
              <a
                href={link}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-1 block text-xs leading-relaxed text-foreground underline underline-offset-2"
              >
                {t('chat.showPage.addToHomeBodySafari')}
              </a>
            )}
          </div>
        ) : null}

        {payload && canManageInstance ? (
          <div className="border-t border-border pt-3">
            <div className="flex items-center gap-3">
              <span className="grid size-9 shrink-0 place-items-center rounded-md border border-border bg-foreground/[0.03] text-cyan-ink">
                <LayoutGrid className="size-4" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">{t('chat.showPage.pinToDock')}</div>
                <div className="text-xs text-muted">{t('chat.showPage.pinToDockCaption')}</div>
              </div>
              <Switch
                checked={dock.isDocked(showDockId(sessionId))}
                onCheckedChange={(next) => {
                  if (next) {
                    void (dock.isPinned(sessionId) ? dock.dock(showDockId(sessionId)) : dock.pin(sessionId));
                  } else {
                    void dock.undock(showDockId(sessionId));
                  }
                }}
                label={t('chat.showPage.pinToDock')}
              />
            </div>
            {dock.isDocked(showDockId(sessionId)) ? (
              <div className="mt-2 flex items-center gap-1.5 rounded-md border border-mint/30 bg-mint/[0.08] px-2.5 py-1.5 text-xs text-foreground">
                <Check className="size-3.5 shrink-0 text-mint-ink" />
                <span className="truncate">
                  {t('chat.showPage.pinnedConfirm', {
                    title: (payload.title ?? '').trim() || t('chat.showPage.title'),
                  })}
                </span>
              </div>
            ) : null}
          </div>
        ) : null}
      </PopoverContent>
    </Popover>
  );
};
