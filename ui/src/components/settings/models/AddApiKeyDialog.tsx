import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import {
  CheckCircle2,
  CircleX,
  Eye,
  EyeOff,
  Info,
  LoaderCircle,
  PlugZap,
  TriangleAlert,
  X,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { Field } from './dialogFields';
import {
  classifyObservation,
  isAbortError,
  PROTOCOL_COPY_KEYS,
  protocolOrderWithHint,
  type AddApiKeyFailure,
  type AddApiKeyOrigin,
} from './addApiKeyState';
import { apiFailure, modelsApi, type SourceCreated } from './modelsApi';
import { reconcileUnknownWrite } from './reconcileUnknownWrite';
import { serverText } from './serverCopy';
import {
  SOURCE_DISPLAY_NAME_MAX_LENGTH,
  SOURCE_PROTOCOLS,
  type ApiKeySourceCreate,
  type SourceObservation,
  type SourceProtocol,
} from './types';

type Phase =
  | { kind: 'form'; report: SourceObservation | null }
  | { kind: 'working'; origin: AddApiKeyOrigin; stage: 'observe' | 'persist' }
  | { kind: 'failure'; origin: AddApiKeyOrigin; cause: AddApiKeyFailure }
  | { kind: 'undetermined'; origin: AddApiKeyOrigin; observation: SourceObservation; hint: SourceProtocol | null }
  | { kind: 'inventory'; origin: AddApiKeyOrigin; observation: SourceObservation }
  | { kind: 'persist_failure'; messageKey: string | null; protocolOrder: SourceProtocol[] | undefined }
  | { kind: 'save_unconfirmed'; protocolOrder: SourceProtocol[] | undefined };

const INITIAL_PHASE: Phase = { kind: 'form', report: null };

const sourceClientNonce = (): string => {
  const uuid = globalThis.crypto.randomUUID?.();
  if (uuid) return `scn_${uuid.replaceAll('-', '').toLowerCase()}`;
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return `scn_${Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')}`;
};

const failureCopy = (cause: AddApiKeyFailure): string => {
  switch (cause) {
    case 'auth': return 'settings.models.addKey.fail.auth';
    case 'network': return 'settings.models.addKey.fail.network';
    case 'engineDown': return 'settings.models.addKey.fail.engineDown';
    case 'unclassified': return 'settings.models.addKey.fail.unclassified';
  }
};

export const AddApiKeyDialog: React.FC<{
  open: boolean;
  onClose: () => void;
  onAdded: (created: SourceCreated) => void;
}> = ({ open, onClose, onAdded }) => {
  const { t } = useTranslation();
  const [displayName, setDisplayName] = React.useState('');
  const [baseUrl, setBaseUrl] = React.useState('');
  const [apiKey, setApiKey] = React.useState('');
  const [revealed, setRevealed] = React.useState(false);
  const [phase, setPhase] = React.useState<Phase>(INITIAL_PHASE);
  const attempt = React.useRef(0);
  const clientNonce = React.useRef(sourceClientNonce());
  const observationAbort = React.useRef<AbortController | null>(null);
  const onAddedRef = React.useRef(onAdded);
  const onCloseRef = React.useRef(onClose);

  React.useEffect(() => {
    onAddedRef.current = onAdded;
    onCloseRef.current = onClose;
  }, [onAdded, onClose]);

  React.useEffect(() => {
    attempt.current += 1;
    observationAbort.current?.abort();
    observationAbort.current = null;
    if (open) {
      clientNonce.current = sourceClientNonce();
      setDisplayName('');
      setBaseUrl('');
      setApiKey('');
      setRevealed(false);
      setPhase(INITIAL_PHASE);
    }
  }, [open]);

  const draft = React.useCallback((protocolOrder?: SourceProtocol[]): ApiKeySourceCreate => ({
    kind: 'api_key',
    vendor: 'custom',
    ...(displayName.trim() ? { display_name: displayName.trim() } : {}),
    base_url: baseUrl.trim(),
    key: apiKey.trim(),
    client_nonce: clientNonce.current,
    ...(protocolOrder ? { protocol_order: protocolOrder } : {}),
  }), [apiKey, baseUrl, displayName]);

  const persist = React.useCallback(async (seq: number, protocolOrder?: SourceProtocol[]) => {
    setPhase({ kind: 'working', origin: 'add', stage: 'persist' });
    try {
      const created = await modelsApi.createApiKeySource(draft(protocolOrder));
      if (attempt.current !== seq) return;
      onAddedRef.current(created);
      onCloseRef.current();
    } catch (error) {
      if (attempt.current !== seq) return;
      const failure = apiFailure(error);
      const definitiveClientFailure = failure?.serverNamed
        && failure.responseStatus !== undefined
        && failure.responseStatus >= 400
        && failure.responseStatus < 500
        && failure.responseStatus !== 409;
      setPhase(definitiveClientFailure
        ? { kind: 'persist_failure', messageKey: failure.detail ?? failure.code ?? null, protocolOrder }
        : { kind: 'save_unconfirmed', protocolOrder });
    }
  }, [draft]);

  const observe = React.useCallback(async (
    origin: AddApiKeyOrigin,
    protocolOrder?: SourceProtocol[],
  ) => {
    const seq = ++attempt.current;
    observationAbort.current?.abort();
    const controller = new AbortController();
    observationAbort.current = controller;
    setPhase({ kind: 'working', origin, stage: 'observe' });
    try {
      const observation = await modelsApi.observeApiKeySource({
        vendor: 'custom',
        base_url: baseUrl.trim(),
        key: apiKey.trim(),
        ...(protocolOrder ? { protocol_order: protocolOrder } : {}),
      }, controller.signal);
      if (attempt.current !== seq) return;
      observationAbort.current = null;
      const verdict = classifyObservation(observation);
      if (verdict.kind === 'ready') {
        if (origin === 'pull') setPhase({ kind: 'form', report: observation });
        else await persist(seq, protocolOrder);
      } else if (verdict.kind === 'undetermined') {
        setPhase({ kind: 'undetermined', origin, observation, hint: null });
      } else if (verdict.kind === 'inventory') {
        setPhase({ kind: 'inventory', origin, observation });
      } else {
        setPhase({ kind: 'failure', origin, cause: verdict.cause });
      }
    } catch (error) {
      if (attempt.current !== seq || isAbortError(error)) return;
      observationAbort.current = null;
      setPhase({
        kind: 'failure',
        origin,
        cause: apiFailure(error)?.code === 'engine_down' ? 'engineDown' : 'unclassified',
      });
    }
  }, [apiKey, baseUrl, persist]);

  const cancel = React.useCallback(() => {
    if (phase.kind === 'working' && phase.stage === 'persist') return;
    attempt.current += 1;
    observationAbort.current?.abort();
    observationAbort.current = null;
    if ('origin' in phase && phase.origin === 'pull') {
      setPhase(INITIAL_PHASE);
      return;
    }
    onCloseRef.current();
  }, [phase]);

  const retry = async () => {
    if (phase.kind === 'undetermined') {
      if (!phase.hint) return;
      await observe(phase.origin, protocolOrderWithHint(phase.hint));
      return;
    }
    if (phase.kind === 'inventory') {
      const order = phase.observation.protocol
        ? protocolOrderWithHint(phase.observation.protocol)
        : undefined;
      // 2026-08-11 ruling: retry repeats the complete observation. There is no
      // inventory-only credential lifetime or server capability.
      await observe(phase.origin, order);
      return;
    }
    if (phase.kind === 'save_unconfirmed') {
      const reconciliation = await reconcileUnknownWrite(
        () => modelsApi.listSources(),
        (sources) => sources.find((source) => source.client_nonce === clientNonce.current),
      );
      if (reconciliation.kind === 'committed') {
        onAddedRef.current({
          source: reconciliation.value,
          added_to: [],
          adopted_by: reconciliation.value.adopted_by ?? [],
        });
        onCloseRef.current();
        return;
      }
      if (reconciliation.kind === 'absent') {
        await persist(++attempt.current, phase.protocolOrder);
      }
      return;
    }
    if (phase.kind === 'persist_failure') {
      await persist(++attempt.current, phase.protocolOrder);
      return;
    }
    if (phase.kind === 'failure') await observe(phase.origin);
  };

  const addAnyway = async () => {
    if (phase.kind !== 'inventory' || phase.origin !== 'add' || !phase.observation.protocol) return;
    const seq = ++attempt.current;
    await persist(seq, protocolOrderWithHint(phase.observation.protocol));
  };

  const editEndpoint = (value: string) => {
    setBaseUrl(value);
    if (phase.kind === 'form' && phase.report) setPhase(INITIAL_PHASE);
  };
  const editKey = (value: string) => {
    setApiKey(value);
    if (phase.kind === 'form' && phase.report) setPhase(INITIAL_PHASE);
  };
  const editDisplayName = (value: string) => {
    setDisplayName(value);
    if (phase.kind === 'persist_failure') setPhase(INITIAL_PHASE);
  };

  const isWorking = phase.kind === 'working';
  const formLocked = isWorking || phase.kind === 'save_unconfirmed';
  const canCancel = !(phase.kind === 'working' && phase.stage === 'persist');
  const trimmedDisplayName = displayName.trim();
  const displayNameValid = displayName.length === 0
    || (trimmedDisplayName.length > 0 && trimmedDisplayName.length <= SOURCE_DISPLAY_NAME_MAX_LENGTH);
  const canObserve = Boolean(baseUrl.trim() && apiKey.trim()) && !formLocked;
  const canSubmit = canObserve && displayNameValid;
  const showForm = phase.kind !== 'undetermined' && phase.kind !== 'inventory';
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(next) => !next && canCancel && cancel()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="model-hub-add-key-overlay fixed inset-0 z-50" />
        <DialogPrimitive.Content
          className="model-hub-add-key-dialog fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col gap-0 overflow-y-auto border border-border-strong bg-surface p-0 shadow-xl outline-none"
          onEscapeKeyDown={(event) => { if (!canCancel) event.preventDefault(); }}
          onPointerDownOutside={(event) => { if (!canCancel) event.preventDefault(); }}
        >
        <header className="model-hub-add-key-head flex flex-col border-b border-border px-5 py-4">
          <div className="flex items-center justify-between gap-3">
            <DialogPrimitive.Title className="model-hub-add-key-title font-bold text-foreground">
              {t('settings.models.addKey.title')}
            </DialogPrimitive.Title>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="model-hub-ink-white-59 size-[27px]"
              aria-label={t('settings.models.addKey.cancel')}
              disabled={!canCancel}
              onClick={cancel}
            >
              <X className="size-[15px]" />
            </Button>
          </div>
          <DialogPrimitive.Description className="model-hub-add-key-subtitle model-hub-ink-muted-b3 font-mono">
            {t('settings.models.addKey.subtitle')}
          </DialogPrimitive.Description>
        </header>

        {showForm && (
          <div className="model-hub-add-key-body flex flex-col">
            <Field className="model-hub-add-key-field" labelClassName="model-hub-add-key-label" label={t('settings.models.addKey.field.name')}>
              {(id) => <Input id={id} value={displayName} disabled={formLocked} aria-invalid={!displayNameValid} onChange={(event) => editDisplayName(event.target.value)} className="model-hub-add-key-input" />}
            </Field>
            <Field className="model-hub-add-key-field" labelClassName="model-hub-add-key-label" hintClassName="model-hub-add-key-hint" label={t('settings.models.addKey.field.baseUrl')} hint={t('settings.models.addKey.field.baseUrl.hint')}>
              {(id) => <Input id={id} value={baseUrl} disabled={formLocked} autoComplete="url" spellCheck={false} onChange={(event) => editEndpoint(event.target.value)} className="model-hub-add-key-input font-mono" />}
            </Field>
            <Field className="model-hub-add-key-field" labelClassName="model-hub-add-key-label" label={t('settings.models.addKey.field.apiKey')}>
              {(id) => <span className="model-hub-add-key-secret relative flex items-center"><Input id={id} value={apiKey} type={revealed ? 'text' : 'password'} disabled={formLocked} autoComplete="off" spellCheck={false} onChange={(event) => editKey(event.target.value)} className="model-hub-add-key-input w-full pr-10 font-mono" /><Button type="button" variant="ghost" size="icon" className="model-hub-ink-white-59 absolute right-1 size-7" aria-label={t(`settings.models.addKey.field.apiKey.${revealed ? 'conceal' : 'reveal'}`)} disabled={formLocked} onClick={() => setRevealed((value) => !value)}>{revealed ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}</Button></span>}
            </Field>

            {!isWorking && (
              <div className="model-hub-add-key-test-row flex items-center justify-between gap-4">
                <Button
                  type="button"
                  variant="outline"
                  className="model-hub-add-key-secondary"
                  disabled={!canObserve}
                  onClick={() => void observe('pull')}
                >
                  <PlugZap className="size-3" />
                  {t('settings.models.addKey.test')}
                </Button>
                <span className="model-hub-add-key-hint">{t('settings.models.addKey.test.hint')}</span>
              </div>
            )}

            {phase.kind === 'form' && phase.report && (
              <div className="model-hub-add-key-strip model-hub-add-key-strip--success">
                <CheckCircle2 className="model-hub-ink-mint size-3.5 shrink-0" />
                <span>{phase.report.models.length === 0
                  ? t('settings.models.addKey.pull.empty')
                  : t('settings.models.addKey.pull.result', { count: phase.report.models.length })}</span>
              </div>
            )}
            {phase.kind === 'working' && (
              <div className="model-hub-add-key-strip model-hub-add-key-strip--working">
                <LoaderCircle className="model-hub-ink-mint size-3.5 shrink-0 animate-spin" />
                <div className="flex min-w-0 flex-col gap-[3px]">
                  <span className="model-hub-add-key-strip-title text-foreground">{t('settings.models.addKey.adding')}</span>
                  <span className="model-hub-add-key-strip-detail">{t('settings.models.addKey.adding.detail')}</span>
                </div>
              </div>
            )}
            {phase.kind === 'failure' && (
              <div className="model-hub-add-key-strip model-hub-add-key-strip--error">
                <CircleX className="model-hub-add-key-error-ink size-3.5 shrink-0" />
                <div className="flex min-w-0 flex-col gap-[3px]">
                  <span className="model-hub-add-key-error-ink model-hub-add-key-strip-title">{t(failureCopy(phase.cause))}</span>
                  {phase.cause === 'auth' && <span className="model-hub-add-key-strip-detail">{t('settings.models.addKey.fail.auth.detail')}</span>}
                  {phase.cause !== 'engineDown' && <span className="model-hub-add-key-strip-detail">{t('settings.models.addKey.fail.subtitle')}</span>}
                </div>
              </div>
            )}
            {phase.kind === 'save_unconfirmed' && (
              <div className="model-hub-add-key-strip model-hub-add-key-strip--error">
                <CircleX className="model-hub-add-key-error-ink size-3.5 shrink-0" />
                <span className="model-hub-add-key-error-ink model-hub-add-key-strip-title">{t('settings.models.addKey.fail.save')}</span>
              </div>
            )}
            {phase.kind === 'persist_failure' && (
              <div className="model-hub-add-key-strip model-hub-add-key-strip--error">
                <CircleX className="model-hub-add-key-error-ink size-3.5 shrink-0" />
                <span className="model-hub-add-key-error-ink model-hub-add-key-strip-title">
                  {serverText(t, phase.messageKey, 'settings.models.addKey.fail.unclassified')
                    ?? t('settings.models.addKey.fail.unclassified')}
                </span>
              </div>
            )}
          </div>
        )}

        {phase.kind === 'undetermined' && (
          <div className="model-hub-add-key-outcome flex flex-col">
            <div className="model-hub-add-key-outcome-wrap">
              <div className="model-hub-add-key-strip model-hub-add-key-strip--advisory">
                <Info className="model-hub-ink-gold size-3.5 shrink-0" />
                <div className="flex min-w-0 flex-col gap-[3px]">
                  <span className="model-hub-add-key-strip-title model-hub-ink-gold">{t('settings.models.addKey.undetermined.title')}</span>
                  <span className="model-hub-add-key-strip-detail">{t('settings.models.addKey.undetermined.detail')}</span>
                </div>
              </div>
            </div>
            <div className="model-hub-add-key-protocol-field flex flex-col">
              <div className="flex items-center gap-1.5">
                <span className="model-hub-add-key-label">{t('settings.models.addKey.undetermined.label')}</span>
                <Info className="model-hub-ink-white-59 size-[13px]" aria-hidden />
              </div>
              <div className="model-hub-add-key-segments flex max-w-full flex-wrap">
                {SOURCE_PROTOCOLS.map((protocol) => (
                  <button
                    key={protocol}
                    type="button"
                    aria-pressed={phase.hint === protocol}
                    className={cn('model-hub-add-key-segment', phase.hint === protocol && 'is-selected')}
                    onClick={() => setPhase({ ...phase, hint: protocol })}
                  >
                    {t(PROTOCOL_COPY_KEYS[protocol])}
                  </button>
                ))}
              </div>
              <span className="model-hub-add-key-hint">{t('settings.models.addKey.undetermined.hint')}</span>
            </div>
          </div>
        )}

        {phase.kind === 'inventory' && (
          <div className="model-hub-add-key-outcome-wrap">
            <div className="model-hub-add-key-strip model-hub-add-key-strip--advisory model-hub-add-key-strip--inventory">
              <TriangleAlert className="model-hub-ink-gold size-3.5 shrink-0" />
              <span className="model-hub-add-key-strip-title model-hub-ink-gold">{t('settings.models.addKey.inventory.title')}</span>
            </div>
          </div>
        )}

        <footer className="model-hub-add-key-foot model-hub-fill-white-05 flex flex-row items-center justify-end border-t border-border">
          <Button
            type="button"
            variant="outline"
            className="model-hub-add-key-action"
            disabled={!canCancel}
            onClick={cancel}
          >
            {t('settings.models.addKey.cancel')}
          </Button>
          {phase.kind === 'inventory' && phase.origin === 'add' && (
            <Button type="button" variant="outline" className="model-hub-add-key-action" onClick={() => void addAnyway()}>
              {t('settings.models.addKey.addAnyway')}
            </Button>
          )}
          {phase.kind === 'form' && (
            <Button
              type="button"
              variant="brand"
              className="model-hub-add-key-action"
              disabled={!canSubmit}
              onClick={() => void observe('add')}
            >
              {t('settings.models.addKey.submit')}
            </Button>
          )}
          {phase.kind === 'working' && (
            <Button type="button" variant="brand" className="model-hub-add-key-action" disabled>
              <LoaderCircle className="size-3 animate-spin" />
              {t('settings.models.addKey.adding')}
            </Button>
          )}
          {(phase.kind === 'failure' || phase.kind === 'persist_failure' || phase.kind === 'save_unconfirmed' || phase.kind === 'inventory' || phase.kind === 'undetermined') && (
            <Button
              type="button"
              variant="brand"
              className={cn(
                'model-hub-add-key-action',
                (phase.kind === 'inventory' || phase.kind === 'save_unconfirmed' || (phase.kind === 'undetermined' && !phase.hint))
                  && 'model-hub-add-key-action--dim',
              )}
              disabled={phase.kind === 'undetermined' && !phase.hint}
              onClick={() => void retry()}
            >
              {t('settings.models.addKey.retry')}
            </Button>
          )}
        </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};
