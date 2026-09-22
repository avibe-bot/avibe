// Mounts the real providers screen inside the real providers, under a stand-in for
// the parts of the setup shell L1 owns.
//
// `fixture.tsx` mounts the `Wizard`, and the Wizard reaches this screen only through
// the registry line that lands after L1 merges. Rather than edit L1's fixture — or
// wait, and ship this screen with no browser evidence at all — this is a second entry
// point onto the same component tree, the way `e2e/model-catalog/gatewayFixture.tsx`
// already does for one surface of Settings.
//
// What is stood in for is only what the shell owns and this screen does not: the
// runtime read, the screen's activity, the flow state, and the primary button. Each
// one is the smallest thing that satisfies the C2 interface — a one-shot read rather
// than L1's owner, a literal `true` rather than a route change — because a fuller
// imitation would start proving the imitation. Everything inside `.onboarding-setup`
// is the shipped component, measured by the browser.
//
// The shell's own header is absent: it is L1's surface and `geometry.spec.ts` already
// measures it in the real Wizard. It contributes nothing to what this fixture is for.
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import { I18nextProvider, useTranslation } from 'react-i18next';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, LoaderCircle } from 'lucide-react';
import { MemoryRouter } from 'react-router-dom';

import en from '../../src/i18n/en.json';
import zh from '../../src/i18n/zh.json';
import '../../src/index.css';
import { ApiProvider } from '../../src/context/ApiContext';
import { ThemeProvider } from '../../src/context/ThemeProvider';
import { ToastProvider } from '../../src/context/ToastProvider';
import { StatusProvider } from '../../src/context/StatusProvider';
import { InstanceAuthorizationContext } from '../../src/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import { Button } from '../../src/components/ui/button';
import { ProvidersScreen } from '../../src/components/onboarding/providers/ProvidersScreen';
import {
  INITIAL_SETUP_FLOW_STATE,
  type SetupAction,
  type SetupFlowState,
  type SetupScreenHandle,
  type SetupScreenId,
} from '../../src/components/onboarding/setupFlow';
import { modelsApi } from '../../src/components/settings/models/modelsApi';
import { useModelHubCapability } from '../../src/components/settings/models/useModelHubCapability';
import {
  foldRegionRead,
  loadingRegion,
  readRegion,
  type RegionRead,
} from '../../src/components/settings/models/regionRead';
import type { RuntimeDependency } from '../../src/components/settings/models/types';

// Exported for the same reason `e2e/model-catalog/gatewayFixture.tsx` exports its
// own: a module that declares a component and exports nothing opts the file out of
// fast refresh, which the lint baseline holds the whole tree to.
export function Shell() {
  const { t } = useTranslation();
  const screen = useRef<SetupScreenHandle>(null);
  const [flowState, setFlowState] = useState<SetupFlowState>(INITIAL_SETUP_FLOW_STATE);
  const [action, setAction] = useState<SetupAction | null>(null);
  const [navigated, setNavigated] = useState<SetupScreenId | null>(null);

  // The shell's single runtime owner, in its smallest honest form: one read, retried
  // on request. L1's owner does more (it is the only writer of this region for three
  // screens); what this screen consumes of it is the projection and the retry.
  const [runtimeRead, setRuntimeRead] = useState<RegionRead<RuntimeDependency>>(loadingRegion);
  const [readToken, setReadToken] = useState(0);
  useEffect(() => {
    let cancelled = false;
    void readRegion(() => modelsApi.getRuntimeStatus()).then((read) => {
      if (!cancelled) setRuntimeRead(read);
    });
    return () => { cancelled = true; };
  }, [readToken]);

  const capability = useModelHubCapability();
  const gatewayEnabled = foldRegionRead<RuntimeDependency, boolean | null>(runtimeRead, {
    loading: () => null,
    ready: (runtime) => runtime.enabled !== false,
    unread: () => null,
    degraded: (runtime) => runtime.enabled !== false,
  });

  const onActionChange = useCallback((next: SetupAction) => setAction(next), []);
  const onNavigate = useCallback((next: SetupScreenId) => setNavigated(next), []);
  const onRetrySetup = useCallback(() => setReadToken((token) => token + 1), []);

  return (
    <div className="onboarding-shell" data-navigated={navigated ?? ''}>
      <main className="onboarding-shell-content">
        <div className="onboarding-step">
          <ProvidersScreen
            ref={screen}
            active
            handoff={false}
            capability={capability === null ? 'pending' : capability ? 'enabled' : 'disabled'}
            gatewayEnabled={gatewayEnabled}
            runtimeRead={runtimeRead}
            onRetrySetup={onRetrySetup}
            flowState={flowState}
            setFlowState={setFlowState}
            onActionChange={onActionChange}
            onNavigate={onNavigate}
          />
          {/* The shell renders the primary action; the screen only publishes it. The
              width rule is a share of the content column, and `.onboarding-setup` —
              which carries that cap in the real shell — is inside the screen here, so
              the cap is restated on the footer's own box rather than left to resolve
              against the full step width. */}
          <div className="onboarding-setup-footer" style={{ maxWidth: 'var(--ob-content-w)' }}>
            <Button
              type="button"
              variant="brand"
              className="group onboarding-action-w onboarding-primary-action"
              disabled={!action || action.disabled}
              onClick={() => screen.current?.activate()}
            >
              {action ? t(action.labelKey, action.labelArgs) : ''}
              {action?.icon === 'spinner' && (
                <LoaderCircle size={16} className="motion-safe:animate-spin" />
              )}
              {action?.icon === 'arrow-right' && <ArrowRight size={16} />}
            </Button>
          </div>
        </div>
      </main>
    </div>
  );
}

const params = new URLSearchParams(location.search);
const language = createInstance();
await language.init({
  lng: params.get('lang') ?? 'en',
  resources: { en: { translation: en }, zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

createRoot(document.getElementById('root')!).render(
  <I18nextProvider i18n={language}>
    <ThemeProvider>
      <ToastProvider>
        <ApiProvider>
          <InstanceAuthorizationContext.Provider
            value={{
              remote: false,
              instanceKind: null,
              instanceRole: 'owner',
              capabilities: OWNER_INSTANCE_CAPABILITIES,
            }}
          >
            <MemoryRouter>
              <StatusProvider>
                <Shell />
              </StatusProvider>
            </MemoryRouter>
          </InstanceAuthorizationContext.Provider>
        </ApiProvider>
      </ToastProvider>
    </ThemeProvider>
  </I18nextProvider>,
);
