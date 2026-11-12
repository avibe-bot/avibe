// The actual Wizard and providers, avoiding unrelated development lazy-route loading.
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import '../../src/i18n';
import '../../src/index.css';
import { ApiProvider } from '../../src/context/ApiContext';
import { ThemeProvider } from '../../src/context/ThemeProvider';
import { ToastProvider } from '../../src/context/ToastProvider';
import { StatusProvider } from '../../src/context/StatusProvider';
import { InstanceAuthorizationContext } from '../../src/context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../src/lib/sessionInfo';
import { Wizard } from '../../src/components/Wizard';

createRoot(document.getElementById('root')!).render(
  <ThemeProvider><ToastProvider><ApiProvider>
    <InstanceAuthorizationContext.Provider value={{ remote: false, instanceKind: null, instanceRole: 'owner', capabilities: OWNER_INSTANCE_CAPABILITIES }}>
      <MemoryRouter initialEntries={['/setup']}><StatusProvider><Routes>
        <Route path="/setup" element={<Wizard />} />
        <Route path="/" element={<div data-setup-completed />} />
      </Routes></StatusProvider></MemoryRouter>
    </InstanceAuthorizationContext.Provider>
  </ApiProvider></ToastProvider></ThemeProvider>,
);
