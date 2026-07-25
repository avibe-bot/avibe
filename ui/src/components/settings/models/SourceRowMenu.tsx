// Per-row lifecycle actions for the 来源 list (frame 01r): an overflow menu that
// stays out of the way (hidden until row hover / focus on desktop) and exposes
// the contracted source mutations the list otherwise couldn't reach — rename
// (PATCH), re-discover (POST /test, hub sources only), and delete (DELETE, with
// the only-supplier guard escalating to a forced delete). Presentation lives in
// SourceRow; this owns the actions + their dialogs so the row stays declarative.
import * as React from 'react';
import { MoreHorizontal, Pencil, RefreshCw, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { useToast } from '@/context/ToastContext';
import { modelsApi } from './modelsApi';
import type { Source } from './types';

const MenuAction: React.FC<{
  Icon: React.ComponentType<{ className?: string }>;
  label: string;
  onClick: () => void;
  destructive?: boolean;
}> = ({ Icon, label, onClick, destructive }) => (
  <button
    type="button"
    onClick={onClick}
    className={cn(
      'flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px] transition-colors',
      destructive ? 'text-destructive hover:bg-destructive/[0.08]' : 'text-foreground hover:bg-surface-2',
    )}
  >
    <Icon className="size-4 shrink-0" />
    {label}
  </button>
);

export const SourceRowMenu: React.FC<{
  source: Source;
  /** Re-fetch sources + agents after any successful mutation. */
  onChanged: () => void;
}> = ({ source, onChanged }) => {
  const { t } = useTranslation();
  const { showToast } = useToast();

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

  return (
    <>
      <Popover open={menuOpen} onOpenChange={setMenuOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            aria-label={t('settings.models.sourceActions.more') as string}
            className={cn(
              'flex size-8 shrink-0 items-center justify-center rounded-md text-muted transition-all hover:bg-surface-2 hover:text-foreground',
              // Quiet on desktop (revealed on row hover / focus / when open),
              // always reachable on touch where hover doesn't exist.
              menuOpen ? 'opacity-100' : 'opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus-visible:opacity-100',
            )}
          >
            {testing ? <RefreshCw className="size-4 animate-spin" /> : <MoreHorizontal className="size-4" />}
          </button>
        </PopoverTrigger>
        <PopoverContent align="end" sideOffset={6} className="w-[200px] border-border bg-card p-1.5 text-foreground shadow-lg">
          <MenuAction Icon={Pencil} label={t('settings.models.sourceActions.rename') as string} onClick={openRename} />
          {canRediscover && (
            <MenuAction Icon={RefreshCw} label={t('settings.models.sourceActions.rediscover') as string} onClick={() => void rediscover()} />
          )}
          <MenuAction Icon={Trash2} label={t('settings.models.sourceActions.delete') as string} onClick={openDelete} destructive />
        </PopoverContent>
      </Popover>

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
            <Button variant="outline" size="sm" onClick={() => setRenameOpen(false)} disabled={renaming}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="brand"
              size="sm"
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
