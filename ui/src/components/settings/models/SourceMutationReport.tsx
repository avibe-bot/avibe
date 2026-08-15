import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Loader2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { GuardImpact } from './GuardImpact';
import { SOURCE_MUTATION_REPORT_PROJECTIONS } from './mutationSettlement';
import type { SourceMutationReportState } from './useSourceMutationReport';

export const SourceMutationReport: React.FC<{
  report: SourceMutationReportState | null;
  onComplete: () => void;
  onDismiss: () => void;
}> = ({ report, onComplete, onDismiss }) => {
  const { t } = useTranslation();
  const impact = report?.commit.impact ?? null;
  const action = report?.commit.action ?? 'edit';
  const copyKind = action === 'edit' ? 'edit' : 'remove';
  const titleKey = impact
    ? `settings.models.sourceDetail.${copyKind}.impact.title`
    : `settings.models.sourceDetail.${copyKind}.settlement.title`;
  const detailKey = impact
    ? `settings.models.sourceDetail.${copyKind}.impact.detail`
    : 'settings.models.sourceDetail.impact.refreshFail';
  const actionKey = report?.landingFailed
    ? 'settings.models.sourceDetail.retry'
    : `settings.models.sourceDetail.${copyKind}.impact.done`;

  return (
    <DialogPrimitive.Root
      open={report !== null}
      onOpenChange={(open) => { if (!open && report?.landingFailed && !report.busy) onDismiss(); }}
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="model-hub-guard-overlay fixed inset-0 z-50" />
        <DialogPrimitive.Content
          data-source-mutation-report={action}
          data-report-projections={Object.keys(SOURCE_MUTATION_REPORT_PROJECTIONS).join(' ')}
          className="model-hub-guard-dialog fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-y-auto border border-border-strong bg-surface outline-none"
          onEscapeKeyDown={(event) => { if (!report?.landingFailed || report.busy) event.preventDefault(); }}
          onPointerDownOutside={(event) => event.preventDefault()}
        >
          <header className="model-hub-guard-head">
            <div className="flex items-center justify-between gap-3">
              <DialogPrimitive.Title className="model-hub-guard-title text-foreground">
                {t(titleKey)}
              </DialogPrimitive.Title>
              <DialogPrimitive.Close asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="model-hub-guard-close"
                  disabled={!report?.landingFailed || report.busy}
                  onClick={onDismiss}
                  aria-label={t(report?.landingFailed ? 'settings.models.sourceDetail.dismissUnverified' : actionKey)}
                  title={t(report?.landingFailed ? 'settings.models.sourceDetail.dismissUnverified' : actionKey)}
                >
                  <X aria-hidden />
                </Button>
              </DialogPrimitive.Close>
            </div>
            <DialogPrimitive.Description className="model-hub-guard-subtitle">
              {t(detailKey)}
            </DialogPrimitive.Description>
          </header>
          {impact && (
            <div className="model-hub-guard-body">
              <GuardImpact hops={impact.hops} gaps={impact.gaps} committed />
              {report?.landingFailed && (
                <p data-manage-impact-failure className="text-[11.5px] text-destructive-ink">
                  {t('settings.models.sourceDetail.impact.refreshFail')}
                </p>
              )}
            </div>
          )}
          <footer className="model-hub-guard-foot">
            {report?.landingFailed && (
              <Button
                type="button"
                variant="outline"
                className="model-hub-guard-action"
                onClick={onDismiss}
                disabled={report.busy}
              >
                {t('settings.models.sourceDetail.dismissUnverified')}
              </Button>
            )}
            <Button
              type="button"
              className="model-hub-guard-action"
              onClick={onComplete}
              disabled={!report || report.busy}
            >
              {report?.busy && <Loader2 className="animate-spin" />}
              {t(actionKey)}
            </Button>
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};
