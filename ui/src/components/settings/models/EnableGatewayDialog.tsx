import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Check, CircleX, LoaderCircle, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { resumeGatewayAdoption, type GatewayAdoptionFailure } from './gatewayAdoption';
import { modelsApi } from './modelsApi';
import { runtimeHasInstallAsset } from './runtimeLifecycle';
import type { AgentSupply, RuntimeDependency } from './types';
import { BACKEND_ADOPTION_VENDOR_KEY } from './vendorMeta';

const Bullet: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <p className="model-hub-adopt-bullet">
    <Check aria-hidden />
    <span>{children}</span>
  </p>
);

export const EnableGatewayDialog: React.FC<{
  agent: AgentSupply;
  runtime: RuntimeDependency | null;
  onClose: () => void;
  onAdopted: (agent: AgentSupply) => void | Promise<void>;
  onRuntime: (runtime: RuntimeDependency) => void;
  trackWrite: (work: () => Promise<void>) => Promise<void>;
}> = ({ agent, runtime, onClose, onAdopted, onRuntime, trackWrite }) => {
  const { t } = useTranslation();
  const [busy, setBusy] = React.useState(false);
  const [runtimeView, setRuntimeView] = React.useState(runtime);
  const [failure, setFailure] = React.useState<GatewayAdoptionFailure | null>(null);
  const backend = t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend });
  const vendor = t(`settings.models.adopt.vendor.${BACKEND_ADOPTION_VENDOR_KEY[agent.backend]}`);
  const needsInstall = runtimeView?.status.health === 'not_installed';
  const missing = Boolean(needsInstall && runtimeView && runtimeHasInstallAsset(runtimeView));
  const installUnsupported = Boolean(needsInstall && !missing);
  const effectKeys = agent.backend === 'opencode'
    ? ['1.opencode', '2.opencode', '3', '4'] as const
    : ['1', '2', '3', '4'] as const;

  const commit = () => {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    void trackWrite(async () => {
      try {
        const result = await resumeGatewayAdoption(modelsApi, agent.backend);
        if (result.runtime) {
          setRuntimeView(result.runtime);
          onRuntime(result.runtime);
        }
        if (!result.ok) {
          setFailure(result.failure);
          return;
        }
        await Promise.resolve(onAdopted(result.agent)).catch(() => {});
        onClose();
      } finally {
        setBusy(false);
      }
    });
  };

  const reason = failure ? t(`settings.models.adopt.fail.reason.${failure.reason}`) : '';
  const failureDetail = failure?.step === 'install'
    ? t('settings.models.install.fail.detail')
    : failure?.request && failure.responseStatus
      ? t('settings.models.adopt.fail.detail', {
          request: failure.request,
          status: failure.responseStatus,
          reason,
        })
    : failure
      ? [failure.request, failure.responseStatus, reason].filter((slot) => slot !== undefined && slot !== '').join(' · ')
      : '';

  return (
    <DialogPrimitive.Root open onOpenChange={(open) => !open && !busy && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="model-hub-adopt-overlay fixed inset-0 z-50" />
        <DialogPrimitive.Content
          className="model-hub-adopt-dialog fixed left-1/2 top-1/2 z-50 flex -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden bg-surface outline-none"
          onEscapeKeyDown={(event) => { if (busy) event.preventDefault(); }}
          onPointerDownOutside={(event) => { if (busy) event.preventDefault(); }}
        >
          <header className="model-hub-adopt-head">
            <div className="flex items-center justify-between gap-3">
              <DialogPrimitive.Title className="model-hub-adopt-title text-foreground">
                {t('settings.models.adopt.title', { backend })}
              </DialogPrimitive.Title>
              <DialogPrimitive.Close asChild>
                <Button type="button" variant="ghost" size="icon" className="model-hub-adopt-close" disabled={busy} aria-label={t('settings.models.adopt.cancel')}>
                  <X />
                </Button>
              </DialogPrimitive.Close>
            </div>
            <DialogPrimitive.Description className="model-hub-adopt-subtitle">
              {t('settings.models.adopt.subtitle', { backend })}
            </DialogPrimitive.Description>
          </header>

          <div className="model-hub-adopt-body">
            {failure && (
              <div className="model-hub-adopt-failure" role="alert">
                <CircleX aria-hidden />
                <span className="min-w-0">
                  <strong>{t('settings.models.adopt.fail.title')}</strong>
                  <span>{failureDetail}</span>
                </span>
              </div>
            )}
            <section className="model-hub-adopt-section">
              <h3>{t('settings.models.adopt.section.effects')}</h3>
              {missing && (
                <Bullet>{t('settings.models.adopt.effects.install', {
                  component: runtimeView?.manifest.name,
                  duration: t('settings.models.install.duration'),
                })}</Bullet>
              )}
              {effectKeys.map((key) => (
                <Bullet key={key}>{t(`settings.models.adopt.effects.${key}`, { backend, vendor })}</Bullet>
              ))}
            </section>
            <section className="model-hub-adopt-section">
              <h3>{t('settings.models.adopt.section.undo')}</h3>
              {(['1', '2', '3'] as const).map((key) => (
                <Bullet key={key}>{t(`settings.models.adopt.undo.${key}`, { backend })}</Bullet>
              ))}
            </section>
          </div>

          <footer className="model-hub-adopt-foot">
            <Button type="button" variant="outline" className="model-hub-adopt-action" onClick={onClose} disabled={busy}>
              {t('settings.models.adopt.cancel')}
            </Button>
            <Button type="button" variant="brand" className="model-hub-adopt-action" onClick={commit} disabled={busy || installUnsupported}>
              {busy && <LoaderCircle className="animate-spin" />}
              {missing ? t('settings.models.adopt.confirm.install') : t('settings.models.adopt.confirm')}
            </Button>
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};

export default EnableGatewayDialog;
