// AUTH-SETUP-137: actual UI/providers; only HTTP responses are synthetic.
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { BrowserRouter, Route, useNavigate } from 'react-router-dom';
import { SettingsOverlayNavigationBoundary } from '../../src/components/settings/SettingsOverlayNavigationBoundary';
import { SettingsOverlayRouteSurface } from '../../src/components/settings/SettingsOverlayRouteSurface';
import { ApiProvider } from '../../src/context/ApiContext';
import { ToastProvider } from '../../src/context/ToastProvider';
import { InstanceAuthorizationContext } from '../../src/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import { VaultsPage } from '../../src/components/workbench/VaultsPage';
import en from '../../src/i18n/en.json';
import zh from '../../src/i18n/zh.json';
import '../../src/index.css';

const params = new URLSearchParams(location.search);
const owner = params.get('role') !== 'member';
const language = createInstance();
await language.init({ lng: params.get('lang') ?? 'en', resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
export function RemoteAccessDestination() {
  const navigate = useNavigate();
  return <section><h1>Remote Access destination fixture</h1><button onClick={() => navigate(-1)}>Back to Vaults</button></section>;
}

createRoot(document.getElementById('root')!).render(
  <I18nextProvider i18n={language}><ToastProvider><ApiProvider>
    <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: 'personal', instanceRole: owner ? 'owner' : 'editor',
      capabilities: { ...OWNER_INSTANCE_CAPABILITIES, can_manage_instance: owner } }}>
      <BrowserRouter><SettingsOverlayNavigationBoundary desktop={false}>
        <SettingsOverlayRouteSurface fallbackElement={<div>Unknown fixture route</div>}>
          <Route path="/e2e/vault-integrity/entry-fixture.html" element={<main className="h-dvh overflow-auto p-4"><VaultsPage /></main>} />
          <Route path="/settings/remote-access" element={<RemoteAccessDestination />} />
        </SettingsOverlayRouteSurface>
      </SettingsOverlayNavigationBoundary></BrowserRouter>
    </InstanceAuthorizationContext.Provider>
  </ApiProvider></ToastProvider></I18nextProvider>,
);
