// A connected card's own frame: change the key behind it, or remove it.
//
// Setup's add dialog only ever creates. Once a key is on the stage — and especially
// once its provider has refused it — the fix belongs to that key, not to a second one
// typed beside it. So the card opens this: the card's own status, a field for the
// replacement, and Remove.
//
// Both writes are the shipped ones. Replacing a key lists the provider's models with
// the new one before it commits, and keeps the old key when that fails, which is the
// same free check the card reports — so a replacement that lands is a key that passed,
// and one that is refused leaves the card exactly as it was. Removing goes through
// the route guard like Settings does, agreeing up front to the route hops the key
// supplied and to nothing else: stopping an Agent is never agreed to on the
// person's behalf, and a guard that names one ends as a failure here.
import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { CircleX, LoaderCircle, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { DialogOverlay } from '@/components/ui/dialog';
import { ApiKeyField } from '@/components/settings/models/ApiKeySourceForm';
import { confirmGuardPlan, sendAgreed } from '@/components/settings/models/guardedWrite';
import { apiFailure, modelsApi } from '@/components/settings/models/modelsApi';
import type { Source } from '@/components/settings/models/types';

import { checkLine, sourceCheckFailure, suppliedModelCount, type SourceCheck } from './providerStage';

type Phase =
  | { kind: 'idle' }
  | { kind: 'saving' }
  | { kind: 'confirmRemove' }
  | { kind: 'removing' }
  | { kind: 'failed'; message: string };

export const SourceKeyDialog: React.FC<{
  source: Source;
  /** The brand the card wears, so the frame names what the card names. */
  label: string;
  /** The card's check, so the frame opens on the card's own answer. */
  check: SourceCheck | undefined;
  /** Whether the screen still admits a write. See `AddSourceDialog`. */
  writable: boolean;
  /** A replacement landed: the key behind it passed the check this reports. */
  onReplaced: (source: Source, check: SourceCheck) => Promise<void>;
  /** The source is gone, or a write's outcome is unknown: re-read the inventory. */
  onChanged: () => Promise<void>;
  /** Ask the provider about the current key again. */
  onRecheck: () => void;
  onClose: () => void;
}> = ({ source, label, check, writable, onReplaced, onChanged, onRecheck, onClose }) => {
  const { t } = useTranslation();
  const [key, setKey] = React.useState('');
  const [revealed, setRevealed] = React.useState(false);
  const [phase, setPhase] = React.useState<Phase>({ kind: 'idle' });
  const apiKey = source.kind === 'api_key';
  const busy = phase.kind === 'saving' || phase.kind === 'removing';

  const mask = source.masked_credential?.trim() || null;
  const line = checkLine(t, check);
  const status = mask && line
    ? t('onboarding.providers.editStatus', { key: mask, check: line })
    : line ?? mask ?? '';

  const save = async () => {
    const value = key.trim();
    if (!apiKey || !value || busy || !writable) return;
    setPhase({ kind: 'saving' });
    try {
      const replaced = await sendAgreed(true, null, (plan) => modelsApi.replaceCredential(
        source.id,
        plan ? { key: value, ...confirmGuardPlan(plan) } : { key: value },
      ));
      await onReplaced(replaced.source, { kind: 'connected', models: suppliedModelCount(replaced.source) });
      onClose();
    } catch (error) {
      const failure = apiFailure(error);
      const verdict = sourceCheckFailure(failure, source);
      setPhase({
        kind: 'failed',
        message: verdict.kind !== 'rejected'
          ? t('onboarding.providers.editSaveFailed')
          : verdict.status === null
            ? t('onboarding.providers.editRejected')
            : t('onboarding.providers.editRejectedStatus', { status: verdict.status }),
      });
      // A refusal the server named rolled back; anything else may have landed.
      if (!failure?.serverNamed) await onChanged().catch(() => undefined);
    }
  };

  const remove = async () => {
    if (busy || !writable) return;
    if (phase.kind !== 'confirmRemove') {
      setPhase({ kind: 'confirmRemove' });
      return;
    }
    setPhase({ kind: 'removing' });
    try {
      await sendAgreed(true, null, (plan) => modelsApi.deleteSource(
        source.id,
        plan ? confirmGuardPlan(plan) : undefined,
      ));
      await onChanged().catch(() => undefined);
      onClose();
    } catch (error) {
      if (apiFailure(error)?.code === 'source_not_found') {
        await onChanged().catch(() => undefined);
        onClose();
        return;
      }
      setPhase({ kind: 'failed', message: t('onboarding.providers.editRemoveFailed') });
    }
  };

  const close = () => {
    if (!busy) onClose();
  };

  const progress = phase.kind === 'saving'
    ? t('onboarding.providers.editSaving')
    : phase.kind === 'removing'
      ? t('onboarding.providers.editRemoving')
      : null;

  return (
    <DialogPrimitive.Root open onOpenChange={(next) => !next && close()}>
      <DialogPrimitive.Portal>
        <DialogOverlay />
        <DialogPrimitive.Content
          className="setup-add-dialog fixed left-1/2 top-1/2 z-50 flex -translate-x-1/2 -translate-y-1/2 flex-col outline-none"
          data-size="compact"
          onEscapeKeyDown={(event) => { if (busy) event.preventDefault(); }}
          onPointerDownOutside={(event) => { if (busy) event.preventDefault(); }}
        >
          <header className="setup-add-head">
            <div className="flex items-start justify-between gap-3">
              <DialogPrimitive.Title className="setup-add-title">
                {t(apiKey ? 'onboarding.providers.editKeyTitleNamed' : 'onboarding.providers.editSubscriptionTitleNamed', { name: label })}
              </DialogPrimitive.Title>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="setup-add-close size-7 shrink-0"
                aria-label={t('common.close')}
                disabled={busy}
                onClick={close}
              >
                <X className="size-[15px]" />
              </Button>
            </div>
            <DialogPrimitive.Description
              className="setup-add-subtitle"
              {...(check?.kind === 'rejected' ? { 'data-tone': 'error' } : {})}
            >
              {status}
            </DialogPrimitive.Description>
            {check?.kind === 'failed' && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="self-start"
                disabled={busy}
                onClick={() => { onRecheck(); onClose(); }}
              >
                {t('onboarding.providers.editRecheck')}
              </Button>
            )}
          </header>

          <div className="setup-add-body">
            {apiKey
              ? (
                <>
                  <ApiKeyField
                    value={key}
                    revealed={revealed}
                    disabled={busy}
                    autoFocus
                    label={t('onboarding.providers.editKeyLabel')}
                    onChange={(value) => {
                      setKey(value);
                      setPhase((current) => (current.kind === 'failed' ? { kind: 'idle' } : current));
                    }}
                    onToggleReveal={() => setRevealed((value) => !value)}
                    onEnter={() => void save()}
                  />
                  <p className="setup-add-note">{t('onboarding.providers.editKeyNote', { name: label })}</p>
                </>
              )
              : <p className="setup-add-note">{t('onboarding.providers.editSubscriptionNote')}</p>}
          </div>

          <footer className="setup-add-foot">
            <span className="setup-add-status" role="status" aria-live="polite">
              {progress && (
                <>
                  <LoaderCircle className="setup-add-status-icon motion-safe:animate-spin" aria-hidden="true" />
                  {progress}
                </>
              )}
              {phase.kind === 'confirmRemove' && t('onboarding.providers.editRemoveConfirm')}
              {phase.kind === 'failed' && (
                <>
                  <CircleX className="setup-add-status-icon setup-add-status-icon--error" aria-hidden="true" />
                  <span className="setup-add-status-error">{phase.message}</span>
                </>
              )}
            </span>
            <Button
              type="button"
              variant={phase.kind === 'confirmRemove' ? 'destructive' : 'outline'}
              size="sm"
              disabled={busy || !writable}
              onClick={() => void remove()}
            >
              {t('onboarding.providers.editRemove')}
            </Button>
            {apiKey && (
              <Button
                type="button"
                variant="brand"
                size="sm"
                disabled={busy || !writable || !key.trim()}
                onClick={() => void save()}
              >
                {t('onboarding.providers.editSave')}
              </Button>
            )}
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};
