// Mounts the product's own Wizard — the real component that renders the Welcome
// and assistant-setup screens — inside the real providers. Nothing here restates
// onboarding layout or behaviour: the whole point is that the browser measures
// the shipped component tree. Every request is answered by the spec's routes, so
// the fixture never reaches a running service and never writes anything.
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { I18N_OPTIONS } from '../../src/i18n/options';
import en from '../../src/i18n/en.json';
import zh from '../../src/i18n/zh.json';
import '../../src/index.css';
import { ApiProvider } from '../../src/context/ApiContext';
import { ThemeProvider } from '../../src/context/ThemeProvider';
import { ToastProvider } from '../../src/context/ToastProvider';
import { MemoryRouter } from 'react-router-dom';
import { StatusProvider } from '../../src/context/StatusProvider';
import { InstanceAuthorizationContext } from '../../src/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import { Wizard } from '../../src/components/Wizard';
import { CodexProviderConfig } from '../../src/components/settings/providers/CodexProviderConfig';

// Settings uses an internal scroll owner on mobile because global CSS locks the
// document. The isolated provider fixture supplies that shell responsibility.
// The product's own ThemeProvider, reading the same `?theme=` it reads in the app:
// `dark` and `light` are explicit preferences, `system` follows the OS. Writing
// `data-theme` here instead would have been the fixture theming itself, which cannot
// show whether System-mode follow and preference preservation still work.
const params = new URLSearchParams(location.search);
const language = createInstance();
// `lang=environment` starts the way the product does: the shipped detection
// options decide from the browser's preferred languages (and a kept pick).
const environment = params.get('lang') === 'environment';
if (environment) language.use(LanguageDetector);
await language.init({
  ...(environment ? I18N_OPTIONS : { lng: params.get('lang') ?? 'en', interpolation: { escapeValue: false } }),
  resources: { en: { translation: en }, zh: { translation: zh } },
});

createRoot(document.getElementById('root')!).render(
  <I18nextProvider i18n={language}>
    <ThemeProvider>
      <ToastProvider>
        <ApiProvider>
          <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: 'owner', capabilities: OWNER_INSTANCE_CAPABILITIES }}><MemoryRouter><StatusProvider>{params.get('surface') === 'disabled-settings'
            ? <main className="mx-auto h-dvh max-w-xl overflow-y-auto p-6"><CodexProviderConfig /></main>
            : <Wizard />}</StatusProvider></MemoryRouter></InstanceAuthorizationContext.Provider>
        </ApiProvider>
      </ToastProvider>
    </ThemeProvider>
  </I18nextProvider>,
);
