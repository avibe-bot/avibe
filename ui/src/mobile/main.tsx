import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import '../i18n';
import '../index.css';
import { MobileOnboarding } from './MobileOnboarding';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MobileOnboarding />
  </StrictMode>,
);
