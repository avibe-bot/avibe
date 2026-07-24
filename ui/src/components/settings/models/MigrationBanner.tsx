// First-open-after-upgrade / setup-wizard migration trigger (spec §5-03). A
// self-contained, dismissible strip: it scans on mount and, when importable
// native configs exist, offers a one-click 导入中枢 that opens the shared
// MigrationDialog. Non-nagging — dismissal is persisted by a stable signature of
// the importable set (see modelHubMigrationDismiss), so it stays hidden until a
// genuinely new config appears. Self-gates to null when nothing is importable,
// the scan fails, or it was dismissed, so it's safe to drop anywhere.
import * as React from 'react';
import { ArrowDownToLine, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { isMigrationDismissed, writeMigrationDismissed } from '@/lib/modelHubMigrationDismiss';
import { MigrationDialog } from './MigrationDialog';
import { modelsApi } from './modelsApi';
import type { MigrationItem } from './types';

export const MigrationBanner: React.FC<{
  /** Bubble the applied count so a host page can refresh sources/agents. */
  onApplied?: (applied: number) => void;
}> = ({ onApplied }) => {
  const { t } = useTranslation();

  const [importable, setImportable] = React.useState<MigrationItem[]>([]);
  const [dialogOpen, setDialogOpen] = React.useState(false);
  const [dismissed, setDismissed] = React.useState(false);
  const [scanToken, setScanToken] = React.useState(0);
  const aliveRef = React.useRef(true);
  React.useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  React.useEffect(() => {
    let cancelled = false;
    modelsApi
      .scanMigration()
      .then((scan) => {
        if (cancelled || !aliveRef.current) return;
        // reauth rows need the interactive OAuth flow and can't be bulk-applied,
        // so they don't count as importable (they'd open a dead-end dialog).
        const items = scan.items.filter((i) => i.proposed_action !== 'reauth');
        setImportable(items);
        // Hidden while the current set is a subset of what was dismissed; a
        // genuinely new id/action resurfaces it.
        setDismissed(isMigrationDismissed(items));
      })
      .catch(() => {
        // A rescan (after apply, or a transient outage) that fails must not keep
        // advertising the previous importable rows — self-hide instead of leaving
        // a stale strip that reopens a dead migration dialog.
        if (!cancelled && aliveRef.current) setImportable([]);
      });
    return () => {
      cancelled = true;
    };
  }, [scanToken]);

  if (importable.length === 0 || dismissed) return null;

  const dismiss = () => {
    writeMigrationDismissed(importable);
    setDismissed(true);
  };

  return (
    <>
      <div className="flex items-center gap-3 rounded-xl border border-gold/40 bg-gold/[0.08] px-4 py-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-[10px] bg-gold/15 text-gold">
          <ArrowDownToLine className="size-[18px]" />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="text-[14px] font-semibold text-foreground">{t('settings.models.migrationBanner.title')}</span>
          <span className="text-[12px] leading-relaxed text-muted">
            {t('settings.models.migrationBanner.body', { count: importable.length })}
          </span>
        </div>
        <Button variant="outline" size="sm" className="shrink-0 border-gold/40 text-gold hover:bg-gold/10 hover:text-gold" onClick={() => setDialogOpen(true)}>
          <ArrowDownToLine className="size-3.5" />
          {t('settings.models.migrationBanner.import')}
        </Button>
        <button
          type="button"
          aria-label={t('settings.models.migrationBanner.dismiss') as string}
          onClick={dismiss}
          className="flex size-7 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-surface-2 hover:text-foreground"
        >
          <X className="size-4" />
        </button>
      </div>

      <MigrationDialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        onApplied={(applied) => {
          setDialogOpen(false);
          // Re-scan so the strip reflects what's left (imported rows drop out).
          setScanToken((n) => n + 1);
          onApplied?.(applied);
        }}
      />
    </>
  );
};
