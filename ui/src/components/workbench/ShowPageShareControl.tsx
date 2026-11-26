import React, { Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Check, Copy, ExternalLink, FileText, Image as ImageIcon, LayoutGrid, Loader2, Share2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { Input } from '../ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { Switch } from '../ui/switch';
import { useApi } from '../../context/ApiContext';
import { useDock } from '../../context/DockContext';
import { showDockId } from '../../context/dockDoc';
import { useToast } from '../../context/ToastContext';
import {
  hasNativeShare,
  hasNativeShareExport,
  isNativeShell,
  nativeSharePresent,
  openNativeExternal,
  type NativeShareKind,
  type NativeShareResult,
} from '../../lib/native';
import { isIosDevice, isRealMobileSafari, isRealSafari, isStandalonePwa } from '../../lib/platform';
import type { ShowPageAccess } from '../../lib/showPageAccess';
import { copyHref, type ShowPageLinkInfo, type ShowPagePayload } from '../../lib/showPageLinks';
import { cn, copyTextToClipboard } from '../../lib/utils';
import { lazyPage } from '../../lib/lazyPage';
import { useShowPageInventory, type ShowPage } from '../useShowPages';
import { ShowPageSystemEntryGuide, type ShowPageSystemEntryMode } from './ShowPageSystemEntryGuide';

// The chat header draws only the Share trigger; fetch audience settings when
// its popover first opens, rather than with every conversation's first screen.
const ShowPageSharingSettings = lazyPage(() =>
  import('./ShowPageSharingSettings').then((module) => module.ShowPageSharingSettings));

type ShowExportKind = Exclude<NativeShareKind, 'link'>;

/**
 * The frame the native shell captures: an iframe marked with this Session, shown on screen now. A
 * frame that takes no pointer events (annotation mode) is covered by its own tools, and the shell's
 * hit test cannot reach it, so it is not offered. Read when the popover opens, before the host
 * reacts to the open popover with the same style.
 */
function isShowFrameVisible(sessionId: string): boolean {
  if (typeof document === 'undefined') return false;
  return Array.from(document.querySelectorAll<HTMLIFrameElement>('iframe[data-avibe-show-session]'))
    .some((frame) => {
      if (frame.dataset.avibeShowSession !== sessionId || !frame.isConnected) return false;
      if (typeof frame.checkVisibility === 'function' && !frame.checkVisibility()) return false;
      if (window.getComputedStyle(frame).pointerEvents === 'none') return false;
      const rect = frame.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0
        && rect.right > 0 && rect.bottom > 0
        && rect.left < window.innerWidth && rect.top < window.innerHeight;
    });
}

// Overlays still playing their exit motion (index.css `motion-*`); Radix unmounts each when its
// motion ends. A capture taken earlier would include them.
const LEAVING_OVERLAY = ['motion-popover', 'motion-dialog', 'motion-card', 'motion-scrim']
  .map((name) => `.${name}[data-state="closed"]`)
  .join(', ');
const OVERLAY_EXIT_LIMIT_MS = 1000;

const nextFrame = () => new Promise<void>((resolve) => {
  window.requestAnimationFrame(() => resolve());
});

/** Resolves once the share UI has left the screen: its exit motion is over and two frames painted. */
async function waitForShareUiToLeave(): Promise<void> {
  const start = performance.now();
  await nextFrame();
  while (document.querySelector(LEAVING_OVERLAY) && performance.now() - start < OVERLAY_EXIT_LIMIT_MS) {
    await nextFrame();
  }
  await nextFrame();
  await nextFrame();
}

export const ShowPageShareControl: React.FC<{
  sessionId: string;
  initialAccess?: ShowPageAccess | null;
  canManageInstance?: boolean;
  /** Dock pin/undock authority. `/api/dock/pins` is an editor-tier route, so
   *  this is deliberately NOT `canManageInstance`. */
  canPinToDock?: boolean;
  onPayloadChange?: (payload: ShowPageLinkInfo) => void;
  onOpenChange?: (open: boolean) => void;
  compact?: boolean;
  ownerWindowId?: string;
  /** The popover is opened by something else (a native Show window's title-bar button), so the
   *  trigger stays out of sight, out of the tab order, and takes no focus back. */
  anchorOnly?: boolean;
}> = ({
  sessionId,
  initialAccess = null,
  canManageInstance = false,
  canPinToDock = false,
  onPayloadChange,
  onOpenChange,
  compact = false,
  ownerWindowId,
  anchorOnly = false,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
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
  const [frameVisible, setFrameVisible] = useState(false);
  const [exportConfirm, setExportConfirm] = useState<ShowExportKind | null>(null);
  const [shareBusy, setShareBusy] = useState<NativeShareKind | null>(null);
  const shareBusyRef = useRef(false);
  const exportConfirmRef = useRef(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
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
  const nativeShell = isNativeShell();
  const nativeShareAvailable = hasNativeShare();
  const exportAvailable = hasNativeShareExport();
  const canWebShare = typeof navigator !== 'undefined' && typeof navigator.share === 'function';
  const canShareLink = nativeShareAvailable || canWebShare;
  // PDF and image exist only in a shell that exports: the phone app, or a desktop Show window. A
  // phone app that predates them explains the update instead.
  const showExport = (nativeShell || exportAvailable) && !!payload && frameVisible;
  const iosStandalone = isIosDevice() && isStandalonePwa();
  const safariBrowser = isRealSafari();
  const iosSafari = isRealMobileSafari();
  const systemEntryUnavailable = nativeShell && payload?.url_available === false;
  const showSystemEntry = Boolean(payload && !offline && (link || systemEntryUnavailable));
  const systemEntryMode: ShowPageSystemEntryMode = systemEntryUnavailable
    ? 'unavailable'
    : nativeShell
      ? 'native'
      : iosSafari || iosStandalone
        ? 'ios'
        : safariBrowser && !isIosDevice()
          ? 'mac'
          : 'other';

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
    if (next) {
      setFrameVisible(isShowFrameVisible(sessionId));
      refresh();
    }
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

  const openInBrowser = async () => {
    if (!link) return;
    await openNativeExternal(link);
  };

  const reportShareResult = (result: NativeShareResult) => {
    // The system sheet closing is the whole answer to a completed or cancelled share.
    if (result.status === 'completed' || result.status === 'cancelled') return;
    if (result.status === 'expired') {
      showToast(t('chat.showPage.shareExpired'), 'error');
    } else if (result.error?.code === 'capability_mismatch') {
      showToast(t('chat.showPage.shareNeedsUpdate'), 'error');
    } else {
      showToast(t('chat.showPage.shareFailed'), 'error');
    }
  };

  const presentNativeShare = async (kind: NativeShareKind) => {
    if (shareBusyRef.current) return;
    shareBusyRef.current = true;
    setShareBusy(kind);
    try {
      if (kind !== 'link') await waitForShareUiToLeave();
      reportShareResult(await nativeSharePresent(kind, sessionId));
    } finally {
      shareBusyRef.current = false;
      setShareBusy(null);
      if (kind !== 'link' && !anchorOnly) triggerRef.current?.focus({ preventScroll: true });
    }
  };

  const shareLink = async () => {
    if (!link) return;
    if (nativeShareAvailable) {
      await presentNativeShare('link');
      return;
    }
    try {
      await navigator.share({ title: t('chat.showPage.title'), url: link });
    } catch {
      // The native sheet may be dismissed without changing state.
    }
  };

  const requestExport = (kind: ShowExportKind) => {
    if (shareBusyRef.current) return;
    // The popover closes first so only the confirmation stands over the page, and it must not hand
    // focus back to its trigger once the confirmation holds it.
    exportConfirmRef.current = true;
    handleOpenChange(false);
    setExportConfirm(kind);
  };

  const closeExportConfirm = () => {
    exportConfirmRef.current = false;
    setExportConfirm(null);
  };

  const confirmExport = () => {
    const kind = exportConfirm;
    closeExportConfirm();
    if (kind) void presentNativeShare(kind);
  };

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button
          ref={triggerRef}
          type="button"
          variant="ghost"
          size="icon"
          className={cn(compact
            ? 'size-6 shrink-0 rounded-md text-muted hover:bg-wash hover:text-foreground'
            : 'size-7 shrink-0', anchorOnly && 'size-7 pointer-events-none opacity-0')}
          aria-label={t('chat.showPage.share')}
          {...(anchorOnly ? { tabIndex: -1, 'aria-hidden': true } : {})}
        >
          <Share2 className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="max-h-[var(--radix-popover-content-available-height)] w-[min(23rem,calc(100vw-1rem))] space-y-3 overflow-y-auto"
        data-window-owner-id={ownerWindowId}
        onCloseAutoFocus={(event) => {
          if (exportConfirmRef.current || anchorOnly) event.preventDefault();
        }}
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
            {canShareLink ? (
              <Button
                type="button"
                variant="outline"
                size="icon"
                className="size-8 shrink-0"
                disabled={!link || shareBusy !== null}
                onClick={shareLink}
                aria-label={t('chat.showPage.shareLink')}
              >
                {shareBusy === 'link'
                  ? <Loader2 className="size-3.5 animate-spin" />
                  : <Share2 className="size-3.5" />}
              </Button>
            ) : null}
          </div>
        ) : null}

        {showExport ? (
          <div className="border-t border-border pt-3">
            <div className="text-xs font-medium text-foreground">{t('chat.showPage.exportTitle')}</div>
            <div className="mt-2 grid grid-cols-2 gap-1.5">
              {([
                ['pdf', FileText, 'chat.showPage.sharePdf'],
                ['image', ImageIcon, 'chat.showPage.shareImage'],
              ] as const).map(([kind, Icon, label]) => (
                <Button
                  key={kind}
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={!exportAvailable || shareBusy !== null}
                  onClick={() => requestExport(kind)}
                >
                  {shareBusy === kind
                    ? <Loader2 className="size-3.5 animate-spin" />
                    : <Icon className="size-3.5" />}
                  {t(label)}
                </Button>
              ))}
            </div>
            {!exportAvailable ? (
              <p className="mt-2 text-xs leading-relaxed text-muted">{t('chat.showPage.shareNeedsUpdate')}</p>
            ) : null}
          </div>
        ) : null}

        {access?.can_publish_public ? (
          <div className="border-t border-border pt-3">
            <Suspense fallback={<div className="text-sm text-muted">{t('common.loading')}</div>}>
              <ShowPageSharingSettings
                active={open}
                canManage
                sessionId={sessionId}
                onApplied={handleShowAccessApplied}
                ownerWindowId={ownerWindowId}
              />
            </Suspense>
          </div>
        ) : null}

        {accessError ? (
          <p className="border-t border-border pt-3 text-[11px] leading-snug text-destructive-ink">
            {t('chat.showPage.accessLoadError')}
          </p>
        ) : null}

        {showSystemEntry ? (
          <ShowPageSystemEntryGuide
            mode={systemEntryMode}
            link={link}
            onOpenBrowser={openInBrowser}
            onCopyLink={copyLink}
          />
        ) : null}

        {payload && canPinToDock ? (
          <div className="border-t border-border pt-3">
            <div className="flex items-center gap-3">
              <span className="grid size-9 shrink-0 place-items-center rounded-md border border-edge bg-foreground/[0.03] text-cyan-ink shadow-elevation-0">
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
      <ConfirmDialog
        open={exportConfirm !== null}
        onOpenChange={(next) => {
          if (next) return;
          closeExportConfirm();
          // After the confirmation stops trapping focus, which it does once it has closed.
          if (!anchorOnly) window.requestAnimationFrame(() => triggerRef.current?.focus({ preventScroll: true }));
        }}
        title={t('chat.showPage.exportTitle')}
        description={t('chat.showPage.exportNotice')}
        confirmLabel={t('common.continue')}
        cancelLabel={t('common.cancel')}
        onConfirm={confirmExport}
        windowOwnerId={ownerWindowId}
      />
    </Popover>
  );
};
