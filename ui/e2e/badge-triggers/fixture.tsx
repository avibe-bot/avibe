import { StrictMode, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Terminal } from 'lucide-react';
import { VersionBadge } from '../../src/components/VersionBadge';
import { Badge } from '../../src/components/ui/badge';
import { BackendRuntimeCard } from '../../src/components/settings/shared/BackendRuntimeCard';
import type { BackendRuntimeState } from '../../src/components/settings/shared/useBackendRuntime';
import { ApiProvider } from '../../src/context/ApiContext';
import { ToastContext } from '../../src/context/ToastContext';
import '../../src/i18n';
import '../../src/index.css';

const noop = () => {};
const asyncNoop = async () => {};
const state = new URLSearchParams(window.location.search).get('state');

export function Fixture() {
  const [enabled, setEnabled] = useState(state !== 'disabled');
  const [cliPath, setCliPath] = useState('codex');
  const runtime: BackendRuntimeState = {
    loaded: true, configError: false, enabled, cliPath,
    cliStatus: state === 'error' ? 'missing' : state === 'loading' ? 'unknown' : 'ok',
    detecting: false, installing: false, installResult: null, installOutputOpen: false,
    savingRuntime: false, runtimeDirty: false, setCliPath, setInstallOutputOpen: noop,
    detect: asyncNoop, install: asyncNoop, onSaveRuntime: asyncNoop,
    toggleEnabled: () => setEnabled((previous) => !previous), handleLifecycleChanged: asyncNoop,
  };
  return <div className="min-h-dvh bg-background text-foreground">
    <header className="flex h-14 items-center justify-between border-b border-border px-4">
      <span>Settings</span><VersionBadge />
    </header>
    <main className="mx-auto max-w-3xl space-y-6 p-4">
      <div><Badge variant="secondary">Reference badge</Badge></div>
      <BackendRuntimeCard backend="codex" label="Codex" description="Backend runtime"
        Icon={Terminal} iconTileClassName="bg-gold" iconClassName="text-gold-ink" runtime={runtime} />
    </main>
  </div>;
}

createRoot(document.getElementById('root')!).render(
  <StrictMode><ToastContext.Provider value={{ showToast: noop }}><ApiProvider>
    <Fixture />
  </ApiProvider></ToastContext.Provider></StrictMode>,
);
