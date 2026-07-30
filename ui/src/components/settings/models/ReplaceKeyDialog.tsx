// 更换 API Key — the credential-replacement journey (api.md 「Credential
// replacement and reauth」, AC-3).
//
// Three things make it more than a form:
//
//  1. The write is ATOMIC and re-discovers on commit, landing the source on
//     `standby` exactly like the recovery test does. So the dialog never claims
//     the source is 使用中 afterwards — it reports what the server reported.
//  2. The supply guard can REFUSE it. An elective replacement is evaluated against
//     the discovered replacement set before the write, so 「the new key can't serve
//     what the old one did」 arrives as `source_last_supplier` with the stranded
//     pairs named. That escalates in place: same typed key, one more confirm, then
//     `force: true`. A dialog stacked on a dialog would ask the user to re-read
//     the same field.
//  3. A SUCCESSFUL replacement can still strand something — the server refuses
//     only an elective write, so a recovering one commits and reports
//     `interrupted_pairs` anyway. That report holds the dialog open.
import * as React from 'react';
import { CheckCircle2, KeyRound, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { apiFailure, modelsApi } from './modelsApi';
import { FieldLabel, IconField } from './dialogFields';
import { repairOutcome, repairSettles, type RepairOutcome } from './repair';
import { SupplyGapNote } from './SupplyGapNote';
import type { Source, SupplyGap } from './types';

type Phase = 'edit' | 'confirm' | 'submitting' | 'done' | 'error';

export const ReplaceKeyDialog: React.FC<{
  /** The row that opened this. Null closes it. */
  source: Source | null;
  onClose: () => void;
  /** A commit happened — re-fetch sources + agents. */
  onReplaced: () => void;
}> = ({ source, onClose, onReplaced }) => {
  const { t } = useTranslation();
  const open = source !== null;
  const [key, setKey] = React.useState('');
  const [phase, setPhase] = React.useState<Phase>('edit');
  const [gaps, setGaps] = React.useState<SupplyGap[]>([]);
  const [outcome, setOutcome] = React.useState<RepairOutcome | null>(null);
  const closeTimer = React.useRef<number | null>(null);
  // Same guard as AddApiKeyDialog's: a commit resolving after the dialog closed or
  // reopened on another row must not write its result into the new state.
  const submitSeq = React.useRef(0);

  React.useEffect(() => {
    submitSeq.current += 1;
    if (open) {
      setKey('');
      setPhase('edit');
      setGaps([]);
      setOutcome(null);
    }
    return () => {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    };
  }, [open, source?.id]);

  const submit = async (force: boolean) => {
    if (!source) return;
    const value = key.trim();
    if (!value) return;
    const seq = submitSeq.current;
    setPhase('submitting');
    try {
      const tail = await modelsApi.replaceCredential(source.id, force ? { key: value, force: true } : { key: value });
      if (submitSeq.current !== seq) return;
      const verdict = repairOutcome(tail);
      setOutcome(verdict);
      setPhase('done');
      // Before the auto-close decision, unconditionally: the credential is
      // committed whatever the report says, so the list is stale from here on.
      onReplaced();
      if (repairSettles(verdict)) closeTimer.current = window.setTimeout(onClose, 1500);
    } catch (e) {
      if (submitSeq.current !== seq) return;
      const failure = apiFailure(e);
      // The guard, in place: keep the typed key, name what would be stranded, and
      // let the same button commit with `force`.
      if (failure?.code === 'source_last_supplier' && !force) {
        setGaps(failure.wouldInterrupt);
        setPhase('confirm');
        return;
      }
      setPhase('error');
    }
  };

  const busy = phase === 'submitting';
  const forcing = phase === 'confirm';
  const settled = phase === 'done';

  return (
    // Closing mid-commit would drop a credential the server has already written.
    <Dialog open={open} onOpenChange={(v) => !v && !busy && onClose()}>
      <DialogContent className="max-w-[520px] gap-5">
        <DialogHeader>
          <DialogTitle className="text-[18px] font-bold">
            {forcing
              ? t('settings.models.repair.forceTitle')
              : t('settings.models.repair.replaceTitle', { name: source?.display_name ?? '' })}
          </DialogTitle>
          <DialogDescription>
            {forcing
              ? t('settings.models.repair.gapsTitle')
              : t('settings.models.repair.replaceBody')}
          </DialogDescription>
        </DialogHeader>

        {forcing ? (
          <SupplyGapNote gaps={gaps} />
        ) : (
          <div className="flex flex-col gap-2">
            <FieldLabel mono>{t('settings.models.repair.replaceLabel')}</FieldLabel>
            <IconField icon={KeyRound}>
              <Input
                type="password"
                autoComplete="off"
                spellCheck={false}
                autoFocus
                placeholder="sk-…"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                disabled={busy || settled}
                className="h-11 pl-9 font-mono text-[14px]"
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !busy && !settled) {
                    e.preventDefault();
                    void submit(false);
                  }
                }}
              />
            </IconField>
          </div>
        )}

        {settled && outcome ? (
          outcome.kind === 'gaps' ? (
            <div className="flex flex-col gap-2 rounded-lg border border-gold/40 bg-gold/[0.08] px-3.5 py-3">
              <span className="text-[12.5px] font-semibold leading-relaxed text-gold">
                {t('settings.models.repair.gapsDone')}
              </span>
              <SupplyGapNote gaps={outcome.gaps} />
            </div>
          ) : (
            <div className="flex items-center gap-2 rounded-lg border border-mint/30 bg-mint-soft/50 px-4 py-3 text-[13px] font-medium text-mint">
              <CheckCircle2 className="size-4 shrink-0" />
              <span>{t(`settings.models.repair.${outcome.kind}`)}</span>
            </div>
          )
        ) : null}

        {phase === 'error' && (
          <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/[0.08] px-4 py-3 text-[13px] text-destructive">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" />
            {/* One sentence, no code: every failure this route can report other
                than the guard above is 「that key didn't work」, and the machine
                code is in the network log for whoever needs it. */}
            <span>{t('settings.models.repair.replaceFailed')}</span>
          </div>
        )}

        <DialogFooter>
          <div className="flex items-center gap-2">
            {/* 关闭 once the credential is committed: 「取消」 on a completed write
                reads as an undo this route does not have. */}
            <Button variant="outline" size="sm" className="h-10 sm:h-9" onClick={onClose} disabled={busy}>
              {t(settled ? 'common.close' : 'common.cancel')}
            </Button>
            {!settled && (
              <Button
                variant={forcing ? 'destructive' : 'brand'}
                size="sm"
                className="h-10 sm:h-9"
                onClick={() => void submit(forcing)}
                disabled={busy || !key.trim()}
              >
                {busy
                  ? t('settings.models.repair.replacing')
                  : forcing
                    ? t('settings.models.repair.forceConfirm')
                    : t('settings.models.repair.replaceSubmit')}
              </Button>
            )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default ReplaceKeyDialog;
