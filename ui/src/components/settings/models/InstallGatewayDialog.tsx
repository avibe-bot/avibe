import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Check, CircleX, LoaderCircle, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { modelsApi } from './modelsApi';
import { installRuntimeUntilSettled } from './runtimeLifecycle';
import type { RuntimeDependency } from './types';

const Bullet: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <p className="model-hub-adopt-bullet"><Check aria-hidden /><span>{children}</span></p>
);

export const InstallGatewayDialog: React.FC<{
  runtime: RuntimeDependency;
  onClose: () => void;
  onRuntime: (runtime: RuntimeDependency | null) => void;
}> = ({ runtime, onClose, onRuntime }) => {
  const { t } = useTranslation();
  const [requesting, setRequesting] = React.useState(false);
  const [failed, setFailed] = React.useState(false);
  const initiated = React.useRef(false);
  const previousHealth = React.useRef(runtime.status.health);
  const installing = requesting || runtime.status.health === 'installing';
  const installErrorKey = 'error_key' in runtime.status ? runtime.status.error_key : null;

  React.useEffect(() => {
    const wasInstalling = previousHealth.current === 'installing';
    previousHealth.current = runtime.status.health;
    if (!initiated.current) return;
    if (wasInstalling && runtime.status.health === 'not_installed' && installErrorKey) {
      setRequesting(false);
      setFailed(true);
    } else if (runtime.status.health !== 'installing' && runtime.status.health !== 'not_installed') {
      onClose();
    }
  }, [installErrorKey, onClose, runtime.status.health]);

  const install = () => {
    if (installing) return;
    initiated.current = true;
    setRequesting(true);
    setFailed(false);
    void installRuntimeUntilSettled(modelsApi, onRuntime)
      .then((result) => {
        if (result.failed) setFailed(true);
        else onClose();
      })
      .finally(() => setRequesting(false));
  };

  return (
    <DialogPrimitive.Root open onOpenChange={(open) => !open && !installing && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="model-hub-adopt-overlay fixed inset-0 z-50" />
        <DialogPrimitive.Content
          className="model-hub-adopt-dialog fixed left-1/2 top-1/2 z-50 flex -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden bg-surface outline-none"
          onEscapeKeyDown={(event) => { if (installing) event.preventDefault(); }}
          onPointerDownOutside={(event) => { if (installing) event.preventDefault(); }}
        >
          <header className="model-hub-adopt-head">
            <div className="flex items-center justify-between gap-3">
              <DialogPrimitive.Title className="model-hub-adopt-title text-foreground">
                {t('settings.models.install.title')}
              </DialogPrimitive.Title>
              <DialogPrimitive.Close asChild>
                <Button type="button" variant="ghost" size="icon" className="model-hub-adopt-close" disabled={installing} aria-label={t('settings.models.install.cancel')}>
                  <X />
                </Button>
              </DialogPrimitive.Close>
            </div>
            <DialogPrimitive.Description className="model-hub-adopt-subtitle">
              {t('settings.models.install.subtitle')}
            </DialogPrimitive.Description>
          </header>
          <div className="model-hub-adopt-body">
            {failed && (
              <div className="model-hub-adopt-failure" role="alert">
                <CircleX aria-hidden />
                <span className="min-w-0">
                  <strong>{t('settings.models.install.fail.title')}</strong>
                  <span>{t('settings.models.install.fail.detail')}</span>
                </span>
              </div>
            )}
            <section className="model-hub-adopt-section">
              <h3>{t('settings.models.install.section.effects')}</h3>
              <Bullet>{t('settings.models.install.effects.1', {
                component: runtime.manifest.name,
                duration: t('settings.models.install.duration'),
              })}</Bullet>
              <Bullet>{t('settings.models.install.effects.2')}</Bullet>
              <Bullet>{t('settings.models.install.effects.3')}</Bullet>
            </section>
          </div>
          <footer className="model-hub-adopt-foot">
            <Button type="button" variant="outline" className="model-hub-adopt-action" onClick={onClose} disabled={installing}>
              {t('settings.models.install.cancel')}
            </Button>
            <Button type="button" variant="brand" className="model-hub-adopt-action" onClick={install} disabled={installing}>
              {installing && <LoaderCircle className="animate-spin" />}
              {installing
                ? t('settings.models.install.progress')
                : failed
                  ? t('settings.models.install.retry')
                  : t('settings.models.install.confirm')}
            </Button>
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};

export default InstallGatewayDialog;
