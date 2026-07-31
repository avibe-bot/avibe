// Per-row lifecycle actions for the 来源 list (design.pen 「产品改造 V6 01」): an
// overflow menu that stays out of the way (hidden until row hover / focus on
// desktop) and exposes the contracted source mutations the list otherwise
// couldn't reach — rename (PATCH) and delete (DELETE, with the only-supplier guard escalating to a forced
// delete). Presentation lives in SourceRow; this owns the actions + their
// dialogs so the row stays declarative.
//
// It also owns the ENTRY to the repair journeys: an inline, always-visible button
// on a stopped row (§4.5's 「one tap to fix it」) plus the elective credential
// entries in the menu. The journeys themselves are raised to the page — see
// RepairJourney — because completing one refetches the list this row lives in.
//
// No reorder actions: in V6 an order belongs to an Agent, not to the source
// inventory, so 上移/下移 moved into the per-Agent 来源顺序 drawer (which keeps a
// keyboard/screen-reader path of its own alongside drag).
//
// On phones the menu presents as a bottom sheet (design.pen M02 m02SheetA) with
// the row's identity in the header and a one-line rationale under each action,
// because a thumb menu has no hover to explain itself.
import * as React from 'react';
import { KeyRound, LogIn, MoreHorizontal, Pencil, RefreshCw, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { ResponsiveMenu } from '@/components/ui/responsive-menu';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/lib/useIsMobile';
import { useToast } from '@/context/ToastContext';
import { apiFailure, modelsApi } from './modelsApi';
import { canReauth, canReplaceKey, reauthCost, repairAction, REPAIR_LABEL_KEY, type RepairKind } from './repair';
import { SupplyGapNote } from './SupplyGapNote';
import { ACCENT_ICON, ACCENT_TILE, sourceVisual } from './vendorMeta';
import type { Source, SupplyGap } from './types';

/** The two remedies that need a dialog of their own, so the page hosts them. */
export type RaisedRepair = Exclude<RepairKind, 'retest'>;

const REPAIR_ICON: Record<RepairKind, React.ComponentType<{ className?: string }>> = {
  reauth: LogIn,
  replace_key: KeyRound,
  retest: RefreshCw,
};

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
  /** Re-fetch sources + agents after a rename or delete. */
  onChanged: () => void;
  onRefresh: (source: Source) => void;
  refreshing: boolean;
  refreshDisabled: boolean;
  /**
   * Raise a remedy that needs a dialog + a server flow to the page, which
   * outlives this row: a re-auth replaces the source's models and a key
   * replacement re-discovers them, so both trigger the refetch that unmounts
   * whatever hosted the dialog. `retest` delegates to the page-owned refresh
   * operation so the row action and the inventory button share one lifetime.
   */
  onRepair?: (source: Source, kind: RaisedRepair) => void;
}> = ({ source, onChanged, onRefresh, refreshing, refreshDisabled, onRepair }) => {
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
  // What that refusal said it would strand. Kept as the server sent it: the
  // confirm has to name the Agents, and re-deriving them from the loaded list
  // would answer a question the guard already answered.
  const [gaps, setGaps] = React.useState<SupplyGap[]>([]);

  // Re-armed on mount, not only cleared on unmount: a cleanup-only guard is
  // one-way, so StrictMode's mount → cleanup → mount would leave every 测试
  // result and delete outcome silently discarded on a live menu.
  const aliveRef = React.useRef(true);
  React.useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

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

  const openDelete = () => {
    setMenuOpen(false);
    setForceMode(false);
    setGaps([]);
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
      const failure = apiFailure(err);
      // The supply guard: `source_last_supplier` is the code DELETE actually
      // sends (`mode_switch_blocked` belongs to the mode route, so the old test
      // never matched and every guarded delete failed with a generic toast).
      // Escalate to `?force=true` AND show what the server said it would strand —
      // that list is the reason a second confirm exists.
      if (failure?.code === 'source_last_supplier' && !forceMode) {
        if (aliveRef.current) {
          setGaps(failure.wouldInterrupt);
          setForceMode(true);
        }
        return;
      }
      if (aliveRef.current) {
        setDeleteOpen(false);
        // A failed DELETE is not necessarily read-only. The supply guard is the
        // one refusal that provably
        // wrote nothing — `delete_source` raises it off a CLONED config, before
        // `_commit_synced` — and it left through the branch above. Everything that
        // reaches here got past that commit or died inside it: a response lost on
        // the way back arrives as `bad_response`, invented by this client about a
        // delete that may well have happened, and `source_not_found` means the row
        // is already gone. Deliberately not gated on `serverNamed`: that says
        // whether the ROUTE named the failure, never whether the server wrote —
        // `oauth_cancel` is the standing proof those are different questions. The
        // toast reports the attempt, the row reports the state.
        onChanged();
        showToast(t('settings.models.sourceActions.deleteFailed') as string, 'error');
      }
    }
  };

  // ── Repair ───────────────────────────────────────────────────────────────
  // §4.5: a `needs_action` row 「carries a detail_key naming the cause, so the row
  // can offer ONE TAP to fix it … instead of a dead-end error string」. `repair.ts`
  // owns which tap that is; this only routes it.
  const blockedRemedy = repairAction(source);
  const runRepair = (kind: RepairKind) => {
    setMenuOpen(false);
    if (kind === 'retest') {
      onRefresh(source);
      return;
    }
    onRepair?.(source, kind);
  };
  // A raised remedy needs the page's host; without it the row would open nothing.
  const offer = (kind: RepairKind | null): kind is RepairKind =>
    kind !== null && (kind === 'retest' || Boolean(onRepair));
  const inlineRemedy = offer(blockedRemedy) ? blockedRemedy : null;

  // Descriptions only exist in the sheet; passing undefined on desktop is what
  // keeps the popover a compact label list.
  const hint = (key: string, opts?: Record<string, unknown>) =>
    isMobile ? (t(`settings.models.sourceActions.${key}`, opts) as string) : undefined;

  const RemedyIcon = inlineRemedy ? REPAIR_ICON[inlineRemedy] : null;

  return (
    <>
      {inlineRemedy && RemedyIcon ? (
        // Always visible, unlike the ⋯ trigger next to it: the whole failure of a
        // status-only row is that the remedy hides behind a hover the blocked
        // state gives no reason to try. Geometry from design.pen V6 03's row-level
        // button (`d6Btn 启用`): outline, radius 8, 6/12 padding, 11.5/600.
        <Button
          variant="outline"
          size="xs"
          className="h-7 shrink-0 rounded-md px-3 text-[11.5px] font-semibold"
          // Composed rather than a per-kind string: the label already says what
          // happens, and every row on the page repeats it.
          aria-label={`${t(REPAIR_LABEL_KEY[inlineRemedy])} · ${source.display_name}`}
          disabled={refreshDisabled}
          onClick={() => runRepair(inlineRemedy)}
        >
          {refreshing && inlineRemedy === 'retest' ? (
            <RefreshCw className="size-3 animate-spin" />
          ) : (
            <RemedyIcon className="size-3" />
          )}
          {t(REPAIR_LABEL_KEY[inlineRemedy])}
        </Button>
      ) : null}

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
              <MoreHorizontal className="size-4" />
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
        {/* Elective credential maintenance — available on a HEALTHY source too
            (api.md gives both routes an elective form, guard included), which is
            why these read the per-route predicates rather than `repairAction`.
            Suppressed for the remedy the inline button already offers, so a
            blocked row doesn't show the same action twice. */}
        {onRepair && canReauth(source) && inlineRemedy !== 'reauth' && (
          <MenuAction
            Icon={LogIn}
            label={t('settings.models.sourceActions.reauth') as string}
            // Only the native channel pays at 「开始」, so only it has something to
            // warn about before the confirm does. On a hub source the same line
            // would promise a loss that does not happen — and the confirm one tap
            // later states that channel's real cost. Deleting the sentence beats
            // qualifying it.
            description={reauthCost(source) === 'immediate' ? hint('reauthHint') : undefined}
            onClick={() => runRepair('reauth')}
          />
        )}
        {onRepair && canReplaceKey(source) && inlineRemedy !== 'replace_key' && (
          <MenuAction
            Icon={KeyRound}
            label={t('settings.models.sourceActions.replaceKey') as string}
            description={hint('replaceKeyHint')}
            onClick={() => runRepair('replace_key')}
          />
        )}
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
          if (!v) {
            setForceMode(false);
            setGaps([]);
          }
        }}
        destructive
        title={t(forceMode ? 'settings.models.sourceActions.deleteForceTitle' : 'settings.models.sourceActions.deleteTitle')}
        description={t(forceMode ? 'settings.models.sourceActions.deleteForceBody' : 'settings.models.sourceActions.deleteBody', {
          name: source.display_name,
        })}
        confirmLabel={t(forceMode ? 'settings.models.sourceActions.deleteForceConfirm' : 'settings.models.sourceActions.deleteConfirm') as string}
        onConfirm={confirmDelete}
      >
        {/* No title: `deleteForceBody` above already states the consequence, and a
            second sentence saying it again is the line worth deleting. */}
        {forceMode ? <SupplyGapNote gaps={gaps} /> : null}
      </ConfirmDialog>
    </>
  );
};
