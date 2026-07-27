// Per-row lifecycle actions for the 来源 list (frame 01r): an overflow menu that
// stays out of the way (hidden until row hover / focus on desktop) and exposes
// the contracted source mutations the list otherwise couldn't reach — rename
// (PATCH), re-discover (POST /test, hub sources only), reorder by one step, and
// delete (DELETE, with the only-supplier guard escalating to a forced delete).
// Presentation lives in SourceRow; this owns the actions + their dialogs so the
// row stays declarative.
//
// On phones the menu presents as a bottom sheet (design.pen M02 m02SheetA) with
// the row's identity in the header and a one-line rationale under each action,
// because a thumb menu has no hover to explain itself. 上移一位 / 下移一位 live
// here rather than as a visible button column: drag is the primary reordering
// gesture, and these exist so the same thing is reachable by keyboard and screen
// reader.
import * as React from 'react';
import { ArrowDown, ArrowUp, MoreHorizontal, Pencil, RefreshCw, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { ResponsiveMenu } from '@/components/ui/responsive-menu';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/lib/useIsMobile';
import { useToast } from '@/context/ToastContext';
import { modelsApi } from './modelsApi';
import { ACCENT_ICON, ACCENT_TILE, sourceVisual } from './vendorMeta';
import type { Source } from './types';

const MenuAction: React.FC<{
  Icon: React.ComponentType<{ className?: string }>;
  label: string;
  /** Shown in the mobile sheet only — the popover stays a compact list. */
  description?: string;
  onClick: () => void;
  disabled?: boolean;
  destructive?: boolean;
}> = ({ Icon, label, description, onClick, disabled, destructive }) => (
  <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    className={cn(
      // min-h-11 keeps each action a comfortable thumb target on phones.
      'flex min-h-11 w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px] transition-colors sm:min-h-0',
      description && 'items-start gap-3 rounded-none border-b border-border px-4 py-3.5 last:border-b-0',
      destructive ? 'text-destructive' : 'text-foreground',
      disabled ? 'opacity-40' : destructive ? 'hover:bg-destructive/[0.08]' : 'hover:bg-surface-2',
    )}
  >
    <Icon className={cn('size-4 shrink-0', description && 'mt-0.5 size-[18px]')} />
    {description ? (
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="text-[14px] font-semibold">{label}</span>
        <span className="text-[12px] leading-relaxed text-muted">{description}</span>
      </span>
    ) : (
      label
    )}
  </button>
);

export const SourceRowMenu: React.FC<{
  source: Source;
  priority: number;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onMove: (delta: -1 | 1) => void;
  /** Re-fetch sources + agents after any successful mutation. */
  onChanged: () => void;
}> = ({ source, priority, canMoveUp, canMoveDown, onMove, onChanged }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const isMobile = useIsMobile();
  const { Icon: SourceIcon, accent } = sourceVisual(source);

  const [menuOpen, setMenuOpen] = React.useState(false);
  const [renameOpen, setRenameOpen] = React.useState(false);
  const [renameValue, setRenameValue] = React.useState(source.display_name);
  const [renaming, setRenaming] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);
  // Set once the server refuses a plain delete (only-supplier guard); the
  // confirm then escalates to a forced delete instead of silently failing.
  const [forceMode, setForceMode] = React.useState(false);
  const [testing, setTesting] = React.useState(false);

  const aliveRef = React.useRef(true);
  React.useEffect(() => () => {
    aliveRef.current = false;
  }, []);

  // Re-discovery only applies to hub sources; native_cli subscriptions are
  // rejected server-side, so we don't offer the action for them.
  const canRediscover = source.supply_channel === 'hub';

  const openRename = () => {
    setMenuOpen(false);
    setRenameValue(source.display_name);
    setRenameOpen(true);
  };

  const submitRename = async () => {
    const name = renameValue.trim();
    if (!name || name === source.display_name) {
      setRenameOpen(false);
      return;
    }
    setRenaming(true);
    try {
      await modelsApi.patchSource(source.id, { display_name: name });
      if (!aliveRef.current) return;
      setRenameOpen(false);
      onChanged();
      showToast(t('settings.models.sourceActions.renamed') as string, 'success');
    } catch {
      if (aliveRef.current) showToast(t('settings.models.sourceActions.renameFailed') as string, 'error');
    } finally {
      if (aliveRef.current) setRenaming(false);
    }
  };

  const rediscover = async () => {
    setMenuOpen(false);
    if (testing) return;
    setTesting(true);
    try {
      const count = await modelsApi.testSource(source.id);
      if (!aliveRef.current) return;
      onChanged();
      showToast(t('settings.models.sourceActions.rediscovered', { count }) as string, 'success');
    } catch {
      if (aliveRef.current) showToast(t('settings.models.sourceActions.rediscoverFailed') as string, 'error');
    } finally {
      if (aliveRef.current) setTesting(false);
    }
  };

  const move = (delta: -1 | 1) => {
    setMenuOpen(false);
    onMove(delta);
  };

  const openDelete = () => {
    setMenuOpen(false);
    setForceMode(false);
    setDeleteOpen(true);
  };

  const confirmDelete = async () => {
    try {
      await modelsApi.deleteSource(source.id, forceMode);
      if (!aliveRef.current) return;
      setDeleteOpen(false);
      onChanged();
      showToast(t('settings.models.sourceActions.deleted') as string, 'success');
    } catch (err) {
      const code = (err as { code?: string } | null)?.code;
      // Only-supplier guard: escalate to a forced delete instead of failing.
      if (code === 'mode_switch_blocked' && !forceMode) {
        if (aliveRef.current) setForceMode(true);
        return;
      }
      if (aliveRef.current) {
        setDeleteOpen(false);
        showToast(t('settings.models.sourceActions.deleteFailed') as string, 'error');
      }
    }
  };

  // Descriptions only exist in the sheet; passing undefined on desktop is what
  // keeps the popover a compact label list.
  const hint = (key: string, opts?: Record<string, unknown>) =>
    isMobile ? (t(`settings.models.sourceActions.${key}`, opts) as string) : undefined;

  return (
    <>
      <ResponsiveMenu
        open={menuOpen}
        onOpenChange={setMenuOpen}
        sheetTitle={source.display_name}
        sheetTitleVisible={false}
        className="w-[200px]"
        sheetHeader={
          <div className="flex items-center gap-3 px-4 pb-3.5 pt-1">
            <span className={cn('flex size-10 shrink-0 items-center justify-center rounded-[10px]', ACCENT_TILE[accent])}>
              <SourceIcon className={cn('size-5', ACCENT_ICON[accent])} />
            </span>
            <span className="flex min-w-0 flex-col gap-0.5">
              <span className="truncate text-[15px] font-bold text-foreground">{source.display_name}</span>
              <span className="truncate text-[12px] text-muted">
                {[
                  t('settings.models.sourceActions.priorityLabel', { position: priority }),
                  t(`settings.models.state.${source.state.status}`),
                  t(`settings.models.billing.${source.billing}`),
                ].join(' · ')}
              </span>
            </span>
          </div>
        }
        trigger={
          <button
            type="button"
            aria-label={t('settings.models.sourceActions.more') as string}
            className={cn(
              // The button is a 44×44 touch target while the drawn box inside it
              // is the design's 36×36 (design.pen M01 m01More). Negative margins
              // cancel the extra 8px so the visual box lands exactly where a bare
              // 36px button would and the row height is unchanged — design size
              // and the touch-target floor both hold, with no reliance on
              // pseudo-element hit testing.
              'group/more -my-1 -mr-1 flex size-11 shrink-0 items-center justify-center text-muted sm:m-0 sm:size-8',
              // Quiet on desktop (revealed on row hover / focus / when open),
              // always reachable on touch where hover doesn't exist.
              menuOpen ? 'opacity-100' : 'opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus-visible:opacity-100',
            )}
          >
            <span className="flex size-9 items-center justify-center rounded-[10px] border border-border bg-surface/60 transition-colors group-hover/more:bg-surface-2 group-hover/more:text-foreground sm:size-8 sm:border-transparent sm:bg-transparent">
              {testing ? <RefreshCw className="size-4 animate-spin" /> : <MoreHorizontal className="size-4" />}
            </span>
          </button>
        }
      >
        <MenuAction
          Icon={Pencil}
          label={t('settings.models.sourceActions.rename') as string}
          description={hint('renameHint')}
          onClick={openRename}
        />
        {canRediscover && (
          <MenuAction
            Icon={RefreshCw}
            label={t('settings.models.sourceActions.rediscover') as string}
            description={hint('rediscoverHint')}
            onClick={() => void rediscover()}
          />
        )}
        <MenuAction
          Icon={ArrowUp}
          label={t('settings.models.sourceActions.moveUp') as string}
          description={hint(canMoveUp ? 'moveUpHint' : 'moveUpHintAtTop', { position: priority })}
          onClick={() => move(-1)}
          disabled={!canMoveUp}
        />
        <MenuAction
          Icon={ArrowDown}
          label={t('settings.models.sourceActions.moveDown') as string}
          description={hint(canMoveDown ? 'moveDownHint' : 'moveDownHintAtBottom')}
          onClick={() => move(1)}
          disabled={!canMoveDown}
        />
        <MenuAction
          Icon={Trash2}
          label={t('settings.models.sourceActions.delete') as string}
          description={hint('deleteHint')}
          onClick={openDelete}
          destructive
        />
      </ResponsiveMenu>

      <Dialog open={renameOpen} onOpenChange={(v) => !v && !renaming && setRenameOpen(false)}>
        <DialogContent className="max-w-[420px] gap-4">
          <DialogHeader>
            <DialogTitle>{t('settings.models.sourceActions.renameTitle')}</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <span className="text-[12px] font-semibold text-foreground">{t('settings.models.sourceActions.renameLabel')}</span>
            <Input
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              maxLength={64}
              autoFocus
              placeholder={t('settings.models.sourceActions.renamePlaceholder') as string}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void submitRename();
                }
              }}
              className="h-11 text-[14px]"
            />
          </div>
          <DialogFooter className="sm:justify-end">
            <Button variant="outline" size="sm" className="h-10 sm:h-9" onClick={() => setRenameOpen(false)} disabled={renaming}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="brand"
              size="sm"
              className="h-10 sm:h-9"
              onClick={() => void submitRename()}
              disabled={renaming || !renameValue.trim() || renameValue.trim() === source.display_name}
            >
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={(v) => {
          setDeleteOpen(v);
          if (!v) setForceMode(false);
        }}
        destructive
        title={t(forceMode ? 'settings.models.sourceActions.deleteForceTitle' : 'settings.models.sourceActions.deleteTitle')}
        description={t(forceMode ? 'settings.models.sourceActions.deleteForceBody' : 'settings.models.sourceActions.deleteBody', {
          name: source.display_name,
        })}
        confirmLabel={t(forceMode ? 'settings.models.sourceActions.deleteForceConfirm' : 'settings.models.sourceActions.deleteConfirm') as string}
        onConfirm={confirmDelete}
      />
    </>
  );
};
