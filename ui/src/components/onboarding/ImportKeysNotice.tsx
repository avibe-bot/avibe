// Setup's API-key import offer: one line under the assistant list saying how many
// keys already on this machine Model Hub can take over, with a way to look
// at them before anything happens.
//
// It is deliberately narrower than the settings migration: this entry is about
// keys only, while OAuth takeover and re-authentication stay in Settings. The
// sentence, the rows in the dialog, the batch submitted and the count left
// afterwards all read the single `isImportableKey` predicate.
//
// It stays visible after an import to say what happened and what is left, so a
// partial selection can be finished without hunting for the entry again.
import * as React from 'react';
import { KeyRound, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { InfoHint } from '@/components/ui/info-hint';
import { MigrationDialog } from '@/components/settings/models/MigrationDialog';
import { importableKeys, isImportableKey, scanMigrationWhenEnabled } from '@/components/settings/models/migrationScan';
import type { MigrationItem } from '@/components/settings/models/types';
import { useModelHubCapability } from '@/components/settings/models/useModelHubCapability';
import { isMigrationDismissed, writeMigrationDismissed } from '@/lib/modelHubMigrationDismiss';
import { useRouteSurfaceActive } from '@/lib/routeSurfaceActivity';

export const ImportKeysNotice: React.FC<{
  /** Bubble the applied count so the host step can refresh assistants/sources. */
  onApplied?: (applied: number) => void;
}> = ({ onApplied }) => {
  const { t } = useTranslation();
  const modelHubEnabled = useModelHubCapability();
  const routeSurfaceActive = useRouteSurfaceActive();

  const [candidates, setCandidates] = React.useState<MigrationItem[]>([]);
  const [imported, setImported] = React.useState(0);
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
    if (modelHubEnabled !== true) {
      setCandidates([]);
      return;
    }
    // Settings retains the wizard. Refresh its offer when it becomes visible
    // again, including migrations performed outside this notice's dialog.
    if (!routeSurfaceActive) return;
    let cancelled = false;
    scanMigrationWhenEnabled(true)
      .then((scan) => {
        if (cancelled || !aliveRef.current || scan === null) return;
        const keys = importableKeys(scan.items);
        setCandidates(keys);
        // Only the first scan consults the persisted dismissal. A rescan follows an
        // import the person just asked for, and hiding their own result because an
        // older batch was once dismissed would lose the outcome report.
        if (scanToken === 0) setDismissed(isMigrationDismissed(keys));
      })
      .catch(() => {
        // A failed scan must not keep advertising the previous rows: the dialog it
        // opens would rescan too, and land on its own error with nothing to show.
        if (!cancelled && aliveRef.current) setCandidates([]);
      });
    return () => {
      cancelled = true;
    };
  }, [modelHubEnabled, routeSurfaceActive, scanToken]);

  const remaining = candidates.length;
  if (modelHubEnabled !== true || dismissed) return null;
  // Nothing found and nothing done — there is no offer and no outcome to report.
  if (remaining === 0 && imported === 0) return null;

  const dismiss = () => {
    // An empty set would overwrite the remembered signature with nothing, so a batch
    // dismissed earlier would start nagging again. With nothing left to import there
    // is nothing to remember: this is just closing a receipt.
    if (remaining > 0) writeMigrationDismissed(candidates);
    setDismissed(true);
  };

  const message = imported === 0
    ? t('settings.models.importNotice.found', { count: remaining })
    : remaining > 0
      ? t('settings.models.importNotice.importedRemaining', { imported, count: remaining })
      : t('settings.models.importNotice.imported', { count: imported });

  return (
    <>
      <div className="onboarding-import-notice">
        <KeyRound size={16} className="onboarding-import-notice-icon" aria-hidden="true" />
        <span className="onboarding-import-notice-text">{message}</span>
        <div className="onboarding-import-notice-actions">
          {remaining > 0 && (
            // A light button, not a text link: this is the capsule's own action and
            // it has to read as one next to the primary CTA below it.
            <Button type="button" variant="secondary" size="sm" className="onboarding-import-notice-action"
              onClick={() => setDialogOpen(true)}>
              {t('settings.models.importNotice.review')}
            </Button>
          )}
          <InfoHint
            align="end"
            hover
            className="onboarding-import-notice-help"
            label={t('settings.models.importNotice.help') as string}
            trigger={t('settings.models.importNotice.help')}
            content={t('settings.models.importNotice.helpBody')}
            contentClassName="w-72"
          />
        </div>
        <button
          type="button"
          className="onboarding-import-notice-dismiss"
          aria-label={t('settings.models.importNotice.dismiss') as string}
          onClick={dismiss}
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>

      {dialogOpen && (
        <MigrationDialog
          open
          eligible={isImportableKey}
          onClose={() => setDialogOpen(false)}
          onApplied={(applied) => {
            setDialogOpen(false);
            setImported((total) => total + applied);
            // Rescan so "what is left" is the server's answer, not arithmetic on a
            // count this component happened to be holding.
            setScanToken((n) => n + 1);
            onApplied?.(applied);
          }}
        />
      )}
    </>
  );
};
